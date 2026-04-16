import argparse
import os
from pathlib import Path

from app.services.local_organizer import LocalOrganizerService, _append_organizer_log


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="makerhub local organizer worker")
    parser.add_argument("--source-path", required=True)
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--library-root", required=True)
    parser.add_argument("--move-files", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    try:
        os.nice(15)
    except OSError:
        pass

    source_path = Path(args.source_path).expanduser()
    source_dir = Path(args.source_dir).expanduser()
    library_root = Path(args.library_root).expanduser()

    service = LocalOrganizerService()
    try:
        service.process_candidate(
            source_path=source_path,
            source_dir=source_dir,
            library_root=library_root,
            move_files=bool(args.move_files),
        )
        return 0
    except Exception as exc:
        _append_organizer_log(
            "worker_failed",
            source=source_path.as_posix(),
            error=str(exc),
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
