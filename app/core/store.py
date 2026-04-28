import json
import threading
from pathlib import Path

from app.core.settings import CONFIG_DIR, CONFIG_PATH, ensure_app_dirs
from app.schemas.models import AppConfig
from app.services.resource_limiter import configure_resource_limits


class JsonStore:
    _lock = threading.RLock()

    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        ensure_app_dirs()
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if not self.path.exists():
                self.save(AppConfig())

    def load(self) -> AppConfig:
        with self._lock:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            config = AppConfig.model_validate(payload)
            configure_resource_limits(config.advanced)
            return config

    def save(self, config: AppConfig) -> AppConfig:
        with self._lock:
            self.path.write_text(
                json.dumps(config.model_dump(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            configure_resource_limits(config.advanced)
        return config
