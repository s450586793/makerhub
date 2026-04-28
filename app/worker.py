import signal
import threading

from app.core.settings import APP_VERSION, PROCESS_ROLE, ensure_app_dirs
from app.core.store import JsonStore
from app.services.archive_worker import ArchiveTaskManager
from app.services.archive_profile_backfill import queue_profile_backfill, read_profile_backfill_status
from app.services.business_logs import append_business_log
from app.services.local_organizer import LocalOrganizerService
from app.services.remote_refresh import RemoteRefreshManager
from app.services.subscriptions import SubscriptionManager
from app.services.task_state import TaskStateStore


WORKER_POLL_SECONDS = 2.0


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
    remote_refresh_manager = RemoteRefreshManager(
        store=store,
        task_store=task_store,
        archive_manager=archive_manager,
        background_enabled=True,
    )

    queue = archive_manager.resume_pending_tasks()
    subscription_manager.start()
    local_organizer.start()
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
        while not stop_event.wait(WORKER_POLL_SECONDS):
            archive_manager.ensure_worker_for_pending()
            profile_backfill_status = read_profile_backfill_status()
            if profile_backfill_status.get("running"):
                queue_profile_backfill(archive_manager)
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
