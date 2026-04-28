import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

from app.core.settings import CONFIG_DIR, CONFIG_PATH, ensure_app_dirs
from app.schemas.models import AppConfig
from app.services.resource_limiter import configure_resource_limits

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local dev only.
    fcntl = None


class JsonStore:
    _lock = threading.RLock()

    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        ensure_app_dirs()
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if not self.path.exists():
                self.save(AppConfig())

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
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            config = AppConfig.model_validate(payload)
            configure_resource_limits(config.advanced)
            return config

    def save(self, config: AppConfig) -> AppConfig:
        with self._lock, self._file_lock():
            temp_path = self.path.with_name(f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            temp_path.write_text(json.dumps(config.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(self.path)
            configure_resource_limits(config.advanced)
        return config
