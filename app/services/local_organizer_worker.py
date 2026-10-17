import argparse
import os
from pathlib import Path

from app.services.local_organizer import LocalOrganizerService, _append_organizer_log


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="makerhub local organizer worker")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--source-path")
    target.add_argument("--package-task-id")
    parser.add_argument("--source-dir")
    parser.add_argument("--library-root")
    parser.add_argument("--move-files", action="store_true")
    args = parser.parse_args()
    if args.source_path and (not args.source_dir or not args.library_root):
        parser.error("--source-dir and --library-root are required with --source-path")
    return args


def main() -> int:
    args = _parse_args()

    try:
        os.nice(15)
    except OSError:
        pass

    service = LocalOrganizerService()
    if args.package_task_id:
        task = next((
            item for item in service.task_store.load_organize_tasks().get("items") or []
            if str(item.get("id") or "") == args.package_task_id
            and str(item.get("kind") or "") == "local_package_import"
        ), None)
        if not task or task.get("status") not in {"pending", "queued", "running"}:
            return 1
        service._process_package_import_task(task)
        latest = next((
            item for item in service.task_store.load_organize_tasks().get("items") or []
            if str(item.get("id") or "") == args.package_task_id
        ), {})
        return 0 if latest.get("status") in {"success", "skipped"} else 1

    source_path = Path(args.source_path).expanduser()
    source_dir = Path(args.source_dir).expanduser()
    library_root = Path(args.library_root).expanduser()
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
