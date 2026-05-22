import signal
import threading
import time

from app.core.settings import APP_VERSION, LOCAL_PREVIEW_POLL_SECONDS, PROCESS_ROLE, ensure_app_dirs
from app.core.store import JsonStore
from app.services.archive_worker import ArchiveTaskManager
from app.services.archive_profile_backfill import queue_profile_backfill, read_profile_backfill_status
from app.services.business_logs import append_business_log
from app.services.local_organizer import LocalOrganizerService
from app.services.local_preview_worker import local_preview_queue_marker_mtime, run_local_preview_generation_once
from app.services.remote_refresh import RemoteRefreshManager
from app.services.source_library import SourceLibraryManager
from app.services.subscriptions import SubscriptionManager
from app.services.task_state import TaskStateStore


WORKER_POLL_SECONDS = 2.0
LOCAL_PREVIEW_IDLE_POLL_SECONDS = 15 * 60


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
    remote_refresh_manager = RemoteRefreshManager(
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

    last_local_preview_poll = 0.0
    last_local_preview_full_scan = 0.0
    last_local_preview_marker_mtime = local_preview_queue_marker_mtime()
    local_preview_active = False
    try:
        while not stop_event.wait(WORKER_POLL_SECONDS):
            archive_manager.ensure_worker_for_pending()
            profile_backfill_status = read_profile_backfill_status()
            if profile_backfill_status.get("running"):
                queue_profile_backfill(archive_manager)
            now = time.monotonic()
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
