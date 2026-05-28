from __future__ import annotations

import base64
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
from app.services.cookie_utils import parse_cookie_values, sanitize_cookie_header
from app.services.proxy_policy import proxy_url
from app.services.state_events import publish_state_event
from app.services.three_mf import normalize_makerworld_source, normalize_three_mf_failure_state


BROWSER_VERIFICATION_STATE_KEY = "browser_verification_sessions"
BROWSER_VERIFICATION_DIR = STATE_DIR / "browser_verification"
BROWSER_VERIFICATION_SCREENSHOT_DIR = BROWSER_VERIFICATION_DIR / "screenshots"
BROWSER_VERIFICATION_PROFILE_DIR = BROWSER_VERIFICATION_DIR / "profiles"
BROWSER_VERIFICATION_INPUT_LIMIT = 100
BROWSER_VERIFICATION_SESSION_LIMIT = 20
BROWSER_VERIFICATION_DEFAULT_VIEWPORT = {"width": 1365, "height": 768}
VERIFICATION_RETRY_STATES = {"verification_required", "cloudflare", "auth_required"}
SENSITIVE_REQUEST_HEADER_KEYS = {
    "x-bbl-captcha-result",
    "cookie",
    "authorization",
    "token",
    "x-token",
    "x-access-token",
}


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
        state = self.load_state()
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
            if item.get("status") in {"queued", "starting", "running", "verified", "retrying"}
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
            if item.get("id") == clean_id and item.get("status") in {"queued", "starting", "running", "verified", "retrying"}:
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


def _cookie_domain(platform: str) -> str:
    return ".makerworld.com" if platform == "global" else ".makerworld.com.cn"


def _cookies_for_context(raw_cookie: str, platform: str) -> list[dict[str, Any]]:
    domain = _cookie_domain(platform)
    cookies = []
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


@dataclass
class BrowserVerificationRuntime:
    store: BrowserVerificationStore = browser_verification_store
    archive_manager: Any = None
    json_store: Any = None

    def __post_init__(self) -> None:
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}

    def poll_once(self) -> dict[str, Any]:
        started = 0
        with self._lock:
            live_ids = {key for key, thread in self._threads.items() if thread.is_alive()}
            self._threads = {key: thread for key, thread in self._threads.items() if key in live_ids}
            for session in self.store.active_or_queued_sessions():
                session_id = str(session.get("id") or "")
                if not session_id or session_id in self._threads:
                    continue
                if session.get("status") in {"queued", "starting", "running"}:
                    thread = threading.Thread(
                        target=self._run_session_guarded,
                        args=(session_id,),
                        name=f"makerhub-browser-verification-{session_id[:10]}",
                        daemon=True,
                    )
                    self._threads[session_id] = thread
                    thread.start()
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
            append_business_log(
                "missing_3mf",
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

        self._ensure_browser_dirs()
        self._ensure_virtual_display()
        context = None
        try:
            context = self._launch_context(session, config)
            cookies = _cookies_for_context(raw_cookie, platform)
            if cookies:
                context.add_cookies(cookies)
            page = context.pages[0] if getattr(context, "pages", None) else context.new_page()
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
            target = session.get("target") if isinstance(session.get("target"), dict) else {}
            start_url = target.get("model_url") or _origin_for_platform(platform)
            self.store.update_session(session_id, status="running", message="验证浏览器已打开，请在画面中完成验证。")
            page.goto(start_url, wait_until="domcontentloaded", timeout=45000)
            deadline = time.time() + 15 * 60
            while time.time() < deadline:
                current = self.store.get_session(session_id)
                if not current or current.get("status") in {"cancelled", "completed", "failed"}:
                    return
                for command in self.store.consume_input_commands(session_id):
                    self._apply_input(page, command)
                self._write_screenshot(page, session_id)
                proof_id = captured_proof.get("id") or current.get("proof_id") or ""
                if proof_id:
                    retry_result = self._retry_after_verification(current, proof_id)
                    self.store.update_session(
                        session_id,
                        status="completed",
                        message="验证已完成，重试任务已提交。",
                        retry_result=retry_result,
                        finished_at=_now(),
                    )
                    return
                time.sleep(0.75)
            self.store.update_session(
                session_id,
                status="expired",
                message="验证会话已超时。",
                finished_at=_now(),
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
            "args": ["--no-sandbox", "--disable-dev-shm-usage"],
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

    def _write_screenshot(self, page: Any, session_id: str) -> None:
        path = self.store.screenshot_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=str(path), type="jpeg", quality=82, timeout=5000)
        except Exception:
            return
        current = self.store.get_session(session_id)
        version = int(current.get("screenshot_version") or 0) + 1 if current else 1
        self.store.update_session(session_id, screenshot_version=version, viewport=dict(BROWSER_VERIFICATION_DEFAULT_VIEWPORT))

    def _apply_input(self, page: Any, command: dict[str, Any]) -> None:
        command_type = str(command.get("type") or "")
        x = int(command.get("x") or 0)
        y = int(command.get("y") or 0)
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
