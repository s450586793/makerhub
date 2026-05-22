from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

from app.core.database import (
    DatabaseUnavailable,
    database_configured,
    database_driver_available,
    load_json_state,
    save_json_state,
)
from app.core.settings import CONFIG_DIR, CONFIG_PATH, ensure_app_dirs
from app.schemas.models import AppConfig
from app.services.resource_limiter import configure_resource_limits

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local dev only.
    fcntl = None


class JsonStore:
    _lock = threading.RLock()
    _CONFIG_STATE_KEY = "app_config"

    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        ensure_app_dirs()
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if not self.path.exists() and self._load_database_payload() is None:
                self.save(AppConfig())

    def _uses_database(self) -> bool:
        try:
            return self.path.resolve() == CONFIG_PATH.resolve()
        except OSError:
            return self.path == CONFIG_PATH

    def _load_database_payload(self) -> dict | None:
        if not self._uses_database() or not database_configured() or not database_driver_available():
            return None
        try:
            payload = load_json_state(self._CONFIG_STATE_KEY)
        except (DatabaseUnavailable, OSError, RuntimeError, ValueError):
            return None
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _save_database_payload(self, payload: dict) -> bool:
        if not self._uses_database() or not database_configured() or not database_driver_available():
            return False
        try:
            save_json_state(self._CONFIG_STATE_KEY, payload)
            return True
        except Exception:
            return False

    @contextmanager
    def _file_lock(self):
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+", encoding="utf-8")
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def load(self) -> AppConfig:
        with self._lock, self._file_lock():
            payload = self._load_database_payload()
            if payload is None:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                self._save_database_payload(payload)
            config = AppConfig.model_validate(payload)
            configure_resource_limits(config.advanced)
            return config

    def save(self, config: AppConfig) -> AppConfig:
        with self._lock, self._file_lock():
            payload = config.model_dump()
            self._save_database_payload(payload)
            temp_path = self.path.with_name(f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(self.path)
            configure_resource_limits(config.advanced)
        return config
