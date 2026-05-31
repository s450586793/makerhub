from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from app.core.database_json_state import load_database_json_state, save_database_json_state
from app.core.settings import STATE_DIR
from app.core.store import JsonStore
from app.core.timezone import now_iso as china_now_iso
from app.services.batch_discovery import extract_model_id, normalize_model_url, normalize_source_url
from app.services.business_logs import append_business_log
from app.services.cookie_utils import extract_auth_token, parse_cookie_values, sanitize_cookie_header
from app.services.proxy_policy import proxy_url
from app.services.state_events import publish_state_event
from app.services.three_mf import normalize_makerworld_source, normalize_three_mf_failure_state


BROWSER_VERIFICATION_STATE_KEY = "browser_verification_sessions"
BROWSER_VERIFICATION_DIR = STATE_DIR / "browser_verification"
BROWSER_VERIFICATION_SCREENSHOT_DIR = BROWSER_VERIFICATION_DIR / "screenshots"
BROWSER_VERIFICATION_PROFILE_DIR = BROWSER_VERIFICATION_DIR / "profiles"
BROWSER_VERIFICATION_INPUT_LIMIT = 100
BROWSER_VERIFICATION_SESSION_LIMIT = 20
BROWSER_VERIFICATION_DEFAULT_VIEWPORT = {"width": 1024, "height": 720}
VERIFICATION_RETRY_STATES = {"verification_required", "cloudflare", "auth_required"}
BROWSER_VERIFICATION_ACTIVE_STATES = {"queued", "starting", "running", "verified", "retrying"}
SENSITIVE_REQUEST_HEADER_KEYS = {
    "x-bbl-captcha-result",
    "cookie",
    "authorization",
    "token",
    "x-token",
    "x-access-token",
}
SENSITIVE_LOG_KEYS = SENSITIVE_REQUEST_HEADER_KEYS | {
    "cf_clearance",
    "__cf_bm",
    "proof",
    "proof_id",
    "browser_verification_proof_id",
}


def _redact_browser_verification_value(key: str, value: Any) -> Any:
    lowered = str(key or "").lower()
    if any(secret_key in lowered for secret_key in SENSITIVE_LOG_KEYS):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            str(item_key): _redact_browser_verification_value(str(item_key), item_value)
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_browser_verification_value(lowered, item) for item in value]
    return value


def _browser_verification_log(event: str, message: str, **fields: Any) -> None:
    safe_fields = {
        str(key): _redact_browser_verification_value(str(key), value)
        for key, value in fields.items()
    }
    append_business_log("missing_3mf", event, message, **safe_fields)


def _empty_state() -> dict[str, Any]:
    return {"items": [], "updated_at": ""}


def _now() -> str:
    return china_now_iso()


def _safe_text(value: Any, limit: int = 400) -> str:
    return str(value or "").strip()[:limit]


def _session_id() -> str:
    return f"bv_{uuid.uuid4().hex}"


def _platform_from_item(item: dict[str, Any]) -> str:
    raw_source = item.get("source") or item.get("platform") or ""
    model_url = str(item.get("model_url") or item.get("url") or "")
    api_url = str(item.get("api_url") or item.get("apiUrl") or "")
    return (
        normalize_makerworld_source(raw_source, model_url)
        or normalize_makerworld_source(raw_source, api_url)
        or normalize_makerworld_source(url=model_url)
        or normalize_makerworld_source(url=api_url)
        or "cn"
    )


def _default_model_url(model_id: str, platform: str) -> str:
    if not model_id:
        return ""
    host = "https://makerworld.com" if platform == "global" else "https://makerworld.com.cn"
    return normalize_model_url(f"{host}/zh/models/{model_id}")


def _safe_target(raw_item: dict[str, Any]) -> dict[str, str]:
    item = raw_item if isinstance(raw_item, dict) else {}
    platform = _platform_from_item(item)
    model_url = normalize_source_url(str(item.get("model_url") or item.get("url") or ""))
    model_id = str(item.get("model_id") or item.get("id") or extract_model_id(model_url) or "").strip()
    if not model_url:
        model_url = _default_model_url(model_id, platform)
    return {
        "model_id": model_id,
        "model_url": model_url,
        "title": _safe_text(item.get("title") or item.get("name") or "", 240),
        "instance_id": _safe_text(item.get("instance_id") or item.get("profileId") or item.get("instanceId") or "", 120),
        "api_url": normalize_source_url(str(item.get("api_url") or item.get("apiUrl") or "")),
    }


def _safe_captcha_id(raw_item: dict[str, Any]) -> str:
    item = raw_item if isinstance(raw_item, dict) else {}
    verification = item.get("verification") if isinstance(item.get("verification"), dict) else {}
    return _safe_text(
        item.get("captcha_id")
        or item.get("captchaId")
        or verification.get("captcha_id")
        or verification.get("captchaId")
        or "",
        180,
    )


def _normalize_status(value: Any) -> str:
    status = str(value or "").strip()
    if status in {"queued", "starting", "running", "verified", "retrying", "completed", "failed", "cancelled", "expired"}:
        return status
    return "queued"


def _normalize_session(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    target = raw.get("target") if isinstance(raw.get("target"), dict) else {}
    platform = str(raw.get("platform") or _platform_from_item(target) or "cn").strip()
    session = {
        "id": str(raw.get("id") or "").strip(),
        "status": _normalize_status(raw.get("status")),
        "platform": "global" if platform == "global" else "cn",
        "target": _safe_target({**target, "source": platform}),
        "captcha_id": _safe_text(raw.get("captcha_id") or target.get("captcha_id") or "", 180),
        "message": _safe_text(raw.get("message") or "", 400),
        "error": _safe_text(raw.get("error") or "", 400),
        "created_at": str(raw.get("created_at") or ""),
        "updated_at": str(raw.get("updated_at") or ""),
        "started_at": str(raw.get("started_at") or ""),
        "finished_at": str(raw.get("finished_at") or ""),
        "proof_id": str(raw.get("proof_id") or "").strip(),
        "proof_captured": bool(raw.get("proof_captured")),
        "retry_result": raw.get("retry_result") if isinstance(raw.get("retry_result"), dict) else {},
        "screenshot_version": int(raw.get("screenshot_version") or 0),
        "viewport": raw.get("viewport") if isinstance(raw.get("viewport"), dict) else dict(BROWSER_VERIFICATION_DEFAULT_VIEWPORT),
        "input_seq": int(raw.get("input_seq") or 0),
        "commands": [],
    }
    commands = raw.get("commands") if isinstance(raw.get("commands"), list) else []
    session["commands"] = [_normalize_input_command(item) for item in commands if _normalize_input_command(item)]
    return session


def _public_session(session: dict[str, Any]) -> dict[str, Any]:
    payload = dict(session)
    payload.pop("commands", None)
    return payload


def _normalize_input_command(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    command_type = str(raw.get("type") or "").strip().lower()
    if command_type not in {"click", "mousemove", "mousedown", "mouseup", "wheel", "key", "text"}:
        return {}
    command = {
        "id": str(raw.get("id") or f"cmd_{uuid.uuid4().hex}"),
        "type": command_type,
        "created_at": str(raw.get("created_at") or _now()),
    }
    for key in ("x", "y", "delta_x", "delta_y"):
        if key in raw:
            try:
                command[key] = int(float(raw.get(key) or 0))
            except (TypeError, ValueError):
                command[key] = 0
    if command_type == "key":
        key_value = str(raw.get("key") or "").strip()
        if key_value:
            command["key"] = key_value[:80]
    elif command_type == "text":
        text_value = str(raw.get("text") or "")
        if text_value:
            command["text"] = text_value[:200]
    return command


class BrowserVerificationProofStore:
    def __init__(self, ttl_seconds: int = 15 * 60) -> None:
        self.ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self._items: dict[str, tuple[str, float]] = {}

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def store(self, proof: str) -> str:
        clean = str(proof or "").strip()
        if not clean:
            return ""
        proof_id = f"proof_{secrets.token_urlsafe(18)}"
        with self._lock:
            self._prune_locked()
            self._items[proof_id] = (clean, time.time() + self.ttl_seconds)
        return proof_id

    def pop(self, proof_id: str) -> str:
        clean_id = str(proof_id or "").strip()
        if not clean_id:
            return ""
        with self._lock:
            self._prune_locked()
            item = self._items.pop(clean_id, None)
        if not item:
            return ""
        return item[0]

    def peek(self, proof_id: str) -> str:
        clean_id = str(proof_id or "").strip()
        if not clean_id:
            return ""
        with self._lock:
            self._prune_locked()
            item = self._items.get(clean_id)
        return item[0] if item else ""

    def _prune_locked(self) -> None:
        now = time.time()
        expired = [key for key, (_value, expires_at) in self._items.items() if expires_at <= now]
        for key in expired:
            self._items.pop(key, None)


proof_store = BrowserVerificationProofStore()


class BrowserVerificationStore:
    def load_state(self) -> dict[str, Any]:
        payload = load_database_json_state(BROWSER_VERIFICATION_STATE_KEY, _empty_state())
        items = [_normalize_session(item) for item in payload.get("items") or []]
        items = [item for item in items if item.get("id")]
        return {"items": items, "updated_at": str(payload.get("updated_at") or "")}

    def save_state(self, state: dict[str, Any]) -> dict[str, Any]:
        items = [_normalize_session(item) for item in state.get("items") or []]
        items = [item for item in items if item.get("id")]
        items = items[:BROWSER_VERIFICATION_SESSION_LIMIT]
        payload = {"items": items, "updated_at": _now()}
        saved = save_database_json_state(BROWSER_VERIFICATION_STATE_KEY, payload)
        publish_state_event("browser_verification", payload={"count": len(items), "updated_at": payload["updated_at"]})
        return saved

    def list_sessions(self) -> list[dict[str, Any]]:
        return [_public_session(item) for item in self.load_state().get("items") or []]

    def get_session(self, session_id: str) -> dict[str, Any]:
        clean_id = str(session_id or "").strip()
        for item in self.load_state().get("items") or []:
            if item.get("id") == clean_id:
                return _public_session(item)
        return {}

    def create_session(self, raw_item: dict[str, Any]) -> dict[str, Any]:
        item = raw_item if isinstance(raw_item, dict) else {}
        target = _safe_target(item)
        platform = _platform_from_item({**item, **target})
        state = self.load_state()
        for existing in state.get("items") or []:
            if existing.get("platform") == platform and existing.get("status") in BROWSER_VERIFICATION_ACTIVE_STATES:
                if existing.get("status") in {"queued", "starting"}:
                    updated = self.update_session(
                        existing.get("id") or "",
                        message="同平台已有验证会话，请先完成或取消已有验证会话。",
                    )
                else:
                    updated = _public_session(existing)
                if updated:
                    return updated
        now = _now()
        session = _normalize_session(
            {
                "id": _session_id(),
                "status": "queued",
                "platform": platform,
                "target": target,
                "captcha_id": _safe_captcha_id(item),
                "message": "等待 worker 打开验证浏览器。",
                "created_at": now,
                "updated_at": now,
                "viewport": dict(BROWSER_VERIFICATION_DEFAULT_VIEWPORT),
            }
        )
        items = [entry for entry in state.get("items") or [] if entry.get("id") != session["id"]]
        state["items"] = [session, *items]
        self.save_state(state)
        append_business_log(
            "missing_3mf",
            "browser_verification_session_created",
            "已创建 3MF 浏览器验证会话。",
            session_id=session["id"],
            platform=session["platform"],
            model_id=target.get("model_id") or "",
            model_url=target.get("model_url") or "",
            instance_id=target.get("instance_id") or "",
        )
        return _public_session(session)

    def update_session(self, session_id: str, **changes: Any) -> dict[str, Any]:
        clean_id = str(session_id or "").strip()
        state = self.load_state()
        updated: dict[str, Any] = {}
        items = []
        for item in state.get("items") or []:
            if item.get("id") == clean_id:
                next_item = dict(item)
                for key, value in changes.items():
                    if value is not None:
                        next_item[key] = value
                next_item["updated_at"] = _now()
                updated = _normalize_session(next_item)
                items.append(updated)
            else:
                items.append(item)
        if not updated:
            return {}
        state["items"] = items
        self.save_state(state)
        return _public_session(updated)

    def cancel_session(self, session_id: str, message: str = "验证已取消。") -> dict[str, Any]:
        return self.update_session(session_id, status="cancelled", message=message, finished_at=_now())

    def active_or_queued_sessions(self) -> list[dict[str, Any]]:
        return [
            item
            for item in self.load_state().get("items") or []
            if item.get("status") in BROWSER_VERIFICATION_ACTIVE_STATES
        ]

    def next_queued_session(self) -> dict[str, Any]:
        for item in self.load_state().get("items") or []:
            if item.get("status") == "queued":
                return item
        return {}

    def enqueue_input(self, session_id: str, raw_command: dict[str, Any]) -> dict[str, Any]:
        command = _normalize_input_command(raw_command)
        if not command:
            return {}
        clean_id = str(session_id or "").strip()
        state = self.load_state()
        saved_command: dict[str, Any] = {}
        items = []
        for item in state.get("items") or []:
            if item.get("id") == clean_id and item.get("status") in {"running", "verified", "retrying"}:
                next_item = dict(item)
                seq = int(next_item.get("input_seq") or 0) + 1
                command["seq"] = seq
                next_item["input_seq"] = seq
                commands = list(next_item.get("commands") or [])
                commands.append(command)
                next_item["commands"] = commands[-BROWSER_VERIFICATION_INPUT_LIMIT:]
                next_item["updated_at"] = _now()
                saved_command = command
                items.append(_normalize_session(next_item))
            else:
                items.append(item)
        if not saved_command:
            return {}
        state["items"] = items
        self.save_state(state)
        return dict(saved_command)

    def consume_input_commands(self, session_id: str) -> list[dict[str, Any]]:
        clean_id = str(session_id or "").strip()
        state = self.load_state()
        consumed: list[dict[str, Any]] = []
        items = []
        for item in state.get("items") or []:
            if item.get("id") == clean_id:
                consumed = list(item.get("commands") or [])
                next_item = dict(item)
                next_item["commands"] = []
                items.append(_normalize_session(next_item))
            else:
                items.append(item)
        if consumed:
            state["items"] = items
            self.save_state(state)
        return consumed

    def screenshot_path(self, session_id: str) -> Path:
        clean_id = str(session_id or "").strip()
        if not clean_id:
            clean_id = "unknown"
        return BROWSER_VERIFICATION_SCREENSHOT_DIR / f"{clean_id}.jpg"

    def read_screenshot(self, session_id: str) -> tuple[bytes, str]:
        path = self.screenshot_path(session_id)
        try:
            return path.read_bytes(), "image/jpeg"
        except OSError:
            return b"", "image/jpeg"


browser_verification_store = BrowserVerificationStore()


def resolve_browser_verification_proof(proof_id: str) -> str:
    return proof_store.peek(proof_id)


def consume_browser_verification_proof(proof_id: str) -> str:
    return proof_store.pop(proof_id)


def _origin_for_platform(platform: str) -> str:
    return "https://makerworld.com" if platform == "global" else "https://makerworld.com.cn"


def _api_origin_for_platform(platform: str) -> str:
    return "https://api.bambulab.com" if platform == "global" else "https://api.bambulab.cn"


def _instance_f3mf_api_url(instance_id: str, platform: str) -> str:
    origin = _api_origin_for_platform(platform)
    return f"{origin}/v1/design-service/instance/{instance_id}/f3mf?type=download&fileType=3mf"


def _profile_key_for_session(session: dict[str, Any]) -> str:
    platform = str(session.get("platform") or "cn").strip()
    return "global" if platform == "global" else "cn"


def _cookie_domains(platform: str) -> tuple[str, ...]:
    if platform == "global":
        return (".makerworld.com", ".api.bambulab.com")
    return (".makerworld.com.cn", ".api.bambulab.cn")


def _cookies_for_context(raw_cookie: str, platform: str) -> list[dict[str, Any]]:
    cookies = []
    for domain in _cookie_domains(platform):
        for name, value in parse_cookie_values(raw_cookie).items():
            cookies.append(
                {
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": "/",
                    "httpOnly": False,
                    "secure": True,
                    "sameSite": "Lax",
                }
            )
    return cookies


def _select_cookie_for_platform(config: Any, platform: str) -> str:
    for item in getattr(config, "cookies", []) or []:
        if str(getattr(item, "platform", "") or "").strip() == platform:
            return sanitize_cookie_header(getattr(item, "cookie", ""))
    return ""


def _auth_headers_for_cookie(raw_cookie: str) -> dict[str, str]:
    auth_token = extract_auth_token(raw_cookie)
    if not auth_token:
        return {}
    return {
        "Authorization": f"Bearer {auth_token}",
        "token": auth_token,
        "X-Token": auth_token,
        "X-Access-Token": auth_token,
    }


def _is_bambu_api_url(url: str) -> bool:
    host = (urlparse(str(url or "")).hostname or "").lower()
    return host in {"api.bambulab.com", "api.bambulab.cn"}


def _bambu_api_auth_headers(url: str, request_headers: Any, auth_headers: dict[str, str]) -> Optional[dict[str, str]]:
    if not auth_headers or not _is_bambu_api_url(url):
        return None
    headers = {str(key): str(value) for key, value in (request_headers or {}).items()}
    headers.update(auth_headers)
    return headers


def _continue_bambu_api_request(route: Any, request: Any, auth_headers: dict[str, str]) -> None:
    headers = _bambu_api_auth_headers(
        str(getattr(request, "url", "") or ""),
        getattr(request, "headers", {}) or {},
        auth_headers,
    )
    if headers is None:
        route.continue_()
        return
    route.continue_(headers=headers)


def _instance_id_from_api_url(api_url: str) -> str:
    path_parts = [part for part in urlparse(api_url).path.split("/") if part]
    for index, part in enumerate(path_parts):
        if part == "instance" and index + 1 < len(path_parts):
            return _safe_text(path_parts[index + 1], 120)
    return ""


def _is_f3mf_url(url: str) -> bool:
    return "/f3mf" in urlparse(str(url or "")).path


def _looks_like_api_denial_text(text: str) -> bool:
    clean = str(text or "").strip()
    if not clean:
        return False
    lower = clean.lower()
    parsed: Any = None
    if clean.startswith("{") or clean.startswith("["):
        try:
            parsed = json.loads(clean)
        except (TypeError, ValueError):
            parsed = None
    if isinstance(parsed, dict):
        code = str(parsed.get("code") or parsed.get("status") or "").strip()
        error_text = " ".join(str(parsed.get(key) or "") for key in ("error", "message", "msg", "detail")).lower()
        return code == "403" or (
            "403" in error_text
            and any(marker in error_text for marker in ("access", "permission", "right", "forbidden", "denied"))
        ) or any(marker in error_text for marker in ("access rights", "permission denied", "forbidden"))
    if "<html" in lower or "<body" in lower or "<button" in lower:
        return False
    return "403" in lower and any(
        marker in lower
        for marker in ("access rights", "permission", "forbidden", "denied", "does not have access")
    )


def _page_looks_like_api_denial(page: Any) -> bool:
    try:
        body_text = page.text_content("body", timeout=800)
    except Exception:
        return False
    return _looks_like_api_denial_text(str(body_text or ""))


def _browser_verification_fallback_url(session: dict[str, Any]) -> str:
    target = session.get("target") if isinstance(session.get("target"), dict) else {}
    model_url = normalize_source_url(str(target.get("model_url") or ""))
    if model_url:
        return model_url
    platform = str(session.get("platform") or _platform_from_item(target) or "cn")
    return _origin_for_platform(platform)


def _fallback_from_api_denial_if_needed(page: Any, session: dict[str, Any], session_id: str = "") -> bool:
    if not _page_looks_like_api_denial(page):
        return False
    fallback_url = _browser_verification_fallback_url(session)
    if not fallback_url:
        return False
    page.goto(fallback_url, wait_until="domcontentloaded", timeout=45000)
    if session_id:
        _browser_verification_log(
            "browser_verification_api_denial_fallback_applied",
            "浏览器验证检测到 API 权限页，已切换到模型页面。",
            session_id=session_id,
            platform=str(session.get("platform") or ""),
            fallback_url=fallback_url,
        )
    return True


def _try_trigger_download_flow(page: Any) -> bool:
    selectors = (
        'button:has-text("Download 3MF")',
        'a:has-text("Download 3MF")',
        '[role="button"]:has-text("Download 3MF")',
        '[role="menuitem"]:has-text("Download 3MF")',
        'button:has-text("下载 3MF")',
        'a:has-text("下载 3MF")',
        '[role="button"]:has-text("下载 3MF")',
        '[role="menuitem"]:has-text("下载 3MF")',
        'button:has-text("Download")',
        'a:has-text("Download")',
        '[role="button"]:has-text("Download")',
        '[role="menuitem"]:has-text("Download")',
        'button:has-text("下载")',
        'a:has-text("下载")',
        '[role="button"]:has-text("下载")',
        '[role="menuitem"]:has-text("下载")',
    )
    for selector in selectors:
        try:
            locator = page.locator(selector).first()
            if not locator.is_visible(timeout=700):
                continue
            locator.click(timeout=1500)
            return True
        except Exception:
            continue
    return False


def _verification_start_url(session: dict[str, Any]) -> str:
    target = session.get("target") if isinstance(session.get("target"), dict) else {}
    api_url = normalize_source_url(str(target.get("api_url") or ""))
    platform = str(session.get("platform") or _platform_from_item(target) or "cn")
    instance_id = _safe_text(target.get("instance_id") or _instance_id_from_api_url(api_url), 120)
    if api_url and _is_f3mf_url(api_url):
        return api_url
    model_url = normalize_source_url(str(target.get("model_url") or ""))
    if model_url:
        return model_url
    if instance_id:
        return _instance_f3mf_api_url(instance_id, platform)
    return _origin_for_platform(platform)


@dataclass
class BrowserVerificationRuntime:
    store: BrowserVerificationStore = browser_verification_store
    archive_manager: Any = None
    json_store: Any = None

    def __post_init__(self) -> None:
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._thread_profile_keys: dict[str, str] = {}

    def poll_once(self) -> dict[str, Any]:
        started = 0
        with self._lock:
            live_ids = {key for key, thread in self._threads.items() if thread.is_alive()}
            self._threads = {key: thread for key, thread in self._threads.items() if key in live_ids}
            self._thread_profile_keys = {
                key: profile_key
                for key, profile_key in self._thread_profile_keys.items()
                if key in live_ids
            }
            busy_profile_keys = set(self._thread_profile_keys.values())
            for session in self.store.active_or_queued_sessions():
                session_id = str(session.get("id") or "")
                if not session_id or session_id in self._threads:
                    continue
                if session.get("status") in {"queued", "starting", "running"}:
                    profile_key = _profile_key_for_session(session)
                    if profile_key in busy_profile_keys:
                        if session.get("status") == "queued":
                            self.store.update_session(
                                session_id,
                                status="queued",
                                message="同平台验证浏览器正在使用中，请先完成或取消已有验证会话。",
                            )
                        continue
                    thread = threading.Thread(
                        target=self._run_session_guarded,
                        args=(session_id,),
                        name=f"makerhub-browser-verification-{session_id[:10]}",
                        daemon=True,
                    )
                    self._threads[session_id] = thread
                    self._thread_profile_keys[session_id] = profile_key
                    busy_profile_keys.add(profile_key)
                    thread.start()
                    target = session.get("target") if isinstance(session.get("target"), dict) else {}
                    _browser_verification_log(
                        "browser_verification_session_worker_started",
                        "浏览器验证 worker 已接收会话。",
                        session_id=session_id,
                        platform=str(session.get("platform") or ""),
                        model_id=str(target.get("model_id") or ""),
                    )
                    started += 1
        return {"started": started, "running": len(self._threads)}

    def _run_session_guarded(self, session_id: str) -> None:
        try:
            self._run_session(session_id)
        except Exception as exc:
            self.store.update_session(
                session_id,
                status="failed",
                error=str(exc),
                message="浏览器验证会话失败。",
                finished_at=_now(),
            )
            _browser_verification_log(
                "browser_verification_session_failed",
                "浏览器验证会话失败。",
                level="warning",
                session_id=session_id,
                error=str(exc),
            )

    def _run_session(self, session_id: str) -> None:
        session = self.store.get_session(session_id)
        if not session or session.get("status") == "cancelled":
            return
        self.store.update_session(session_id, status="starting", message="正在启动验证浏览器。", started_at=_now())
        config = (self.json_store or JsonStore()).load()
        platform = str(session.get("platform") or "cn")
        raw_cookie = _select_cookie_for_platform(config, platform)
        if not raw_cookie:
            self.store.update_session(
                session_id,
                status="failed",
                error="未找到对应平台 Cookie。",
                message="请先在设置页登录对应平台账号。",
                finished_at=_now(),
            )
            return

        try:
            start_url = _verification_start_url(session)
        except ValueError as exc:
            self.store.update_session(
                session_id,
                status="failed",
                error=str(exc),
                message="验证会话无法启动。",
                finished_at=_now(),
            )
            return

        self._ensure_browser_dirs()
        self._ensure_virtual_display()
        context = None
        try:
            context = self._launch_context(session, config)
            _browser_verification_log(
                "browser_verification_context_launched",
                "浏览器验证 CloakBrowser 上下文已启动。",
                session_id=session_id,
                platform=platform,
            )
            cookies = _cookies_for_context(raw_cookie, platform)
            if cookies:
                context.add_cookies(cookies)
            page = context.pages[0] if getattr(context, "pages", None) else context.new_page()
            auth_headers = _auth_headers_for_cookie(raw_cookie)
            if auth_headers and hasattr(page, "route"):
                page.route("**/*", lambda route, request: _continue_bambu_api_request(route, request, auth_headers))
            page.set_viewport_size(dict(BROWSER_VERIFICATION_DEFAULT_VIEWPORT))
            captured_proof = {"id": ""}

            def _capture_request(request) -> None:
                try:
                    url = str(request.url or "")
                    if "/f3mf" not in url:
                        return
                    headers = {str(key).lower(): value for key, value in (request.headers or {}).items()}
                    proof = str(headers.get("x-bbl-captcha-result") or "").strip()
                    if not proof or captured_proof.get("id"):
                        return
                    proof_id = proof_store.store(proof)
                    captured_proof["id"] = proof_id
                    _browser_verification_log(
                        "browser_verification_proof_captured",
                        "已捕获浏览器验证结果。",
                        session_id=session_id,
                        platform=platform,
                        proof_id=proof_id,
                    )
                    self.store.update_session(
                        session_id,
                        status="verified",
                        proof_id=proof_id,
                        proof_captured=True,
                        message="已捕获验证结果，正在重试 3MF 下载。",
                    )
                except Exception:
                    return

            page.on("request", _capture_request)
            running_message = (
                "验证浏览器已打开，请在画面中点击下载 3MF 并完成验证。"
                if "/f3mf" not in urlparse(start_url).path
                else "验证浏览器已打开，请在画面中完成验证。"
            )
            self.store.update_session(session_id, status="running", message=running_message)
            page.goto(start_url, wait_until="domcontentloaded", timeout=45000)
            _browser_verification_log(
                "browser_verification_start_url_loaded",
                "浏览器验证起始页面已加载。",
                session_id=session_id,
                platform=platform,
                start_url=start_url,
            )
            fell_back_to_web = False
            try:
                fell_back_to_web = _fallback_from_api_denial_if_needed(page, session, session_id=session_id)
            except Exception:
                fell_back_to_web = False
            if fell_back_to_web or not _is_f3mf_url(start_url):
                try:
                    triggered = _try_trigger_download_flow(page)
                except Exception as exc:
                    _browser_verification_log(
                        "browser_verification_download_trigger_failed",
                        "浏览器验证自动触发下载失败，等待用户手动操作。",
                        level="warning",
                        session_id=session_id,
                        platform=platform,
                        error=str(exc),
                    )
                else:
                    _browser_verification_log(
                        "browser_verification_download_trigger_attempted",
                        "浏览器验证已尝试自动触发下载。",
                        session_id=session_id,
                        platform=platform,
                        triggered=triggered,
                    )
            deadline = time.time() + 15 * 60
            while time.time() < deadline:
                current = self.store.get_session(session_id)
                if not current or current.get("status") in {"cancelled", "completed", "failed"}:
                    return
                for command in self.store.consume_input_commands(session_id):
                    self._apply_input(page, command, current)
                self._write_screenshot(page, session_id)
                proof_id = captured_proof.get("id") or current.get("proof_id") or ""
                if proof_id:
                    retry_result = self._retry_after_verification(current, proof_id)
                    _browser_verification_log(
                        "browser_verification_retry_submitted",
                        "验证完成后已提交缺失 3MF 重试。",
                        session_id=session_id,
                        platform=platform,
                        retry_result=retry_result,
                    )
                    self.store.update_session(
                        session_id,
                        status="completed",
                        message="验证已完成，重试任务已提交。",
                        retry_result=retry_result,
                        finished_at=_now(),
                    )
                    _browser_verification_log(
                        "browser_verification_session_completed",
                        "浏览器验证会话已完成。",
                        session_id=session_id,
                        platform=platform,
                    )
                    return
                time.sleep(1.5)
            self.store.update_session(
                session_id,
                status="expired",
                message="验证会话已超时。",
                finished_at=_now(),
            )
            _browser_verification_log(
                "browser_verification_session_expired",
                "浏览器验证会话已超时。",
                level="warning",
                session_id=session_id,
                platform=platform,
            )
        finally:
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass

    def _launch_context(self, session: dict[str, Any], config: Any):
        from cloakbrowser import launch_persistent_context

        platform = str(session.get("platform") or "cn")
        target = session.get("target") if isinstance(session.get("target"), dict) else {}
        target_url = target.get("model_url") or _origin_for_platform(platform)
        proxy = proxy_url(getattr(config, "proxy", None), target_url, platform=platform)
        user_data_dir = BROWSER_VERIFICATION_PROFILE_DIR / platform
        user_data_dir.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {
            "headless": False,
            "humanize": True,
            "viewport": dict(BROWSER_VERIFICATION_DEFAULT_VIEWPORT),
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-component-update",
                "--disable-extensions",
                "--disable-sync",
                "--metrics-recording-only",
            ],
        }
        if proxy:
            kwargs["proxy"] = proxy
        return launch_persistent_context(str(user_data_dir), **kwargs)

    def _retry_after_verification(self, session: dict[str, Any], proof_id: str) -> dict[str, Any]:
        manager = self.archive_manager
        if manager is None:
            return {}
        target = session.get("target") if isinstance(session.get("target"), dict) else {}
        target_model_url = str(target.get("model_url") or "")
        target_model_id = str(target.get("model_id") or extract_model_id(target_model_url) or "").strip()
        primary = {}
        if target_model_id or extract_model_id(target_model_url):
            primary = {
                "model_id": target_model_id,
                "model_url": target_model_url,
                "title": target.get("title") or "",
                "instance_id": target.get("instance_id") or "",
                "status": "verification_required",
            }
        return manager.retry_verification_missing_3mf(
            platform=str(session.get("platform") or "cn"),
            primary=primary,
            proof_id=proof_id,
        )

    def _ensure_browser_dirs(self) -> None:
        BROWSER_VERIFICATION_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        BROWSER_VERIFICATION_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    def _ensure_virtual_display(self) -> None:
        if os.environ.get("DISPLAY"):
            return
        display = ":99"
        os.environ["DISPLAY"] = display
        subprocess.Popen(
            ["Xvfb", display, "-screen", "0", "1365x768x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.2)

    def _verification_clip(self, page: Any) -> dict[str, int]:
        try:
            raw_clip = page.evaluate(
                """
                () => {
                  const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 1024;
                  const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 720;
                  const pad = 24;
                  const directSelectors = [
                    '.cf-turnstile',
                    '[data-sitekey]',
                    'iframe[src*="challenges.cloudflare.com"]',
                    '.geetest_panel',
                    '.geetest_box',
                    '.geetest_popup_box',
                    '.geetest_widget',
                    '[class*="geetest"]',
                    '[class*="captcha"]',
                    '[id*="captcha"]',
                    '[class*="verify"]'
                  ];
                  const textNeedles = [
                    'verify you are human',
                    'performing security verification',
                    'security verification',
                    'not a bot'
                  ];
                  function visibleRect(element) {
                    if (!element || !element.getBoundingClientRect) return null;
                    const rect = element.getBoundingClientRect();
                    const style = window.getComputedStyle(element);
                    if (
                      rect.width < 80 ||
                      rect.height < 40 ||
                      rect.right <= 0 ||
                      rect.bottom <= 0 ||
                      rect.left >= viewportWidth ||
                      rect.top >= viewportHeight ||
                      style.visibility === 'hidden' ||
                      style.display === 'none' ||
                      Number(style.opacity || '1') <= 0.05
                    ) {
                      return null;
                    }
                    return rect;
                  }
                  function paddedClip(rect) {
                    const x = Math.max(0, Math.floor(rect.left - pad));
                    const y = Math.max(0, Math.floor(rect.top - pad));
                    const right = Math.min(viewportWidth, Math.ceil(rect.right + pad));
                    const bottom = Math.min(viewportHeight, Math.ceil(rect.bottom + pad));
                    return { x, y, width: right - x, height: bottom - y };
                  }
                  const candidates = [];
                  for (const selector of directSelectors) {
                    for (const element of document.querySelectorAll(selector)) {
                      const rect = visibleRect(element);
                      if (rect) {
                        candidates.push({ clip: paddedClip(rect), score: rect.width * rect.height });
                      }
                    }
                  }
                  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_ELEMENT);
                  while (walker.nextNode()) {
                    const element = walker.currentNode;
                    const text = String(element.innerText || element.textContent || '').toLowerCase();
                    if (!text || !textNeedles.some((needle) => text.includes(needle))) continue;
                    const rect = visibleRect(element);
                    if (!rect) continue;
                    candidates.push({ clip: paddedClip(rect), score: rect.width * rect.height + 1000000 });
                  }
                  candidates.sort((a, b) => a.score - b.score);
                  return candidates.length ? candidates[0].clip : null;
                }
                """
            )
        except Exception:
            return {}
        if not isinstance(raw_clip, dict):
            return {}
        try:
            x = max(0, int(float(raw_clip.get("x") or 0)))
            y = max(0, int(float(raw_clip.get("y") or 0)))
            width = int(float(raw_clip.get("width") or 0))
            height = int(float(raw_clip.get("height") or 0))
        except (TypeError, ValueError):
            return {}
        if width < 80 or height < 40:
            return {}
        return {"x": x, "y": y, "width": width, "height": height}

    def _write_screenshot(self, page: Any, session_id: str) -> None:
        path = self.store.screenshot_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        clip = self._verification_clip(page)
        screenshot_kwargs: dict[str, Any] = {
            "path": str(path),
            "type": "jpeg",
            "quality": 82,
            "timeout": 5000,
        }
        if clip:
            screenshot_kwargs["clip"] = dict(clip)
        try:
            page.screenshot(**screenshot_kwargs)
        except Exception:
            return
        current = self.store.get_session(session_id)
        version = int(current.get("screenshot_version") or 0) + 1 if current else 1
        previously_cropped = bool((current or {}).get("viewport", {}).get("cropped")) if isinstance((current or {}).get("viewport"), dict) else False
        viewport = (
            {
                "width": int(clip["width"]),
                "height": int(clip["height"]),
                "offset_x": int(clip["x"]),
                "offset_y": int(clip["y"]),
                "cropped": True,
            }
            if clip
            else dict(BROWSER_VERIFICATION_DEFAULT_VIEWPORT)
        )
        if clip and not previously_cropped:
            _browser_verification_log(
                "browser_verification_surface_detected",
                "浏览器验证已检测到可裁剪的验证区域。",
                session_id=session_id,
                platform=str((current or {}).get("platform") or ""),
                cropped=True,
                width=int(clip["width"]),
                height=int(clip["height"]),
            )
        self.store.update_session(session_id, screenshot_version=version, viewport=viewport)

    def _apply_input(self, page: Any, command: dict[str, Any], session: Optional[dict[str, Any]] = None) -> None:
        command_type = str(command.get("type") or "")
        viewport = session.get("viewport") if isinstance(session, dict) and isinstance(session.get("viewport"), dict) else {}
        x = int(command.get("x") or 0) + int(viewport.get("offset_x") or 0)
        y = int(command.get("y") or 0) + int(viewport.get("offset_y") or 0)
        if command_type == "click":
            page.mouse.click(x, y)
        elif command_type == "mousemove":
            page.mouse.move(x, y)
        elif command_type == "mousedown":
            page.mouse.move(x, y)
            page.mouse.down()
        elif command_type == "mouseup":
            page.mouse.move(x, y)
            page.mouse.up()
        elif command_type == "wheel":
            page.mouse.wheel(int(command.get("delta_x") or 0), int(command.get("delta_y") or 0))
        elif command_type == "key" and command.get("key"):
            page.keyboard.press(str(command.get("key")))
        elif command_type == "text" and command.get("text"):
            page.keyboard.type(str(command.get("text")))


browser_verification_runtime = BrowserVerificationRuntime()
