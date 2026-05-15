import json
from datetime import timedelta
from pathlib import Path
from typing import Iterable, Optional

from fastapi import Request

from app.core.security import generate_api_token, generate_session_id, hash_api_token, hash_password, verify_password
from app.core.settings import STATE_DIR, ensure_app_dirs
from app.core.store import JsonStore
from app.core.timezone import now as china_now, now_iso as china_now_iso, parse_datetime
from app.schemas.models import ApiTokenRecord, ApiTokenView


SESSION_COOKIE_NAME = "makerhub_session"
SESSION_TTL_DAYS = 14
SESSIONS_PATH = STATE_DIR / "auth_sessions.json"
TOKEN_PERMISSION_LABELS = {
    "archive_write": "提交归档",
    "mobile_import": "本地导入",
    "models_read": "读取模型库",
    "share_manage": "管理分享",
    "system_manage": "系统管理",
    "token_manage": "Token 管理",
}
DEFAULT_API_TOKEN_PERMISSIONS = ["archive_write"]
TOKEN_PERMISSION_ORDER = list(TOKEN_PERMISSION_LABELS.keys())


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
    def __init__(self, store: Optional[JsonStore] = None, sessions_path: Path = SESSIONS_PATH) -> None:
        self.store = store or JsonStore()
        self.sessions_path = sessions_path
        ensure_app_dirs()

    def _read_sessions(self) -> dict:
        if not self.sessions_path.exists():
            payload = {"items": []}
            self._write_sessions(payload)
            return payload

        try:
            payload = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {"items": []}
            self._write_sessions(payload)
            return payload

        if not isinstance(payload, dict):
            payload = {"items": []}
        payload["items"] = payload.get("items") if isinstance(payload.get("items"), list) else []
        return payload

    def _write_sessions(self, payload: dict) -> None:
        self.sessions_path.parent.mkdir(parents=True, exist_ok=True)
        self.sessions_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
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

    def create_session(self, username: str) -> dict:
        payload = self._prune_sessions(self._read_sessions())
        now = china_now()
        session = {
            "id": generate_session_id(),
            "username": username,
            "created_at": now.isoformat(),
            "last_seen_at": now.isoformat(),
            "expires_at": (now + timedelta(days=SESSION_TTL_DAYS)).isoformat(),
        }
        payload["items"].append(session)
        self._write_sessions(payload)
        return session

    def delete_session(self, session_id: str) -> None:
        payload = self._read_sessions()
        payload["items"] = [item for item in payload.get("items") or [] if str(item.get("id") or "") != session_id]
        self._write_sessions(payload)

    def clear_all_sessions(self) -> None:
        self._write_sessions({"items": []})

    def get_session(self, session_id: str) -> Optional[dict]:
        if not session_id:
            return None
        payload = self._prune_sessions(self._read_sessions())
        target = None
        changed = False
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
                    item["last_seen_at"] = now.isoformat()
                    changed = True
                target = item
                break
        if changed:
            self._write_sessions(payload)
        return target

    def _token_view(self, item: ApiTokenRecord, *, now=None) -> ApiTokenView:
        return ApiTokenView(
            id=item.id,
            name=item.name,
            token_prefix=item.token_prefix,
            token_value=item.token_value,
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
    ) -> tuple[str, ApiTokenView]:
        config = self.store.load()
        raw_token = generate_api_token(prefix=token_prefix)
        now = china_now_iso()
        display_name = str(name or "").strip() or f"Token {len(config.api_tokens) + 1}"
        safe_expires_days = max(0, min(int(expires_days or 0), 3650))
        expires_at = (china_now() + timedelta(days=safe_expires_days)).isoformat() if safe_expires_days else ""
        record = ApiTokenRecord(
            id=generate_session_id(),
            name=display_name,
            token_prefix=raw_token[:12],
            token_hash=hash_api_token(raw_token),
            token_value=raw_token,
            created_at=now,
            permissions=normalize_token_permissions(permissions),
            expires_at=expires_at,
        )
        config.api_tokens.insert(0, record)
        self.store.save(config)
        return raw_token, self._token_view(record)

    def revoke_api_token(self, token_id: str) -> list[ApiTokenView]:
        config = self.store.load()
        now = china_now_iso()
        for item in config.api_tokens:
            if item.id == token_id:
                item.disabled = True
                item.revoked_at = item.revoked_at or now
                break
        self.store.save(config)
        return self.list_api_tokens()

    def validate_api_token(self, raw_token: str, *, required_permission: str = "") -> Optional[ApiTokenView]:
        token = str(raw_token or "").strip()
        if not token:
            return None

        token_hash = hash_api_token(token)
        config = self.store.load()
        matched = None
        now = china_now()
        for item in config.api_tokens:
            if token_status(item, now=now) != "active":
                continue
            if item.token_hash == token_hash:
                permissions = normalize_token_permissions(item.permissions)
                clean_required = str(required_permission or "").strip().lower()
                if clean_required and clean_required not in permissions:
                    return None
                item.permissions = permissions
                item.last_used_at = china_now_iso()
                matched = item
                break

        if matched is None:
            return None

        self.store.save(config)
        return self._token_view(matched)

    def change_password(self, current_password: str, new_password: str) -> None:
        config = self.store.load()
        if not verify_password(current_password, config.user.password_hash):
            raise ValueError("当前密码不正确。")
        new_secret = str(new_password or "")
        if len(new_secret) < 4:
            raise ValueError("新密码至少需要 4 个字符。")
        config.user.password_hash = hash_password(new_secret)
        config.user.password_updated_at = china_now_iso()
        self.store.save(config)
        self.clear_all_sessions()

    def extract_api_token(self, request: Request) -> str:
        auth_header = str(request.headers.get("Authorization") or "").strip()
        if auth_header.lower().startswith("bearer "):
            return auth_header[7:].strip()
        custom = str(request.headers.get("X-API-Token") or request.headers.get("X-Token") or "").strip()
        if custom:
            return custom
        return str(request.query_params.get("token") or "").strip()

    def resolve_request_auth(self, request: Request, allow_api_token: bool = False) -> Optional[dict]:
        session_id = str(request.cookies.get(SESSION_COOKIE_NAME) or "").strip()
        if session_id:
            session = self.get_session(session_id)
            if session:
                return {
                    "kind": "session",
                    "username": session.get("username") or "",
                    "session_id": session_id,
                }

        if allow_api_token:
            token = self.extract_api_token(request)
            token_view = self.validate_api_token(token, required_permission="archive_write")
            if token_view:
                return {
                    "kind": "token",
                    "username": "",
                    "token": token_view.model_dump(),
                }
        return None
