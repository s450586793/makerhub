import os
import signal
import sys
import threading
import time
import uuid

from app.core.settings import APP_VERSION
from app.services.self_update import (
    WORKER_START_TOKEN_ENV,
    record_worker_heartbeat,
    worker_heartbeat_readiness,
)


WORKER_HEALTHCHECK_MODE = "--healthcheck" in sys.argv[1:]

if not WORKER_HEALTHCHECK_MODE:
    from app.core.database import close_database_pool
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
    from app.services.database_maintenance import run_database_maintenance_if_due
    from app.services.local_organizer import LocalOrganizerService
    from app.services.local_preview_worker import local_preview_queue_marker_mtime, run_local_preview_generation_once
    from app.services.source_refresh import SourceRefreshTaskManager
    from app.services.source_library import SourceLibraryManager
    from app.services.subscriptions import SubscriptionManager
    from app.services.task_state import TaskStateStore


WORKER_POLL_SECONDS = 2.0
LOCAL_PREVIEW_IDLE_POLL_SECONDS = 15 * 60
ACCOUNT_COOKIE_MAINTENANCE_POLL_SECONDS = 10 * 60


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
    while not stop_event.wait(WORKER_POLL_SECONDS):
        try:
            record_worker_heartbeat(start_token=start_token)
        except Exception:
            pass


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
    archive_model_index_rebuild_thread: threading.Thread | None = None
    try:
        while not stop_event.wait(WORKER_POLL_SECONDS):
            _run_database_maintenance()
            archive_manager.ensure_worker_for_pending()
            archive_model_index_rebuild_status = read_archive_model_index_rebuild_status()
            if archive_model_index_rebuild_thread is not None and not archive_model_index_rebuild_thread.is_alive():
                archive_model_index_rebuild_thread = None
            if archive_model_index_rebuild_status.get("running") and archive_model_index_rebuild_thread is None:
                archive_model_index_rebuild_thread = _start_archive_model_index_rebuild_worker(archive_model_index_rebuild_status)
            now = time.monotonic()
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
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=WORKER_POLL_SECONDS + 1)
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
