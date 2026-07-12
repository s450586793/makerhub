import errno
import hashlib
import os
import re
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Callable, Optional

from app.core.settings import STATE_DIR

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local dev only.
    fcntl = None


def _env_int(name: str, default: int, minimum: int = 1, maximum: int = 64) -> int:
    try:
        value = int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        value = default
    return max(min(value, maximum), minimum)


RESOURCE_LIMITS = {
    "makerworld_page_api": _env_int("MAKERHUB_LIMIT_MAKERWORLD_REQUESTS", 2, 1, 8),
    "comment_assets": _env_int("MAKERHUB_LIMIT_COMMENT_ASSETS", 4, 1, 16),
    "three_mf_download": _env_int("MAKERHUB_LIMIT_THREE_MF_DOWNLOADS", 1, 1, 4),
    "disk_io": _env_int("MAKERHUB_LIMIT_DISK_IO", 1, 1, 4),
}
_RESOURCE_LIMIT_BOUNDS = {
    "makerworld_page_api": (1, 8),
    "comment_assets": (1, 16),
    "three_mf_download": (1, 4),
    "disk_io": (1, 4),
}
_CONFIG_FIELD_MAP = {
    "makerworld_request_limit": "makerworld_page_api",
    "comment_asset_download_limit": "comment_assets",
    "three_mf_download_limit": "three_mf_download",
    "disk_io_limit": "disk_io",
}
_GLOBAL_SLOT_POLL_SECONDS = 0.05
_PUBLISHED_RESOURCE_LIMITS: dict[str, int] = {}


def _clamp_limit(name: str, value: Any) -> int:
    minimum, maximum = _RESOURCE_LIMIT_BOUNDS.get(name, (1, 64))
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        numeric = RESOURCE_LIMITS.get(name, minimum)
    return max(min(numeric, maximum), minimum)


class _ResourceGate:
    def __init__(self, name: str, capacity: int) -> None:
        self.name = name
        self.capacity = max(int(capacity or 1), 1)
        self._condition = threading.Condition()
        self.active = 0
        self.wait_count = 0
        self.total_wait_ms = 0.0
        self.max_wait_ms = 0.0
        self._next_ticket = 0
        self._waiters: deque[int] = deque()
        self._owners: dict[int, int] = {}

    def set_capacity(self, capacity: int) -> None:
        with self._condition:
            self.capacity = _clamp_limit(self.name, capacity)
            self._condition.notify_all()

    def is_owned_by_current_thread(self) -> bool:
        owner_id = threading.get_ident()
        with self._condition:
            return self._owners.get(owner_id, 0) > 0

    def current_capacity(self) -> int:
        with self._condition:
            return self.capacity

    def acquire(self) -> float:
        started_at = time.perf_counter()
        owner_id = threading.get_ident()
        with self._condition:
            owner_depth = self._owners.get(owner_id, 0)
            if owner_depth > 0:
                self._owners[owner_id] = owner_depth + 1
                return 0.0

            ticket = self._next_ticket
            self._next_ticket += 1
            self._waiters.append(ticket)
            try:
                while self.active >= self.capacity or self._waiters[0] != ticket:
                    self._condition.wait(timeout=1)
                self._waiters.popleft()
                self.active += 1
                self._owners[owner_id] = 1
                self._condition.notify_all()
            except BaseException:
                try:
                    self._waiters.remove(ticket)
                except ValueError:
                    pass
                self._condition.notify_all()
                raise
        return (time.perf_counter() - started_at) * 1000

    def record_wait(self, wait_ms: float) -> None:
        with self._condition:
            if wait_ms >= 1:
                self.wait_count += 1
                self.total_wait_ms += wait_ms
                self.max_wait_ms = max(self.max_wait_ms, wait_ms)

    def release(self) -> None:
        owner_id = threading.get_ident()
        with self._condition:
            owner_depth = self._owners.get(owner_id, 0)
            if owner_depth > 1:
                self._owners[owner_id] = owner_depth - 1
                return
            if owner_depth == 1:
                self._owners.pop(owner_id, None)
                self.active = max(self.active - 1, 0)
            self._condition.notify_all()

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            return {
                "capacity": self.capacity,
                "active": self.active,
                "waiting": len(self._waiters),
                "wait_count": self.wait_count,
                "total_wait_ms": round(self.total_wait_ms, 1),
                "max_wait_ms": round(self.max_wait_ms, 1),
            }


_GATES: dict[str, _ResourceGate] = {}
_GATES_LOCK = threading.Lock()


def _resource_slot_directory(name: str):
    raw_name = str(name or "").strip() or "default"
    clean_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw_name).strip("._") or "default"
    if clean_name != raw_name:
        digest = hashlib.sha256(raw_name.encode("utf-8")).hexdigest()[:12]
        clean_name = f"{clean_name}-{digest}"
    return STATE_DIR / "resource_slots" / clean_name


def _read_control_capacity(path, fallback: int) -> tuple[int, bool]:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        raw = ""
    try:
        return max(int(raw), 1), True
    except (TypeError, ValueError):
        return max(int(fallback or 1), 1), False


def _write_control_capacity(path, capacity: int) -> None:
    temp_path = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
    )
    try:
        with temp_path.open("x", encoding="utf-8") as handle:
            handle.write(str(max(int(capacity or 1), 1)))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _publish_global_capacity(name: str, capacity: int) -> None:
    if fcntl is None:
        return
    slot_dir = _resource_slot_directory(name)
    slot_dir.mkdir(parents=True, exist_ok=True)
    control_path = slot_dir / "capacity.control"
    control_lock = (slot_dir / "capacity.lock").open("a+", encoding="utf-8")
    try:
        fcntl.flock(control_lock.fileno(), fcntl.LOCK_EX)
        current, valid = _read_control_capacity(control_path, capacity)
        clean_capacity = max(int(capacity or 1), 1)
        if not valid or current != clean_capacity:
            _write_control_capacity(control_path, clean_capacity)
    finally:
        fcntl.flock(control_lock.fileno(), fcntl.LOCK_UN)
        control_lock.close()


def _retired_global_slots_are_draining(slot_dir, capacity: int) -> bool:
    for slot_path in slot_dir.glob("*.lock"):
        try:
            slot_number = int(slot_path.stem)
        except ValueError:
            continue
        if slot_number < capacity:
            continue
        handle = slot_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return True
            raise
        _release_global_slot(handle)
    return False


def _try_acquire_global_slot(name: str, capacity: int):
    if fcntl is None:
        return None
    slot_dir = _resource_slot_directory(name)
    slot_dir.mkdir(parents=True, exist_ok=True)
    control_path = slot_dir / "capacity.control"
    control_lock = (slot_dir / "capacity.lock").open("a+", encoding="utf-8")
    try:
        fcntl.flock(control_lock.fileno(), fcntl.LOCK_EX)
        shared_capacity, valid = _read_control_capacity(control_path, 1)
        if not valid:
            shared_capacity = 1

        # 容量读取、退役槽扫描和新槽领取受同一个控制锁保护，避免缩容 TOCTOU。
        if _retired_global_slots_are_draining(slot_dir, shared_capacity):
            return None
        for slot_number in range(shared_capacity):
            handle = (slot_dir / f"{slot_number}.lock").open("a+", encoding="utf-8")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                handle.close()
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    continue
                raise
            return handle
        return None
    finally:
        fcntl.flock(control_lock.fileno(), fcntl.LOCK_UN)
        control_lock.close()


def _acquire_global_slot(name: str, gate: _ResourceGate):
    if fcntl is None:
        return None
    while True:
        handle = _try_acquire_global_slot(name, gate.current_capacity())
        if handle is not None:
            return handle
        time.sleep(_GLOBAL_SLOT_POLL_SECONDS)


def _release_global_slot(handle) -> None:
    if handle is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _gate_for(name: str) -> _ResourceGate:
    clean_name = str(name or "").strip() or "default"
    with _GATES_LOCK:
        gate = _GATES.get(clean_name)
        if gate is None:
            gate = _ResourceGate(clean_name, RESOURCE_LIMITS.get(clean_name, 1))
            _GATES[clean_name] = gate
        return gate


def configure_resource_limits(config: Any, *, publish_global: bool = True) -> dict[str, int]:
    raw_config = config.model_dump() if hasattr(config, "model_dump") else config
    if not isinstance(raw_config, dict):
        raw_config = {}
    changed: dict[str, int] = {}
    with _GATES_LOCK:
        for config_key, resource_name in _CONFIG_FIELD_MAP.items():
            if config_key not in raw_config:
                continue
            next_limit = _clamp_limit(resource_name, raw_config.get(config_key))
            RESOURCE_LIMITS[resource_name] = next_limit
            changed[resource_name] = next_limit
            gate = _GATES.get(resource_name)
            if gate is not None:
                gate.set_capacity(next_limit)
            if publish_global:
                _publish_global_capacity(resource_name, next_limit)
                _PUBLISHED_RESOURCE_LIMITS[resource_name] = next_limit
    return changed


def resource_limits_payload() -> dict[str, int]:
    with _GATES_LOCK:
        return {
            config_key: int(RESOURCE_LIMITS.get(resource_name) or _RESOURCE_LIMIT_BOUNDS[resource_name][0])
            for config_key, resource_name in _CONFIG_FIELD_MAP.items()
        }


@contextmanager
def resource_slot(
    name: str,
    *,
    detail: str = "",
    log_wait: Optional[Callable[..., None]] = None,
    warn_after_ms: float = 250.0,
):
    gate = _gate_for(name)
    started_at = time.perf_counter()
    reentrant = gate.is_owned_by_current_thread()
    gate.acquire()
    global_handle = None
    try:
        if not reentrant:
            global_handle = _acquire_global_slot(name, gate)
        wait_ms = (time.perf_counter() - started_at) * 1000
        gate.record_wait(wait_ms)
        if wait_ms >= warn_after_ms and callable(log_wait):
            try:
                log_wait(
                    "resource_waited",
                    resource=name,
                    detail=detail,
                    wait_ms=round(wait_ms, 1),
                    capacity=gate.current_capacity(),
                )
            except Exception:
                pass
        yield wait_ms
    finally:
        _release_global_slot(global_handle)
        gate.release()


def resource_snapshot() -> dict[str, dict[str, Any]]:
    with _GATES_LOCK:
        names = sorted(set(RESOURCE_LIMITS) | set(_GATES))
    return {name: _gate_for(name).snapshot() for name in names}
