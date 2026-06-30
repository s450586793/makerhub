from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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
        self._loaded_subscription_fingerprints: dict[int, tuple[tuple[Any, ...], ...]] = {}
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

    def _file_payload_paths(self) -> list[Path]:
        return [self.path]

    def _load_file_payload(self) -> dict:
        for path in self._file_payload_paths():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                return payload
        return {}

    @staticmethod
    def _subscription_fingerprint(payload: dict) -> tuple[tuple[Any, ...], ...]:
        subscriptions = payload.get("subscriptions") if isinstance(payload, dict) else []
        if not isinstance(subscriptions, list):
            return ()
        rows: list[tuple[Any, ...]] = []
        for item in subscriptions:
            if not isinstance(item, dict):
                continue
            rows.append(
                (
                    str(item.get("id") or ""),
                    str(item.get("url") or ""),
                    str(item.get("mode") or ""),
                    str(item.get("name") or ""),
                    str(item.get("cron") or ""),
                    bool(item.get("enabled")),
                )
            )
        return tuple(rows)

    def _merge_concurrent_subscription_changes(self, config: AppConfig, payload: dict) -> dict:
        loaded_fingerprint = self._loaded_subscription_fingerprints.get(id(config))
        if loaded_fingerprint is None:
            return payload
        if self._subscription_fingerprint(payload) != loaded_fingerprint:
            return payload

        current_payload = self._load_database_payload() if self._uses_database() else self._load_file_payload()
        current_subscriptions = current_payload.get("subscriptions") if isinstance(current_payload, dict) else None
        if not isinstance(current_subscriptions, list):
            return payload
        if self._subscription_fingerprint(current_payload) == loaded_fingerprint:
            return payload

        merged = dict(payload)
        merged["subscriptions"] = current_subscriptions
        return merged

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
            config = AppConfig.model_validate(payload)
            self._loaded_subscription_fingerprints[id(config)] = self._subscription_fingerprint(config.model_dump())
            configure_resource_limits(config.advanced)
            return config

    def save(self, config: AppConfig) -> AppConfig:
        with self._lock, self._file_lock():
            payload = config.model_dump()
            payload = self._merge_concurrent_subscription_changes(config, payload)
            if self._uses_database():
                self._save_database_payload(payload)
            else:
                temp_path = self.path.with_name(f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                temp_path.replace(self.path)
            saved = AppConfig.model_validate(payload)
            self._loaded_subscription_fingerprints[id(config)] = self._subscription_fingerprint(payload)
            self._loaded_subscription_fingerprints[id(saved)] = self._subscription_fingerprint(payload)
            configure_resource_limits(saved.advanced)
        return saved
