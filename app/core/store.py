from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from app.core.database import database_configured
from app.core.database_json_state import (
    load_database_json_state,
    load_database_json_state_with_revision,
    save_database_json_state,
    update_database_json_state,
)
from app.core.settings import CONFIG_DIR, CONFIG_PATH, ensure_app_dirs
from app.schemas.models import AppConfig
from app.services.resource_limiter import configure_resource_limits

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local dev only.
    fcntl = None


class ConfigConflictError(RuntimeError):
    pass


_MISSING = object()


def _merge_config_changes(baseline: Any, current: Any, desired: Any, *, path: str = "config") -> Any:
    if desired == baseline:
        return deepcopy(current)
    if current == baseline or current == desired:
        return deepcopy(desired)

    if isinstance(baseline, dict) and isinstance(current, dict) and isinstance(desired, dict):
        merged = deepcopy(current)
        for key in baseline.keys() | desired.keys():
            baseline_value = baseline.get(key, _MISSING)
            current_value = current.get(key, _MISSING)
            desired_value = desired.get(key, _MISSING)
            field_path = f"{path}.{key}"
            if desired_value is _MISSING:
                if current_value is _MISSING:
                    continue
                if current_value == baseline_value:
                    merged.pop(key, None)
                    continue
                raise ConfigConflictError(f"配置字段 {field_path} 已被其他操作修改，请重新加载。")
            if baseline_value is _MISSING:
                if current_value is _MISSING or current_value == desired_value:
                    merged[key] = deepcopy(desired_value)
                    continue
                raise ConfigConflictError(f"配置字段 {field_path} 已被其他操作修改，请重新加载。")
            if current_value is _MISSING:
                raise ConfigConflictError(f"配置字段 {field_path} 已被其他操作删除，请重新加载。")
            merged[key] = _merge_config_changes(
                baseline_value,
                current_value,
                desired_value,
                path=field_path,
            )
        return merged

    raise ConfigConflictError(f"配置字段 {path} 已被其他操作修改，请重新加载。")


class JsonStore:
    _lock = threading.RLock()
    _CONFIG_STATE_KEY = "app_config"

    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self._loaded_configs: dict[int, tuple[dict[str, Any], int | None]] = {}
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

    def _load_database_record(self) -> tuple[dict, int | None] | None:
        if not self._uses_database():
            return None
        if database_configured():
            return load_database_json_state_with_revision(self._CONFIG_STATE_KEY, {})
        return self._load_database_payload() or {}, None

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

    def _write_file_payload(self, payload: dict[str, Any]) -> None:
        temp_path = self.path.with_name(f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.path)

    def _remember_loaded(self, config: AppConfig, payload: dict[str, Any], revision: int | None) -> None:
        self._loaded_configs[id(config)] = (deepcopy(payload), revision)

    def load(self) -> AppConfig:
        with self._lock, self._file_lock():
            record = self._load_database_record()
            if record is None:
                payload = self._load_file_payload()
                revision = None
            else:
                payload, revision = record
            if record is not None and not payload:
                payload = AppConfig().model_dump()
                if database_configured():
                    payload, revision = update_database_json_state(
                        self._CONFIG_STATE_KEY,
                        payload,
                        lambda current: current or payload,
                    )
                else:
                    self._save_database_payload(payload)
            config = AppConfig.model_validate(payload)
            self._remember_loaded(config, config.model_dump(), revision)
            configure_resource_limits(config.advanced)
            return config

    def save(self, config: AppConfig) -> AppConfig:
        with self._lock, self._file_lock():
            desired = config.model_dump()
            loaded = self._loaded_configs.get(id(config))
            if self._uses_database():
                current = self._load_database_payload() or {}
            else:
                current = self._load_file_payload()
            baseline = (
                loaded[0]
                if loaded is not None
                else AppConfig.model_validate(current or {}).model_dump()
            )

            def merge_latest(latest: dict[str, Any]) -> dict[str, Any]:
                normalized_latest = AppConfig.model_validate(latest or {}).model_dump()
                merged = _merge_config_changes(baseline, normalized_latest, desired)
                return AppConfig.model_validate(merged).model_dump()

            revision: int | None = None
            if self._uses_database() and database_configured():
                payload, revision = update_database_json_state(
                    self._CONFIG_STATE_KEY,
                    AppConfig().model_dump(),
                    merge_latest,
                )
            else:
                payload = merge_latest(current)
                if self._uses_database():
                    self._save_database_payload(payload)
                else:
                    self._write_file_payload(payload)
            saved = AppConfig.model_validate(payload)
            self._remember_loaded(config, payload, revision)
            self._remember_loaded(saved, payload, revision)
            configure_resource_limits(saved.advanced)
        return saved

    def update(
        self,
        mutator: Callable[[AppConfig], AppConfig | dict[str, Any] | None],
        *,
        expected_revision: int | None = None,
    ) -> AppConfig:
        if not callable(mutator):
            raise TypeError("配置 mutator 必须可调用。")

        def apply_mutation(payload: dict[str, Any]) -> dict[str, Any]:
            config = AppConfig.model_validate(payload or AppConfig().model_dump())
            result = mutator(config)
            if result is None:
                updated = config
            elif isinstance(result, AppConfig):
                updated = result
            elif isinstance(result, dict):
                updated = AppConfig.model_validate(result)
            else:
                raise TypeError("配置 mutator 必须返回 AppConfig、dict 或 None。")
            return updated.model_dump()

        with self._lock, self._file_lock():
            revision: int | None = None
            if self._uses_database() and database_configured():
                payload, revision = update_database_json_state(
                    self._CONFIG_STATE_KEY,
                    AppConfig().model_dump(),
                    apply_mutation,
                    expected_revision=expected_revision,
                )
            else:
                current = self._load_database_payload() if self._uses_database() else self._load_file_payload()
                payload = apply_mutation(current or {})
                if self._uses_database():
                    self._save_database_payload(payload)
                else:
                    self._write_file_payload(payload)
            saved = AppConfig.model_validate(payload)
            self._remember_loaded(saved, payload, revision)
            configure_resource_limits(saved.advanced)
            return saved
