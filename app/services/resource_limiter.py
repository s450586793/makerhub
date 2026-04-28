import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Optional


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


class _ResourceGate:
    def __init__(self, name: str, capacity: int) -> None:
        self.name = name
        self.capacity = max(int(capacity or 1), 1)
        self._semaphore = threading.BoundedSemaphore(self.capacity)
        self._lock = threading.Lock()
        self.active = 0
        self.wait_count = 0
        self.total_wait_ms = 0.0
        self.max_wait_ms = 0.0

    def acquire(self) -> float:
        started_at = time.perf_counter()
        self._semaphore.acquire()
        wait_ms = (time.perf_counter() - started_at) * 1000
        with self._lock:
            self.active += 1
            if wait_ms >= 1:
                self.wait_count += 1
                self.total_wait_ms += wait_ms
                self.max_wait_ms = max(self.max_wait_ms, wait_ms)
        return wait_ms

    def release(self) -> None:
        with self._lock:
            self.active = max(self.active - 1, 0)
        self._semaphore.release()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "capacity": self.capacity,
                "active": self.active,
                "wait_count": self.wait_count,
                "total_wait_ms": round(self.total_wait_ms, 1),
                "max_wait_ms": round(self.max_wait_ms, 1),
            }


_GATES: dict[str, _ResourceGate] = {}
_GATES_LOCK = threading.Lock()


def _gate_for(name: str) -> _ResourceGate:
    clean_name = str(name or "").strip() or "default"
    with _GATES_LOCK:
        gate = _GATES.get(clean_name)
        if gate is None:
            gate = _ResourceGate(clean_name, RESOURCE_LIMITS.get(clean_name, 1))
            _GATES[clean_name] = gate
        return gate


@contextmanager
def resource_slot(
    name: str,
    *,
    detail: str = "",
    log_wait: Optional[Callable[..., None]] = None,
    warn_after_ms: float = 250.0,
):
    gate = _gate_for(name)
    wait_ms = gate.acquire()
    if wait_ms >= warn_after_ms and callable(log_wait):
        try:
            log_wait(
                "resource_waited",
                resource=name,
                detail=detail,
                wait_ms=round(wait_ms, 1),
                capacity=gate.capacity,
            )
        except Exception:
            pass
    try:
        yield wait_ms
    finally:
        gate.release()


def resource_snapshot() -> dict[str, dict[str, Any]]:
    with _GATES_LOCK:
        names = sorted(set(RESOURCE_LIMITS) | set(_GATES))
    return {name: _gate_for(name).snapshot() for name in names}
