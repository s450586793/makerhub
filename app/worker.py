import os
import signal
import sys
import threading
import time
import uuid
from typing import Callable

from app.core.settings import APP_VERSION
from app.services.self_update import (
    WORKER_HEARTBEAT_MAX_AGE_SECONDS,
    WORKER_START_TOKEN_ENV,
    record_worker_heartbeat,
    worker_heartbeat_readiness,
)


WORKER_HEALTHCHECK_MODE = "--healthcheck" in sys.argv[1:]

if not WORKER_HEALTHCHECK_MODE:
    from app.core.database import DatabaseUnavailable, close_database_pool
    from app.core.settings import LOCAL_PREVIEW_POLL_SECONDS, PROCESS_ROLE, ensure_app_dirs
    from app.core.store import JsonStore
    from app.services.account_cookie_maintenance import run_account_cookie_maintenance_once
    from app.services.archive_worker import ArchiveTaskManager
    from app.services.archive_model_index_rebuild import (
        read_archive_model_index_rebuild_status,
        request_archive_model_index_rebuild,
        run_archive_model_index_rebuild,
        should_auto_rebuild_database_index,
    )
    from app.services.business_logs import append_business_log
    from app.services.catalog import release_catalog_memory
    from app.services.cloakbrowser_session import stop_idle_profiles
    from app.services.database_maintenance import run_database_maintenance_if_due
    from app.services.local_organizer import LocalOrganizerService
    from app.services.local_preview_worker import local_preview_queue_marker_mtime, run_local_preview_generation_once
    from app.services.process_memory import process_rss_mib, release_process_memory
    from app.services.source_refresh import SourceRefreshTaskManager
    from app.services.source_library import SourceLibraryManager, release_source_library_memory
    from app.services.subscriptions import SubscriptionManager
    from app.services.task_state import TaskStateStore


WORKER_POLL_SECONDS = 2.0
WORKER_IDLE_POLL_SECONDS = 15.0
WORKER_HEARTBEAT_INTERVAL_SECONDS = 10.0
LOCAL_PREVIEW_IDLE_POLL_SECONDS = 15 * 60
ACCOUNT_COOKIE_MAINTENANCE_POLL_SECONDS = 10 * 60
WORKER_MEMORY_MAINTENANCE_INTERVAL_SECONDS = 5 * 60
WORKER_RECYCLE_RSS_MIB_ENV = "MAKERHUB_WORKER_RECYCLE_RSS_MIB"
DEFAULT_WORKER_RECYCLE_RSS_MIB = 2048
WORKER_HARD_RECYCLE_RSS_MIB_ENV = "MAKERHUB_WORKER_HARD_RECYCLE_RSS_MIB"
DEFAULT_WORKER_HARD_RECYCLE_RSS_MIB = 4096
CLOAKBROWSER_IDLE_CHECK_INTERVAL_SECONDS = 60
AUTO_MISSING_3MF_RETRY_INTERVAL_SECONDS = 60


def archive_queue_has_runnable_work(queue: dict) -> bool:
    if int((queue or {}).get("running_count") or 0) > 0:
        return True
    if int((queue or {}).get("queued_count") or 0) <= 0:
        return False
    queued = (queue or {}).get("queued")
    if not isinstance(queued, list) or not queued:
        return True
    return any(
        str(item.get("status") or "queued").strip().lower() in {"", "queued", "pending"}
        for item in queued
        if isinstance(item, dict)
    )


def worker_recycle_rss_mib() -> int:
    raw = str(os.environ.get(WORKER_RECYCLE_RSS_MIB_ENV) or DEFAULT_WORKER_RECYCLE_RSS_MIB).strip()
    try:
        return max(int(raw), 0)
    except (TypeError, ValueError):
        return DEFAULT_WORKER_RECYCLE_RSS_MIB


def worker_hard_recycle_rss_mib() -> int:
    raw = str(
        os.environ.get(WORKER_HARD_RECYCLE_RSS_MIB_ENV)
        or DEFAULT_WORKER_HARD_RECYCLE_RSS_MIB
    ).strip()
    try:
        return max(int(raw), 0)
    except (TypeError, ValueError):
        return DEFAULT_WORKER_HARD_RECYCLE_RSS_MIB


def worker_should_recycle(
    *,
    rss_mib: float,
    threshold_mib: int,
    hard_threshold_mib: int,
    activity: dict[str, bool],
) -> bool:
    rss = float(rss_mib or 0.0)
    hard_threshold = int(hard_threshold_mib or 0)
    if hard_threshold > 0 and rss >= hard_threshold:
        return True
    return (
        int(threshold_mib or 0) > 0
        and rss >= int(threshold_mib)
        and not any(bool(value) for value in activity.values())
    )


def run_worker_memory_maintenance(
    activity_loader: Callable[[], dict[str, bool]],
    *,
    threshold_mib: int | None = None,
    hard_threshold_mib: int | None = None,
) -> dict:
    result = {
        "recycle": False,
        "reason": "",
        "rss_mib": 0.0,
        "threshold_mib": 0,
        "hard_threshold_mib": 0,
        "error": "",
    }
    try:
        activity = activity_loader()
        release_catalog_memory()
        release_source_library_memory()
        release_process_memory()
        rss_mib = process_rss_mib()
        effective_threshold = worker_recycle_rss_mib() if threshold_mib is None else max(int(threshold_mib), 0)
        effective_hard_threshold = (
            worker_hard_recycle_rss_mib()
            if hard_threshold_mib is None
            else max(int(hard_threshold_mib), 0)
        )
        hard_limit_reached = (
            effective_hard_threshold > 0
            and rss_mib >= effective_hard_threshold
        )
        recycle = worker_should_recycle(
            rss_mib=rss_mib,
            threshold_mib=effective_threshold,
            hard_threshold_mib=effective_hard_threshold,
            activity=activity,
        )
        result.update(
            recycle=recycle,
            reason="hard_limit" if recycle and hard_limit_reached else "idle_limit" if recycle else "",
            rss_mib=rss_mib,
            threshold_mib=effective_threshold,
            hard_threshold_mib=effective_hard_threshold,
        )
    except Exception as exc:
        result["error"] = str(exc)[:240]
    return result


def run_worker_idle_missing_3mf_retry(
    archive_manager,
    archive_queue: dict,
    *,
    limit: int | None = None,
) -> dict:
    if archive_queue_has_runnable_work(archive_queue):
        return {"accepted": False, "reason": "archive_queue_busy"}
    try:
        return archive_manager.retry_idle_missing_3mf(limit=limit)
    except Exception as exc:
        append_business_log(
            "missing_3mf",
            "idle_retry_failed",
            "归档队列空闲补档检查失败。",
            level="warning",
            error=str(exc)[:240],
        )
        return {"accepted": False, "reason": "error", "error": str(exc)[:240]}


def _run_database_maintenance() -> dict:
    result = run_database_maintenance_if_due()
    if result.get("ran") and (
        int(result.get("events_deleted") or 0)
        or int(result.get("logs_deleted") or 0)
        or result.get("errors")
    ):
        append_business_log(
            "database",
            "retention_cleanup_completed",
            "数据库历史状态清理已完成。",
            level="warning" if result.get("errors") else "info",
            events_deleted=int(result.get("events_deleted") or 0),
            logs_deleted=int(result.get("logs_deleted") or 0),
            errors=result.get("errors") or {},
        )
    return result


def _run_worker_heartbeat_loop(stop_event: threading.Event, start_token: str) -> None:
    while not stop_event.wait(WORKER_HEARTBEAT_INTERVAL_SECONDS):
        try:
            record_worker_heartbeat(start_token=start_token)
        except Exception:
            pass


def worker_poll_seconds(queue: dict, *, rebuild_running: bool) -> float:
    if archive_queue_has_runnable_work(queue) or rebuild_running:
        return WORKER_POLL_SECONDS
    return WORKER_IDLE_POLL_SECONDS


def _start_archive_model_index_rebuild_worker(status: dict) -> threading.Thread:
    options = {"force": bool(status.get("force"))}
    thread = threading.Thread(
        target=_run_archive_model_index_rebuild_worker,
        args=(options,),
        name="makerhub-archive-model-index-rebuild",
        daemon=True,
    )
    thread.start()
    return thread


def _run_archive_model_index_rebuild_worker(options: dict) -> None:
    try:
        run_archive_model_index_rebuild(force=bool(options.get("force")))
    except Exception as exc:
        append_business_log(
            "database",
            "archive_model_index_rebuild_worker_failed",
            "归档模型数据库索引后台重建线程失败。",
            level="error",
            error=str(exc),
        )


def main() -> int:
    if WORKER_HEALTHCHECK_MODE:
        readiness = worker_heartbeat_readiness(
            expected_start_token=os.getenv(WORKER_START_TOKEN_ENV) or None,
            expected_version=APP_VERSION,
        )
        return 0 if readiness.get("ready") else 1

    ensure_app_dirs()
    stop_event = threading.Event()

    def _stop(_signum, _frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    worker_start_token = os.getenv(WORKER_START_TOKEN_ENV) or uuid.uuid4().hex
    record_worker_heartbeat(start_token=worker_start_token)
    heartbeat_thread = threading.Thread(
        target=_run_worker_heartbeat_loop,
        args=(stop_event, worker_start_token),
        name="makerhub-worker-heartbeat",
        daemon=True,
    )
    heartbeat_thread.start()

    store = JsonStore()
    task_store = TaskStateStore()
    archive_manager = ArchiveTaskManager(background_enabled=True)
    subscription_manager = SubscriptionManager(
        archive_manager=archive_manager,
        store=store,
        task_store=task_store,
        background_enabled=True,
    )
    local_organizer = LocalOrganizerService(store=store, task_store=task_store)
    source_library_manager = SourceLibraryManager(store=store, task_store=task_store)
    remote_refresh_manager = SourceRefreshTaskManager(
        store=store,
        task_store=task_store,
        archive_manager=archive_manager,
        background_enabled=True,
    )
    queue = archive_manager.resume_pending_tasks()
    subscription_manager.start()
    local_organizer.start()
    source_library_manager.start()
    remote_refresh_manager.start()

    append_business_log(
        "system",
        "worker_started",
        "makerhub worker 已启动。",
        app_version=APP_VERSION,
        process_role=PROCESS_ROLE,
        queued_count=int(queue.get("queued_count") or 0),
        recovered_active=int(queue.get("recovered_count") or 0),
    )
    _run_database_maintenance()
    try:
        initial_rebuild_status = read_archive_model_index_rebuild_status()
        if (
            not initial_rebuild_status.get("running")
            and should_auto_rebuild_database_index()
        ):
            request_archive_model_index_rebuild(
                force=False,
                auto=True,
                reason="worker_startup",
            )
            append_business_log(
                "database",
                "archive_model_index_auto_rebuild_queued",
                "检测到数据库索引未完成，已自动提交全库索引初始化。",
            )
    except Exception as exc:
        append_business_log(
            "database",
            "archive_model_index_auto_rebuild_check_failed",
            "数据库索引自动初始化检测失败。",
            level="warning",
            error=str(exc),
        )

    last_local_preview_poll = 0.0
    last_local_preview_full_scan = 0.0
    last_local_preview_marker_mtime = local_preview_queue_marker_mtime()
    local_preview_active = False
    last_account_cookie_poll = 0.0
    last_cloakbrowser_idle_check = 0.0
    last_auto_missing_3mf_retry = 0.0
    archive_model_index_rebuild_thread: threading.Thread | None = None
    next_poll_seconds = WORKER_POLL_SECONDS
    last_memory_maintenance = 0.0
    database_failures = 0
    try:
        while not stop_event.wait(next_poll_seconds):
            try:
                _run_database_maintenance()
                try:
                    local_organizer.start()
                except Exception as exc:
                    append_business_log(
                        "organizer",
                        "daemon_restart_failed",
                        "本地整理后台进程启动失败，稍后自动重试。",
                        level="warning",
                        error=str(exc)[:240],
                    )
                archive_queue = archive_manager.ensure_worker_for_pending()
                archive_model_index_rebuild_status = read_archive_model_index_rebuild_status()
                if archive_model_index_rebuild_thread is not None and not archive_model_index_rebuild_thread.is_alive():
                    archive_model_index_rebuild_thread = None
                if archive_model_index_rebuild_status.get("running") and archive_model_index_rebuild_thread is None:
                    archive_model_index_rebuild_thread = _start_archive_model_index_rebuild_worker(archive_model_index_rebuild_status)
                next_poll_seconds = worker_poll_seconds(
                    archive_queue,
                    rebuild_running=bool(archive_model_index_rebuild_status.get("running")),
                )
                now = time.monotonic()
                if now - last_cloakbrowser_idle_check >= CLOAKBROWSER_IDLE_CHECK_INTERVAL_SECONDS:
                    last_cloakbrowser_idle_check = now
                    try:
                        idle_result = stop_idle_profiles()
                        if int(idle_result.get("stopped_count") or 0) > 0:
                            append_business_log(
                                "system",
                                "cloakbrowser_idle_profiles_stopped",
                                "已停止空闲的指纹浏览器 profile。",
                                stopped_count=int(idle_result.get("stopped_count") or 0),
                                stopped_profiles=list(idle_result.get("stopped_profiles") or []),
                            )
                    except Exception as exc:
                        append_business_log(
                            "system",
                            "cloakbrowser_idle_cleanup_failed",
                            "指纹浏览器空闲 profile 清理失败。",
                            level="warning",
                            error=str(exc)[:240],
                        )
                if now - last_account_cookie_poll >= ACCOUNT_COOKIE_MAINTENANCE_POLL_SECONDS:
                    last_account_cookie_poll = now
                    try:
                        run_account_cookie_maintenance_once(store=store)
                    except Exception as exc:
                        append_business_log(
                            "settings",
                            "online_account_cookie_maintenance_failed",
                            "线上账号 Cookie 定时检测失败。",
                            level="warning",
                            error=str(exc),
                        )
                if now - last_auto_missing_3mf_retry >= AUTO_MISSING_3MF_RETRY_INTERVAL_SECONDS:
                    last_auto_missing_3mf_retry = now
                    retry_result = run_worker_idle_missing_3mf_retry(archive_manager, archive_queue)
                    if retry_result.get("accepted"):
                        archive_queue = task_store.load_archive_queue_compact(item_limit=1)
                marker_mtime = local_preview_queue_marker_mtime()
                marker_changed = bool(marker_mtime and marker_mtime != last_local_preview_marker_mtime)
                quick_interval = max(int(LOCAL_PREVIEW_POLL_SECONDS or 20), 5)
                idle_interval = max(int(LOCAL_PREVIEW_IDLE_POLL_SECONDS or 0), quick_interval)
                should_poll_preview = (
                    (local_preview_active or marker_changed) and now - last_local_preview_poll >= quick_interval
                ) or (
                    now - last_local_preview_full_scan >= idle_interval
                )
                if should_poll_preview:
                    last_local_preview_poll = now
                    if not (local_preview_active or marker_changed):
                        last_local_preview_full_scan = now
                    try:
                        result = run_local_preview_generation_once()
                        local_preview_active = bool(result.get("processed"))
                        if marker_changed:
                            last_local_preview_marker_mtime = marker_mtime
                        if not local_preview_active:
                            last_local_preview_full_scan = now
                    except Exception as exc:
                        local_preview_active = False
                        if marker_changed:
                            last_local_preview_marker_mtime = marker_mtime
                        append_business_log(
                            "model",
                            "local_model_preview_worker_error",
                            "本地模型 Three.js 封面 worker 轮询失败。",
                            level="warning",
                            error=str(exc),
                        )
                if now - last_memory_maintenance >= WORKER_MEMORY_MAINTENANCE_INTERVAL_SECONDS:
                    last_memory_maintenance = now

                    def _load_worker_activity() -> dict[str, bool]:
                        organize_queue = task_store.load_organize_tasks()
                        return {
                            "archive": int(archive_queue.get("running_count") or 0) > 0,
                            "subscription": subscription_manager._has_active_work(),
                            "source_library": source_library_manager._has_active_work(),
                            "source_refresh": remote_refresh_manager._has_active_work(),
                            "organizer": int(organize_queue.get("running_count") or 0) > 0,
                            "index_rebuild": bool(archive_model_index_rebuild_status.get("running")),
                            "preview": bool(local_preview_active),
                        }

                    memory_result = run_worker_memory_maintenance(_load_worker_activity)
                    if memory_result.get("error"):
                        append_business_log(
                            "system",
                            "worker_memory_maintenance_failed",
                            "makerhub worker 空闲内存维护失败。",
                            level="warning",
                            error=str(memory_result.get("error") or "")[:240],
                        )
                    if memory_result.get("recycle"):
                        hard_limit = memory_result.get("reason") == "hard_limit"
                        append_business_log(
                            "system",
                            "worker_memory_recycle",
                            (
                                "makerhub worker 内存超过硬上限，正在重启并自动恢复任务。"
                                if hard_limit
                                else "makerhub worker 已到达安全任务间隙，正在重启回收内存。"
                            ),
                            level="warning",
                            reason=str(memory_result.get("reason") or ""),
                            rss_mib=float(memory_result.get("rss_mib") or 0.0),
                            threshold_mib=int(memory_result.get("threshold_mib") or 0),
                            hard_threshold_mib=int(memory_result.get("hard_threshold_mib") or 0),
                        )
                        break
            except DatabaseUnavailable:
                database_failures += 1
                next_poll_seconds = min(5.0 * (2 ** min(database_failures - 1, 3)), 30.0)
                # 数据库拥堵时直接输出日志，避免告警再次等待连接池。
                print(
                    "[makerhub][warning][system] worker_database_unavailable "
                    f"数据库暂时不可用，保留任务并在 {next_poll_seconds:g} 秒后重试。",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            if database_failures:
                append_business_log(
                    "system",
                    "worker_database_recovered",
                    "数据库连接已恢复，继续处理原有任务。",
                    failed_polls=database_failures,
                )
                database_failures = 0
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=WORKER_HEARTBEAT_INTERVAL_SECONDS + 1)
        local_organizer.stop()
        append_business_log(
            "system",
            "worker_stopped",
            "makerhub worker 已停止。",
            app_version=APP_VERSION,
            process_role=PROCESS_ROLE,
        )
        close_database_pool()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
