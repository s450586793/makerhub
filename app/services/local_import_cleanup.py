from __future__ import annotations

import fcntl
import os
import re
import shutil
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.core.timezone import parse_datetime
from app.services.business_logs import append_business_log


STAGING_NAME_PATTERN = re.compile(r"\d{13}-\d+-\d+-[0-9a-f]{8}")
_STAGING_LEASES = threading.local()


@contextmanager
def staging_in_use(path: Path, *, blocking: bool = True) -> Iterator[bool]:
    """上传、解压和回收共享目录锁，任务历史被清空也不能删掉使用中的文件。"""
    key = (os.getpid(), str(path.absolute()))
    leases = getattr(_STAGING_LEASES, "paths", None)
    if leases is None:
        leases = _STAGING_LEASES.paths = set()
    if key in leases:
        yield blocking
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            acquired = True
        except BlockingIOError:
            yield False
            return
        current = path.lstat()
        opened = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError("本地导入暂存目录已被替换。")
        leases.add(key)
        yield True
    finally:
        if acquired:
            leases.discard(key)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _owned_staging(path: Path, root: Path) -> bool:
    if root.is_symlink() or path.is_symlink() or not STAGING_NAME_PATTERN.fullmatch(path.name):
        return False
    if path.parent.absolute() != root.absolute() or path.parent.resolve() != root.resolve():
        return False
    info = path.lstat()
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_dev == root.stat().st_dev
        and not os.path.ismount(path)
    )


def _check_tree(path: Path) -> None:
    device = path.stat().st_dev
    for parent, dirs, _files in os.walk(path, followlinks=False):
        for name in dirs:
            child = Path(parent) / name
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_dev != device or os.path.ismount(child):
                raise OSError("本地导入暂存内存在符号链接或挂载目录，已停止清理。")


def cleanup_staging_dir(path: Path, *, root: Path) -> bool:
    """调用方须持有 staging_in_use；清理失败不改变导入结果，交给周期任务重试。"""
    try:
        if not path.exists() and not path.is_symlink():
            return True
        if not _owned_staging(path, root):
            return False
        _check_tree(path)
        shutil.rmtree(path)
        return True
    except OSError as exc:
        append_business_log(
            "organizer", "local_import_staging_cleanup_failed",
            "本地导入暂存清理失败，将在后台重试。", level="warning",
            staging_dir=path.as_posix(), error=str(exc)[:240],
        )
        return False


def _task_protects_staging(task: dict[str, Any]) -> bool:
    status = str(task.get("status") or "").strip().lower()
    meta = task.get("meta") if isinstance(task.get("meta"), dict) else {}
    return (
        status not in {"success", "skipped", "failed", "cancelled"}
        or bool(task.get("retry_at") or meta.get("retry_at") or meta.get("resumable"))
    )


def cleanup_expired_import_staging(
    *, task_store: Any, root: Path, ttl_seconds: float, now: float | None = None,
) -> dict[str, int]:
    result = {"removed": 0, "errors": 0}
    if not root.exists() or root.is_symlink():
        return result
    timestamp = time.time() if now is None else now
    try:
        for path in root.iterdir():
            if not _owned_staging(path, root):
                continue
            try:
                with staging_in_use(path, blocking=False) as acquired:
                    if not acquired:
                        continue
                    # 获取锁后再读任务；数据库不可用时不把任务当成空集合。
                    payload = task_store.load_organize_tasks()
                    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                        raise ValueError("本地整理任务状态无效，已停止暂存清理。")
                    tasks = [
                        task for task in payload["items"]
                        if isinstance(task, dict) and str(task.get("staging_dir") or "").strip()
                        and Path(str(task["staging_dir"])).absolute() == path.absolute()
                    ]
                    if any(_task_protects_staging(task) for task in tasks):
                        continue
                    updated_at = path.stat().st_mtime
                    for task in tasks:
                        parsed = parse_datetime(task.get("updated_at"))
                        if parsed is not None:
                            updated_at = max(updated_at, parsed.timestamp())
                    terminal = tasks and all(
                        str(task.get("status") or "").lower() in {"success", "skipped"}
                        for task in tasks
                    )
                    if not terminal and timestamp - updated_at < ttl_seconds:
                        continue
                    if cleanup_staging_dir(path, root=root):
                        result["removed"] += 1
                    else:
                        result["errors"] += 1
            except FileNotFoundError:
                continue
    except Exception as exc:
        result["errors"] += 1
        append_business_log(
            "organizer", "local_import_staging_maintenance_failed",
            "本地导入暂存回收检查失败，稍后自动重试。", level="warning",
            error=str(exc)[:240],
        )
    return result
