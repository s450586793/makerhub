import signal
import os
import threading
import time

from app.core.settings import APP_VERSION, LOCAL_PREVIEW_POLL_SECONDS, PROCESS_ROLE, ensure_app_dirs
from app.core.store import JsonStore
from app.core.timezone import now_iso as china_now_iso
from app.services.account_cookie_maintenance import run_account_cookie_maintenance_once
from app.services.archive_worker import ArchiveTaskManager
from app.services.archive_profile_backfill import (
    queue_profile_backfill,
    read_profile_backfill_status,
    should_auto_run_database_migration,
    write_profile_backfill_status,
)
from app.services.business_logs import append_business_log
from app.services.local_organizer import LocalOrganizerService
from app.services.local_preview_worker import local_preview_queue_marker_mtime, run_local_preview_generation_once
from app.services.source_refresh import SourceRefreshTaskManager
from app.services.source_library import SourceLibraryManager
from app.services.subscriptions import SubscriptionManager
from app.services.task_state import TaskStateStore


WORKER_POLL_SECONDS = 2.0
LOCAL_PREVIEW_IDLE_POLL_SECONDS = 15 * 60
ACCOUNT_COOKIE_MAINTENANCE_POLL_SECONDS = 10 * 60


def _runtime_engine_enabled() -> bool:
    return os.getenv("MAKERHUB_RUNTIME_ENGINE", "").strip().lower() in {"1", "true", "v2", "runtime"}


def _execute_runtime_engine_once() -> dict:
    from app.api.runtime_routes import runtime_engine

    return runtime_engine.execute_next_batch()


def _start_profile_backfill_worker(
    archive_manager: ArchiveTaskManager,
    profile_backfill_status: dict,
) -> threading.Thread:
    options = {
        "database_rebuild_requested": bool(profile_backfill_status.get("database_rebuild_requested")),
        "force_database_rebuild": bool(profile_backfill_status.get("force_database_rebuild")),
        "database_only": bool(profile_backfill_status.get("database_only")),
    }
    thread = threading.Thread(
        target=_run_profile_backfill_worker,
        args=(archive_manager, options),
        name="makerhub-profile-backfill",
        daemon=True,
    )
    thread.start()
    return thread


def _run_profile_backfill_worker(archive_manager: ArchiveTaskManager, options: dict) -> None:
    try:
        queue_profile_backfill(
            archive_manager,
            rebuild_database=bool(options.get("database_rebuild_requested")),
            force_database_rebuild=bool(options.get("force_database_rebuild")),
            database_only=bool(options.get("database_only")),
        )
    except Exception as exc:
        append_business_log(
            "archive_backfill",
            "profile_backfill_worker_failed",
            "现有库信息补全后台线程失败。",
            level="error",
            error=str(exc),
        )


def main() -> int:
    ensure_app_dirs()
    stop_event = threading.Event()

    def _stop(_signum, _frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

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
    try:
        initial_backfill_status = read_profile_backfill_status()
        if (
            not initial_backfill_status.get("running")
            and should_auto_run_database_migration()
        ):
            write_profile_backfill_status(
                {
                    "running": True,
                    "phase": "database_migration",
                    "database_rebuild_requested": True,
                    "force_database_rebuild": False,
                    "database_only": False,
                    "auto_database_migration": True,
                    "started_at": china_now_iso(),
                    "finished_at": "",
                    "last_error": "",
                    "last_result": {},
                }
            )
            append_business_log(
                "database",
                "archive_model_index_auto_migration_queued",
                "检测到数据库索引未完成，已自动提交全库索引初始化。",
            )
    except Exception as exc:
        append_business_log(
            "database",
            "archive_model_index_auto_migration_check_failed",
            "数据库索引自动初始化检测失败。",
            level="warning",
            error=str(exc),
        )

    last_local_preview_poll = 0.0
    last_local_preview_full_scan = 0.0
    last_local_preview_marker_mtime = local_preview_queue_marker_mtime()
    local_preview_active = False
    last_account_cookie_poll = 0.0
    profile_backfill_thread: threading.Thread | None = None
    try:
        while not stop_event.wait(WORKER_POLL_SECONDS):
            if _runtime_engine_enabled():
                try:
                    _execute_runtime_engine_once()
                except Exception as exc:
                    append_business_log(
                        "runtime",
                        "worker_tick_failed",
                        "运行核心 worker 轮询失败。",
                        level="warning",
                        error=str(exc),
                    )
            archive_manager.ensure_worker_for_pending()
            profile_backfill_status = read_profile_backfill_status()
            if profile_backfill_thread is not None and not profile_backfill_thread.is_alive():
                profile_backfill_thread = None
            if profile_backfill_status.get("running") and profile_backfill_thread is None:
                profile_backfill_thread = _start_profile_backfill_worker(archive_manager, profile_backfill_status)
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
        local_organizer.stop()
        append_business_log(
            "system",
            "worker_stopped",
            "makerhub worker 已停止。",
            app_version=APP_VERSION,
            process_role=PROCESS_ROLE,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
