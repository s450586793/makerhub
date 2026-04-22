import os
import signal

from app.services.local_organizer import LocalOrganizerService, _append_organizer_log


def main() -> int:
    os.environ["MAKERHUB_LOCAL_ORGANIZER_DAEMON"] = "1"
    try:
        os.nice(10)
    except OSError:
        pass

    service = LocalOrganizerService()

    def _stop(_signum, _frame) -> None:
        service.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    _append_organizer_log("daemon_loop_started", pid=os.getpid())
    try:
        service.run_forever()
    finally:
        service.stop()
        _append_organizer_log("daemon_loop_stopped", pid=os.getpid())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
