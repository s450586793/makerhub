from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

from app.core.database_json_state import load_database_json_state, save_database_json_state
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
            if not self._uses_database() and not self.path.exists():
                self.save(AppConfig())

    def _uses_database(self) -> bool:
        try:
            return self.path.resolve() == CONFIG_PATH.resolve()
        except OSError:
            return self.path == CONFIG_PATH

    def _load_database_payload(self) -> dict | None:
        if not self._uses_database():
            return None
        return load_database_json_state(self._CONFIG_STATE_KEY, {})

    def _save_database_payload(self, payload: dict) -> bool:
        if not self._uses_database():
            return False
        save_database_json_state(self._CONFIG_STATE_KEY, payload)
        return True

    def _load_file_payload(self) -> dict:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _merge_missing_cookie_values(payload: dict, file_payload: dict) -> bool:
        file_cookies_by_platform = {
            str(item.get("platform") or "").strip(): item
            for item in file_payload.get("cookies") or []
            if isinstance(item, dict)
            and str(item.get("platform") or "").strip()
            and str(item.get("cookie") or "").strip()
        }
        if not file_cookies_by_platform:
            return False

        merged = []
        seen = set()
        changed = False
        for item in payload.get("cookies") or []:
            if not isinstance(item, dict):
                continue
            platform = str(item.get("platform") or "").strip()
            replacement = file_cookies_by_platform.get(platform)
            if replacement and not str(item.get("cookie") or "").strip():
                next_item = dict(item)
                for key, value in replacement.items():
                    if key == "platform":
                        continue
                    if value not in (None, ""):
                        next_item[key] = value
                merged.append(next_item)
                changed = True
            else:
                merged.append(dict(item))
            if platform:
                seen.add(platform)

        for platform, item in file_cookies_by_platform.items():
            if platform not in seen:
                merged.append(dict(item))
                changed = True

        if changed:
            payload["cookies"] = merged
        return changed

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
                payload = self._load_file_payload()
            elif not payload:
                payload = AppConfig().model_dump()
                self._save_database_payload(payload)
            elif self._uses_database() and self._merge_missing_cookie_values(payload, self._load_file_payload()):
                self._save_database_payload(payload)
            config = AppConfig.model_validate(payload)
            configure_resource_limits(config.advanced)
            return config

    def save(self, config: AppConfig) -> AppConfig:
        with self._lock, self._file_lock():
            payload = config.model_dump()
            if self._uses_database():
                self._save_database_payload(payload)
            else:
                temp_path = self.path.with_name(f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                temp_path.replace(self.path)
            configure_resource_limits(config.advanced)
        return config
