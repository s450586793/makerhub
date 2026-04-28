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

    def set_capacity(self, capacity: int) -> None:
        with self._condition:
            self.capacity = _clamp_limit(self.name, capacity)
            self._condition.notify_all()

    def acquire(self) -> float:
        started_at = time.perf_counter()
        with self._condition:
            while self.active >= self.capacity:
                self._condition.wait(timeout=1)
            self.active += 1
        wait_ms = (time.perf_counter() - started_at) * 1000
        with self._condition:
            if wait_ms >= 1:
                self.wait_count += 1
                self.total_wait_ms += wait_ms
                self.max_wait_ms = max(self.max_wait_ms, wait_ms)
        return wait_ms

    def release(self) -> None:
        with self._condition:
            self.active = max(self.active - 1, 0)
            self._condition.notify()

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
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


def configure_resource_limits(config: Any) -> dict[str, int]:
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
    return changed


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
