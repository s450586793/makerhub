import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Callable, Iterable, Optional

from fastapi import Request

from app.core.database import DatabaseUnavailable
from app.core.database_json_state import load_database_json_state, update_database_json_state
from app.core.security import (
    default_admin_password_hash,
    generate_api_token,
    generate_session_id,
    hash_api_token,
    hash_password,
    verify_password,
)
from app.core.settings import STATE_DIR, ensure_app_dirs
from app.core.store import JsonStore
from app.core.timezone import now as china_now, now_iso as china_now_iso, parse_datetime
from app.schemas.models import ApiTokenRecord, ApiTokenView, AppConfig


SESSION_COOKIE_NAME = "makerhub_session"
SESSION_TTL_DAYS = 14
SESSION_AUTH_CACHE_TTL_SECONDS = 15
LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60
LOGIN_FAILURE_LOCK_SECONDS = 5 * 60
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_STATE_KEY = "auth_login_failures"
LOGIN_FAILURE_MAX_KEYS = 2048
LOGIN_FAILURE_FALLBACK_TTL_SECONDS = 30 * 60
SESSIONS_PATH = STATE_DIR / "auth_sessions.json"
SESSIONS_STATE_KEY = "auth_sessions"
_SESSION_AUTH_CACHE: dict[str, tuple[float, int, dict]] = {}
_SESSION_AUTH_CACHE_LOCK = threading.RLock()
TOKEN_PERMISSION_LABELS = {
    "archive_write": "提交归档任务",
    "mobile_import": "移动端/本地导入",
    "models_read": "查看模型库",
    "share_manage": "接收/管理分享",
    "system_manage": "系统管理",
    "token_manage": "Token 管理",
}
DEFAULT_API_TOKEN_PERMISSIONS = ["archive_write"]
TOKEN_PERMISSION_ORDER = list(TOKEN_PERMISSION_LABELS.keys())
MIN_ADMIN_PASSWORD_LENGTH = 12
ADMIN_BOOTSTRAP_PASSWORD_FILENAME = "admin-bootstrap-password"


@dataclass(frozen=True)
class BootstrapCredentialResult:
    rotated: bool
    source: str
    bootstrap_path: Path


def _bootstrap_password_path() -> Path:
    return STATE_DIR / ADMIN_BOOTSTRAP_PASSWORD_FILENAME


def _write_bootstrap_password(path: Path, password: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(f"{password}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary_path, path)
        path.chmod(0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def _prepare_generated_bootstrap_password(path: Path, candidate: str) -> str:
    try:
        _write_bootstrap_password(path, candidate)
        return candidate
    except FileExistsError:
        existing = path.read_text(encoding="utf-8").strip()
        if len(existing) < MIN_ADMIN_PASSWORD_LENGTH:
            raise RuntimeError(f"管理员 bootstrap 密码文件无效：{path}")
        path.chmod(0o600)
        return existing


def _remove_bootstrap_password() -> None:
    _bootstrap_password_path().unlink(missing_ok=True)


def _admin_credential_requires_rotation(password_hash: str) -> bool:
    stored_hash = str(password_hash or "").strip()
    return not stored_hash or verify_password("admin", stored_hash)


def normalize_token_permissions(values: Optional[Iterable[str]]) -> list[str]:
    seen: set[str] = set()
    permissions: list[str] = []
    legacy_values = {"api", "all", "*"}
    for value in values or []:
        clean = str(value or "").strip().lower()
        if clean in legacy_values:
            clean = "archive_write"
        if clean not in TOKEN_PERMISSION_LABELS or clean in seen:
            continue
        seen.add(clean)
        permissions.append(clean)
    return permissions or list(DEFAULT_API_TOKEN_PERMISSIONS)


def token_status(record: ApiTokenRecord, now=None) -> str:
    if record.disabled or record.revoked_at:
        return "revoked"
    if record.expires_at:
        expires_at = parse_datetime(record.expires_at)
        if expires_at is not None and expires_at < (now or china_now()):
            return "expired"
    return "active"


class AuthManager:
    def __init__(self, store: Optional[JsonStore] = None, sessions_path=SESSIONS_PATH) -> None:
        self.store = store or JsonStore()
        self.sessions_path = sessions_path
        self._login_failures: dict[str, dict[str, float]] = {}
        self._login_failure_lock = threading.RLock()
        ensure_app_dirs()

    def _read_sessions(self) -> dict:
        payload = load_database_json_state(SESSIONS_STATE_KEY, {"items": [], "generation": 0})
        if not isinstance(payload, dict):
            payload = {"items": [], "generation": 0}
        payload["items"] = payload.get("items") if isinstance(payload.get("items"), list) else []
        try:
            payload["generation"] = max(int(payload.get("generation") or 0), 0)
        except (TypeError, ValueError):
            payload["generation"] = 0
        return payload

    def _update_sessions(self, mutator) -> dict:
        payload, _revision = update_database_json_state(
            SESSIONS_STATE_KEY,
            {"items": [], "generation": 0},
            mutator,
        )
        return payload

    def _clear_session_cache(self, session_id: str = "") -> None:
        with _SESSION_AUTH_CACHE_LOCK:
            clean_session_id = str(session_id or "").strip()
            if clean_session_id:
                _SESSION_AUTH_CACHE.pop(clean_session_id, None)
            else:
                _SESSION_AUTH_CACHE.clear()

    def _cached_session(self, session_id: str, generation: int) -> Optional[dict]:
        clean_session_id = str(session_id or "").strip()
        if not clean_session_id:
            return None
        now_ts = time.monotonic()
        with _SESSION_AUTH_CACHE_LOCK:
            item = _SESSION_AUTH_CACHE.get(clean_session_id)
            if not item:
                return None
            expires_at, cached_generation, session = item
            if expires_at <= now_ts or cached_generation != generation:
                _SESSION_AUTH_CACHE.pop(clean_session_id, None)
                return None
            return dict(session)

    def _cache_session(self, session_id: str, session: dict, generation: int) -> None:
        clean_session_id = str(session_id or "").strip()
        if not clean_session_id or not isinstance(session, dict):
            return
        with _SESSION_AUTH_CACHE_LOCK:
            _SESSION_AUTH_CACHE[clean_session_id] = (
                time.monotonic() + SESSION_AUTH_CACHE_TTL_SECONDS,
                generation,
                dict(session),
            )

    def _prune_sessions(self, payload: dict) -> dict:
        now = china_now()
        items = []
        for item in payload.get("items") or []:
            expires_at = str(item.get("expires_at") or "")
            if expires_at:
                expires_at_dt = parse_datetime(expires_at)
                if expires_at_dt is None:
                    continue
                if expires_at_dt < now:
                    continue
            items.append(item)
        payload["items"] = items
        return payload

    def authenticate_credentials(self, username: str, password: str) -> bool:
        config = self.store.load()
        if str(username or "").strip() != config.user.username:
            return False
        return verify_password(password, config.user.password_hash)

    def default_password_active(self) -> bool:
        config = self.store.load()
        return (
            str(config.user.username or "").strip() == "admin"
            and verify_password("admin", config.user.password_hash)
            and str(config.user.password_hash or "").strip() == default_admin_password_hash()
        )

    def login_failure_key(self, request: Optional[Request], username: str) -> str:
        clean_username = str(username or "").strip().lower() or "-"
        client_host = ""
        if request is not None and request.client:
            client_host = str(request.client.host or "").strip().lower()
        return f"{client_host or '-'}:{clean_username}"

    def login_backoff_seconds(self, key: str) -> int:
        now = china_now().timestamp()
        clean_key = str(key or "")
        try:
            payload = load_database_json_state(LOGIN_FAILURE_STATE_KEY, {"items": {}})
            items = payload.get("items") if isinstance(payload, dict) else {}
            item = items.get(clean_key) if isinstance(items, dict) else None
            return self._login_backoff_for_item(item, now)
        except DatabaseUnavailable:
            pass
        with self._login_failure_lock:
            self._prune_login_failure_items(self._login_failures, now)
            return self._login_backoff_for_item(self._login_failures.get(clean_key), now)

    @staticmethod
    def _login_backoff_for_item(item: object, now: float) -> int:
        if not isinstance(item, dict):
            return 0
        locked_until = float(item.get("locked_until") or 0)
        if locked_until <= now:
            return 0
        return max(int(locked_until - now), 1)

    @staticmethod
    def _prune_login_failure_items(items: dict, now: float) -> None:
        expired_keys = []
        for item_key, item in items.items():
            if not isinstance(item, dict):
                expired_keys.append(item_key)
                continue
            locked_until = float(item.get("locked_until") or 0)
            last_seen = float(item.get("last_seen") or item.get("first_seen") or 0)
            if locked_until <= now and now - last_seen > LOGIN_FAILURE_FALLBACK_TTL_SECONDS:
                expired_keys.append(item_key)
        for item_key in expired_keys:
            items.pop(item_key, None)

    @staticmethod
    def _cap_login_failure_items(items: dict, *, preserve_key: str = "") -> None:
        limit = max(int(LOGIN_FAILURE_MAX_KEYS), 1)
        while len(items) > limit:
            candidates = [item for item in items if item != preserve_key] or list(items)
            oldest_key = min(
                candidates,
                key=lambda item_key: float(
                    (items.get(item_key) or {}).get("last_seen")
                    or (items.get(item_key) or {}).get("first_seen")
                    or 0
                ),
            )
            items.pop(oldest_key, None)

    @staticmethod
    def _record_login_failure_item(items: dict, key: str, now: float) -> int:
        item = items.get(key) if isinstance(items.get(key), dict) else {}
        first_seen = float(item.get("first_seen") or now)
        if now - first_seen > LOGIN_FAILURE_WINDOW_SECONDS:
            first_seen = now
            count = 0
        else:
            count = int(item.get("count") or 0)
        count += 1
        locked_until = float(item.get("locked_until") or 0)
        if count >= LOGIN_FAILURE_LIMIT:
            locked_until = now + LOGIN_FAILURE_LOCK_SECONDS
        items[key] = {
            "first_seen": first_seen,
            "last_seen": now,
            "count": count,
            "locked_until": locked_until,
        }
        AuthManager._cap_login_failure_items(items, preserve_key=key)
        return max(int(locked_until - now), 0)

    def record_login_failure(self, key: str) -> int:
        now = china_now().timestamp()
        clean_key = str(key or "")
        result = {"retry_after": 0}

        def record(payload):
            items = payload.get("items") if isinstance(payload.get("items"), dict) else {}
            self._prune_login_failure_items(items, now)
            result["retry_after"] = self._record_login_failure_item(items, clean_key, now)
            payload["items"] = items
            return payload

        try:
            update_database_json_state(LOGIN_FAILURE_STATE_KEY, {"items": {}}, record)
            with self._login_failure_lock:
                self._login_failures.pop(clean_key, None)
            return result["retry_after"]
        except DatabaseUnavailable:
            pass
        with self._login_failure_lock:
            self._prune_login_failure_items(self._login_failures, now)
            return self._record_login_failure_item(self._login_failures, clean_key, now)

    def clear_login_failures(self, key: str) -> None:
        clean_key = str(key or "")

        def clear(payload):
            items = payload.get("items") if isinstance(payload.get("items"), dict) else {}
            items.pop(clean_key, None)
            payload["items"] = items
            return payload

        try:
            update_database_json_state(LOGIN_FAILURE_STATE_KEY, {"items": {}}, clear)
        except DatabaseUnavailable:
            pass
        with self._login_failure_lock:
            self._login_failures.pop(clean_key, None)

    def create_session(self, username: str) -> dict:
        now = china_now()
        session = {
            "id": generate_session_id(),
            "username": username,
            "created_at": now.isoformat(),
            "last_seen_at": now.isoformat(),
            "expires_at": (now + timedelta(days=SESSION_TTL_DAYS)).isoformat(),
        }

        def create(payload):
            payload = self._prune_sessions(payload)
            payload["items"].append(dict(session))
            payload["generation"] = int(payload.get("generation") or 0) + 1
            return payload

        self._update_sessions(create)
        return session

    def delete_session(self, session_id: str) -> None:
        clean_session_id = str(session_id or "")

        def delete(payload):
            items = payload.get("items") if isinstance(payload.get("items"), list) else []
            remaining = [item for item in items if str(item.get("id") or "") != clean_session_id]
            if len(remaining) != len(items):
                payload["generation"] = int(payload.get("generation") or 0) + 1
            payload["items"] = remaining
            return payload

        self._update_sessions(delete)
        self._clear_session_cache(session_id)

    def clear_all_sessions(self) -> None:
        def clear(payload):
            payload["items"] = []
            payload["generation"] = int(payload.get("generation") or 0) + 1
            return payload

        self._update_sessions(clear)
        self._clear_session_cache()

    def get_session(self, session_id: str) -> Optional[dict]:
        if not session_id:
            return None
        payload = self._prune_sessions(self._read_sessions())
        generation = int(payload.get("generation") or 0)
        cached = self._cached_session(session_id, generation)
        if cached:
            return cached
        target = None
        for item in payload.get("items") or []:
            if str(item.get("id") or "") == session_id:
                now = china_now()
                last_seen = str(item.get("last_seen_at") or "")
                should_touch = True
                if last_seen:
                    last_seen_dt = parse_datetime(last_seen)
                    if last_seen_dt is None:
                        should_touch = True
                    else:
                        should_touch = (now - last_seen_dt).total_seconds() >= 300
                if should_touch:
                    touched = {"session": None}

                    def touch(latest):
                        latest = self._prune_sessions(latest)
                        for current in latest.get("items") or []:
                            if str(current.get("id") or "") == session_id:
                                current["last_seen_at"] = now.isoformat()
                                latest["generation"] = int(latest.get("generation") or 0) + 1
                                touched["session"] = dict(current)
                                break
                        return latest

                    updated = self._update_sessions(touch)
                    target = touched["session"]
                    generation = int(updated.get("generation") or 0)
                    if target is None:
                        return None
                    break
                target = item
                break
        if target:
            self._cache_session(session_id, target, generation)
        return target

    def _token_view(self, item: ApiTokenRecord, *, now=None, token_value: str = "") -> ApiTokenView:
        return ApiTokenView(
            id=item.id,
            name=item.name,
            token_prefix=item.token_prefix,
            token_value=token_value,
            created_at=item.created_at,
            permissions=normalize_token_permissions(item.permissions),
            expires_at=item.expires_at,
            last_used_at=item.last_used_at,
            disabled=item.disabled,
            revoked_at=item.revoked_at,
            status=token_status(item, now=now),
        )

    def list_api_tokens(self) -> list[ApiTokenView]:
        config = self.store.load()
        if any(item.legacy_token_value_present for item in config.api_tokens):
            config = self.store.update(lambda current: current)
        items = sorted(config.api_tokens, key=lambda item: item.created_at, reverse=True)
        now = china_now()
        return [self._token_view(item, now=now) for item in items]

    def create_api_token(
        self,
        name: str,
        *,
        permissions: Optional[Iterable[str]] = None,
        expires_days: int = 0,
        token_prefix: str = "mht",
        config_mutator: Optional[Callable[[AppConfig, ApiTokenRecord], None]] = None,
    ) -> tuple[str, ApiTokenView]:
        raw_token = generate_api_token(prefix=token_prefix)
        now = china_now_iso()
        requested_name = str(name or "").strip()
        safe_expires_days = max(0, min(int(expires_days or 0), 3650))
        expires_at = (china_now() + timedelta(days=safe_expires_days)).isoformat() if safe_expires_days else ""
        token_id = generate_session_id()

        def add_token(config):
            record = ApiTokenRecord(
                id=token_id,
                name=requested_name or f"Token {len(config.api_tokens) + 1}",
                token_prefix=raw_token[:12],
                token_hash=hash_api_token(raw_token),
                created_at=now,
                permissions=normalize_token_permissions(permissions),
                expires_at=expires_at,
            )
            config.api_tokens.insert(0, record)
            if config_mutator is not None:
                config_mutator(config, record)
        saved = self.store.update(add_token)
        record = next(item for item in saved.api_tokens if item.id == token_id)
        return raw_token, self._token_view(record, token_value=raw_token)

    def revoke_api_token(self, token_id: str) -> list[ApiTokenView]:
        now = china_now_iso()

        def revoke(config):
            for item in config.api_tokens:
                if item.id == token_id:
                    item.disabled = True
                    item.revoked_at = item.revoked_at or now
                    break

        self.store.update(revoke)
        return self.list_api_tokens()

    def validate_api_token(self, raw_token: str, *, required_permission: str = "") -> Optional[ApiTokenView]:
        token = str(raw_token or "").strip()
        if not token:
            return None

        token_hash = hash_api_token(token)
        now = china_now()
        config = self.store.load()
        candidate_id = ""
        clean_required = str(required_permission or "").strip().lower()
        for item in config.api_tokens:
            if token_status(item, now=now) != "active":
                continue
            if item.token_hash == token_hash:
                permissions = normalize_token_permissions(item.permissions)
                if clean_required and clean_required not in permissions:
                    return None
                candidate_id = item.id
                break

        if not candidate_id:
            return None

        last_used_at = china_now_iso()

        def touch_token(latest):
            for item in latest.api_tokens:
                if item.id != candidate_id or item.token_hash != token_hash:
                    continue
                if token_status(item) != "active":
                    break
                permissions = normalize_token_permissions(item.permissions)
                if clean_required and clean_required not in permissions:
                    break
                item.permissions = permissions
                item.last_used_at = last_used_at
                return

        saved = self.store.update(touch_token)
        matched = next(
            (
                item
                for item in saved.api_tokens
                if item.id == candidate_id
                and item.token_hash == token_hash
                and token_status(item) == "active"
                and (not clean_required or clean_required in normalize_token_permissions(item.permissions))
            ),
            None,
        )
        return self._token_view(matched) if matched is not None else None

    def change_password(self, current_password: str, new_password: str) -> None:
        new_secret = str(new_password or "")
        if len(new_secret) < MIN_ADMIN_PASSWORD_LENGTH:
            raise ValueError(f"新密码至少需要 {MIN_ADMIN_PASSWORD_LENGTH} 个字符。")

        def change(config):
            if not verify_password(current_password, config.user.password_hash):
                raise ValueError("当前密码不正确。")
            config.user.password_hash = hash_password(new_secret)
            config.user.password_updated_at = china_now_iso()

        self.store.update(change)
        try:
            self.clear_all_sessions()
        finally:
            _remove_bootstrap_password()

    def extract_api_token(self, request: Request) -> str:
        auth_header = str(request.headers.get("Authorization") or "").strip()
        if auth_header.lower().startswith("bearer "):
            return auth_header[7:].strip()
        custom = str(request.headers.get("X-API-Token") or request.headers.get("X-Token") or "").strip()
        if custom:
            return custom
        return ""

    def resolve_request_auth(
        self,
        request: Request,
        allow_api_token: bool = False,
        api_token_permission: str = "",
    ) -> Optional[dict]:
        session_id = str(request.cookies.get(SESSION_COOKIE_NAME) or "").strip()
        if session_id:
            session = self.get_session(session_id)
            if session:
                return {
                    "kind": "session",
                    "username": session.get("username") or "",
                    "session_id": session_id,
                }

        if allow_api_token or api_token_permission:
            token = self.extract_api_token(request)
            token_view = self.validate_api_token(token, required_permission=api_token_permission)
            if token_view:
                return {
                    "kind": "token",
                    "username": "",
                    "token": token_view.model_dump(),
                }
        return None


def ensure_secure_admin_credential(store: JsonStore) -> BootstrapCredentialResult:
    bootstrap_path = _bootstrap_password_path()
    current = store.load()
    needs_token_cleanup = any(item.legacy_token_value_present for item in current.api_tokens)
    if not _admin_credential_requires_rotation(current.user.password_hash):
        if needs_token_cleanup:
            store.update(lambda config: config)
        return BootstrapCredentialResult(
            rotated=False,
            source="existing",
            bootstrap_path=bootstrap_path,
        )

    environment_password = str(os.getenv("MAKERHUB_ADMIN_PASSWORD") or "")
    if len(environment_password) >= MIN_ADMIN_PASSWORD_LENGTH:
        password = environment_password
        source = "environment"
    else:
        password = secrets.token_urlsafe(24)
        source = "generated"
        password = _prepare_generated_bootstrap_password(bootstrap_path, password)
    rotation = {"applied": False}

    def rotate(config):
        if not _admin_credential_requires_rotation(config.user.password_hash):
            return
        config.user.password_hash = hash_password(password)
        config.user.password_updated_at = ""
        rotation["applied"] = True

    store.update(rotate)
    if not rotation["applied"]:
        return BootstrapCredentialResult(
            rotated=False,
            source="existing",
            bootstrap_path=bootstrap_path,
        )

    if source == "environment":
        bootstrap_path.unlink(missing_ok=True)
    AuthManager(store=store).clear_all_sessions()
    return BootstrapCredentialResult(
        rotated=True,
        source=source,
        bootstrap_path=bootstrap_path,
    )
