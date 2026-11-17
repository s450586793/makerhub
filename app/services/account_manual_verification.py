from __future__ import annotations

import base64
from contextlib import ExitStack
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import queue
import selectors
import subprocess
import threading
import time
from typing import Any
from urllib.parse import quote
import uuid

from app.core.settings import ROOT_DIR, STATE_DIR
from app.core.store import JsonStore
from app.core.timezone import now as china_now, now_iso as china_now_iso
from app.services import cloakbrowser_session as browser
from app.services.account_health import get_account_health
from app.services.account_verification_recovery import (
    _clear_verified_gate,
    _find_target,
    _fresh_target,
    _GATE_FIELDS,
    _queue_blockers,
    _same_login,
    _UNSAFE_STATUSES,
    _valid_download,
)
from app.services.cookie_utils import extract_auth_token, sanitize_cookie_header
from app.services.state_events import publish_state_event
from app.services.three_mf_pacing import ThreeMfAuthorizationAttempt
from app.services.three_mf_quota import reserve_three_mf_download_slot


MANUAL_BRIDGE_SCRIPT = ROOT_DIR / "app" / "services" / "cloakbrowser_manual.mjs"
SESSION_SECONDS = 10 * 60
_TERMINAL_STATUSES = {"completed", "cancelled", "expired", "failed"}
_MAX_REPLY_BYTES = 10 * 1024 * 1024


class ManualVerificationError(RuntimeError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


class _ManualBridge:
    """凭据仅通过私有管道传给当前子进程，不写入参数、日志或磁盘。"""

    def __init__(self, deadline: float, stopped: threading.Event):
        self.deadline = deadline
        self.stopped = stopped
        self.buffer = bytearray()
        self.process = subprocess.Popen(
            ["node", MANUAL_BRIDGE_SCRIPT.as_posix()],
            cwd=ROOT_DIR.as_posix(), env=browser._safe_bridge_env(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)

    def command(self, action: str, **payload: Any) -> dict[str, Any]:
        timeout = 50 if action == "init" else 3 if action == "close" else 20
        limit = time.monotonic() + timeout if action == "close" else min(self.deadline, time.monotonic() + timeout)
        if self.stopped.is_set() and action != "close":
            raise ManualVerificationError("验证窗口已关闭。")
        if self.process.poll() is not None or not self.process.stdin:
            raise ManualVerificationError("验证窗口连接已关闭，请重新打开。")
        self.process.stdin.write((json.dumps({"action": action, **payload}, ensure_ascii=False) + "\n").encode())
        self.process.stdin.flush()
        while time.monotonic() < limit:
            if self.stopped.is_set() and action != "close":
                raise ManualVerificationError("验证窗口已关闭。")
            if b"\n" in self.buffer:
                line, _, remaining = self.buffer.partition(b"\n")
                self.buffer = bytearray(remaining)
                output = json.loads(line)
                if not isinstance(output, dict):
                    raise ManualVerificationError("验证窗口返回异常，请重新打开。")
                return output
            for key, _mask in self.selector.select(timeout=0.2):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    raise ManualVerificationError("验证窗口连接已关闭，请重新打开。")
                self.buffer.extend(chunk)
                if len(self.buffer) > _MAX_REPLY_BYTES:
                    raise ManualVerificationError("验证画面过大，请重新打开。")
        raise ManualVerificationError("验证窗口响应超时，请重新打开。")

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                self.command("close")
            except Exception:
                pass
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.selector.close()
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()


@dataclass
class _Session:
    session_id: str
    platform: str
    owner: str
    account: Any
    snapshot: dict[str, Any]
    target: dict[str, str]
    expires_at: str
    deadline: float
    lease: Any = None
    status: str = "starting"
    message: str = "正在打开验证窗口。"
    image: str = ""
    width: int = 0
    height: int = 0
    dispatched: bool = False
    authorization_blocked: bool = False
    completed: bool = False
    stopped: threading.Event = field(default_factory=threading.Event)
    entered: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    commands: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=8))
    lock: threading.RLock = field(default_factory=threading.RLock)
    bridge: Any = None
    attempt: Any = None

    def public(self) -> dict[str, Any]:
        with self.lock:
            return {key: getattr(self, key) for key in (
                "session_id", "platform", "status", "message", "image", "width", "height", "expires_at", "completed",
            )}

    def update(self, **fields: Any) -> None:
        with self.lock:
            for key, value in fields.items():
                setattr(self, key, value)


class ManualVerificationManager:
    def __init__(self, store: JsonStore | None = None, *, state_dir: Path = STATE_DIR):
        self.store = store or JsonStore()
        self.state_dir = state_dir
        self.sessions: dict[str, _Session] = {}
        self.lock = threading.RLock()

    @staticmethod
    def _owner(raw_session_id: str) -> str:
        if not raw_session_id:
            raise ManualVerificationError("此操作需要登录会话。", 403)
        return hashlib.sha256(raw_session_id.encode()).hexdigest()

    def _lease(self):
        import fcntl

        path = self.state_dir / "account_manual_verification.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise ManualVerificationError("已有验证窗口正在使用，请先完成或关闭。") from None
        return handle

    def create(self, platform: str, owner_session_id: str) -> dict[str, Any]:
        owner = self._owner(owner_session_id)
        if platform not in {"cn", "global"}:
            raise ManualVerificationError("不支持的平台。", 404)
        if not browser.cloakbrowser_configured():
            raise ManualVerificationError("指纹浏览器服务未配置。", 503)
        with self.lock:
            self._prune()
            for existing in self.sessions.values():
                if not existing.done.is_set():
                    if (not existing.stopped.is_set() and existing.status not in _TERMINAL_STATUSES
                            and existing.owner == owner and existing.platform == platform):
                        return existing.public()
                    raise ManualVerificationError("已有验证窗口正在使用，请先完成或关闭。")
            config = self.store.load()
            account = next((item for item in config.cookies if item.platform == platform), None)
            if account is None or not str(account.browser_profile_id or "").strip():
                raise ManualVerificationError("请先在设置中关联该平台的指纹浏览器。", 400)
            if account.browser_status == "account_mismatch":
                raise ManualVerificationError("浏览器账号不一致，请先重新关联账号。")
            try:
                if _queue_blockers(platform).get("active"):
                    raise ManualVerificationError("归档任务正在运行，请稍后打开验证窗口。")
            except ManualVerificationError:
                raise
            except Exception:
                raise ManualVerificationError("归档状态暂时不可用，请稍后重试。", 503) from None
            snapshot = get_account_health(platform)
            try:
                target = _find_target(platform, snapshot) or {}
            except Exception:
                target = {}
            session = _Session(
                session_id=uuid.uuid4().hex, platform=platform, owner=owner,
                account=account.model_copy(deep=True), snapshot=snapshot, target=target,
                expires_at=(china_now() + timedelta(seconds=SESSION_SECONDS)).isoformat(timespec="seconds"),
                deadline=time.monotonic() + SESSION_SECONDS, lease=self._lease(),
            )
            self.sessions[session.session_id] = session
            try:
                threading.Thread(target=self._worker, args=(session,), daemon=True,
                                 name=f"makerhub-manual-verification-{platform}").start()
            except Exception:
                session.lease.close()
                self.sessions.pop(session.session_id, None)
                raise ManualVerificationError("暂时无法打开验证窗口，请稍后重试。", 503) from None
            return session.public()

    def _prune(self) -> None:
        for session_id, session in list(self.sessions.items()):
            if time.monotonic() > session.deadline:
                with session.lock:
                    if session.status not in _TERMINAL_STATUSES:
                        session.update(status="expired", message="验证窗口已过期，请重新打开。")
                        session.stopped.set()
                        if not session.entered.is_set():
                            if session.lease:
                                session.lease.close()
                            session.done.set()
                if time.monotonic() - session.deadline > SESSION_SECONDS:
                    self.sessions.pop(session_id, None)

    def _get(self, platform: str, session_id: str, owner_session_id: str) -> _Session:
        owner = self._owner(owner_session_id)
        with self.lock:
            self._prune()
            session = self.sessions.get(session_id)
            if session is None or session.platform != platform or session.owner != owner:
                raise ManualVerificationError("验证窗口不存在或不属于当前登录会话。", 404)
            return session

    def execute(self, platform: str, session_id: str, owner_session_id: str,
                action: str, *, events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        session = self._get(platform, session_id, owner_session_id)
        if action == "close":
            with session.lock:
                if session.status not in _TERMINAL_STATUSES:
                    session.update(status="cancelled", message="验证窗口已关闭。")
                    session.stopped.set()
                    if not session.entered.is_set():
                        if session.lease:
                            session.lease.close()
                        session.done.set()
            session.done.wait(timeout=3)
            return session.public()
        if session.status in _TERMINAL_STATUSES:
            return session.public()
        future: Future = Future()
        try:
            session.commands.put_nowait((action, events or [], future))
        except queue.Full:
            raise ManualVerificationError("验证操作过于频繁，请稍后重试。", 429) from None
        try:
            return future.result(timeout=22)
        except FutureTimeout:
            return session.public()

    def _safe_frame(self, session: _Session, output: dict[str, Any]) -> None:
        if session.attempt is not None and isinstance(output.get("authorization"), dict):
            session.attempt.response = output["authorization"]
        if session.stopped.is_set():
            return
        fields: dict[str, Any] = {}
        image = output.get("image")
        if isinstance(image, str) and len(image) <= 8 * 1024 * 1024:
            try:
                decoded = base64.b64decode(image, validate=True)
                width, height = output.get("width"), output.get("height")
                if (decoded.startswith(b"\xff\xd8\xff") and type(width) is int and type(height) is int
                        and 0 < width <= 4096 and 0 < height <= 4096):
                    fields.update(image=image, width=width, height=height)
            except (ValueError, TypeError):
                pass
        status = output.get("status")
        if status == "error":
            raise ManualVerificationError("验证窗口暂时不可用，请重新打开。")
        if status in {"verifying", "verification_required", "waiting", "ready", "authorizing", "authorized", "auth_required",
                      "page_ready", "page_verification", "challenge", "loading"}:
            messages = {
                "ready": "页面已就绪，正在复核下载权限。" if session.target else "页面验证已完成，可点击完成。",
                "authorizing": "请在画面中完成验证码，成功后会自动继续。",
                "authorized": "验证已通过，正在恢复归档。",
                "auth_required": "请在画面中完成登录。",
                "page_ready": "页面验证已完成，可点击完成。",
                "loading": "正在加载验证页面。",
            }
            fields.update(status=status, message=messages.get(status, "请在画面中完成验证。"))
            if session.authorization_blocked:
                fields["message"] = session.message
        with session.lock:
            if not session.stopped.is_set() and session.status not in _TERMINAL_STATUSES:
                session.update(**fields)

    def _dispatch(self, session: _Session, output: dict[str, Any]):
        if session.dispatched or session.authorization_blocked or not session.target or output.get("status") != "ready":
            return None
        target = _fresh_target(session.platform, session.target, output.get("preflight") or {})
        if not target or target["instance_id"] != session.target["instance_id"]:
            return None
        latest = get_account_health(session.platform)
        if (not _same_login(self.store, session.account) or latest.get("status") in _UNSAFE_STATUSES
                or any(latest.get(key, "") != session.snapshot.get(key, "") for key in _GATE_FIELDS)):
            raise ManualVerificationError("账号或验证状态已变化，请重新打开验证窗口。")
        blockers = _queue_blockers(session.platform)
        if blockers.get("active"):
            raise ManualVerificationError("归档任务正在运行，请稍后重新打开验证窗口。")
        if blockers.get("manual"):
            session.update(message="有归档任务正在运行或已手动暂停，暂不发起下载授权。")
            return None
        attempt = ThreeMfAuthorizationAttempt(session.platform, session.account.browser_profile_id, self.state_dir,
                                              model_url=target["model_url"], instance_id=target["instance_id"],
                                              verification_probe=True)
        attempt.__enter__()
        attempt.request_started = False
        if attempt.response is not None:
            attempt.__exit__(None, None, None)
            session.authorization_blocked = True
            session.update(message="下载授权正在冷却或今日额度已满，请稍后重试。")
            return None
        # 节奏等待后重新核对身份、状态和用户暂停，再预约一次下载额度。
        latest = get_account_health(session.platform)
        blockers = _queue_blockers(session.platform)
        if blockers.get("active"):
            attempt.__exit__(None, None, None)
            raise ManualVerificationError("归档任务正在运行，请稍后重新打开验证窗口。")
        if (session.stopped.is_set() or time.monotonic() >= session.deadline
                or not _same_login(self.store, session.account) or blockers.get("manual")
                or any(latest.get(key, "") != session.snapshot.get(key, "") for key in _GATE_FIELDS)
                or latest.get("status") in _UNSAFE_STATUSES):
            attempt.__exit__(None, None, None)
            return None
        config = self.store.load()
        quota = reserve_three_mf_download_slot(source=session.platform, url=target["model_url"],
                                              limit=getattr(config.three_mf_limits, f"{session.platform}_daily_limit"), **target)
        if quota.get("allowed") is not True:
            attempt.__exit__(None, None, None)
            session.authorization_blocked = True
            session.update(message="已达到今日 3MF 下载额度，请明日重试。")
            return None
        session.dispatched = True
        attempt.request_started = True
        session.attempt = attempt
        self._safe_frame(session, session.bridge.command("authorize"))
        return attempt

    def _sync_snapshot(self, session: _Session, output: dict[str, Any], *, authorized: bool) -> bool:
        snapshot = output.get("snapshot")
        if not isinstance(snapshot, dict):
            return False
        cookie = browser._authenticated_makerworld_cookie(snapshot, session.platform)
        if not cookie:
            return False
        if extract_auth_token(cookie) != extract_auth_token(session.account.cookie):
            raise ManualVerificationError("浏览器登录态已变化，请先在设置中同步账号，再重新验证。")
        applied = False

        def sync(config):
            nonlocal applied
            current = next((item for item in config.cookies if item.platform == session.platform), None)
            if (current is None or current.browser_profile_id != session.account.browser_profile_id
                    or sanitize_cookie_header(current.cookie) != sanitize_cookie_header(session.account.cookie)
                    or current.browser_status == "account_mismatch"):
                return config
            current.cookie = cookie
            current.browser_status = "synced"
            current.browser_message = "人工验证完成，浏览器登录态已同步。"
            current.browser_synced_at = china_now_iso()
            applied = True
            return config

        saved = self.store.update(sync)
        if applied:
            session.account.cookie = cookie
            publish_state_event("online_accounts", "state.changed", {"platform": session.platform, "status": "synced"})
            if not authorized:
                from app.api.config import _schedule_online_account_cookie_test

                current = next(item for item in saved.cookies if item.platform == session.platform)
                _schedule_online_account_cookie_test(session.platform, current, saved.proxy)
        return applied

    def _complete_authorization(self, session: _Session, output: dict[str, Any], attempt: Any) -> bool:
        if session.stopped.is_set() or time.monotonic() >= session.deadline:
            return False
        response = output.get("authorization")
        if output.get("status") != "authorized" or not session.dispatched or not isinstance(response, dict):
            return False
        if attempt is not None:
            attempt.response = response
        status_code = response.get("status_code")
        snapshot = output.get("snapshot")
        browser_cookie = browser._authenticated_makerworld_cookie(snapshot, session.platform) if isinstance(snapshot, dict) else ""
        if browser_cookie and extract_auth_token(browser_cookie) != extract_auth_token(session.account.cookie):
            raise ManualVerificationError("浏览器登录态已变化，请先在设置中同步账号，再重新验证。")
        if (type(status_code) is not int or not 200 <= status_code < 300 or not _valid_download(response.get("payload"))
                or not browser_cookie or extract_auth_token(browser_cookie) != extract_auth_token(session.account.cookie)
                or not _same_login(self.store, session.account) or any(_queue_blockers(session.platform).values())):
            raise ManualVerificationError("尚未确认下载权限，当前暂停状态已保留。")
        if not self._sync_snapshot(session, output, authorized=True) or not _same_login(self.store, session.account):
            raise ManualVerificationError("账号已变化，当前暂停状态已保留。")
        # 取消与提交共用同一把锁，谁先提交决定本次是否恢复归档。
        with session.lock:
            if session.stopped.is_set() or time.monotonic() >= session.deadline:
                return False
            if not _clear_verified_gate(session.platform, session.snapshot, session.target):
                raise ManualVerificationError("下载状态已变化，当前暂停状态已保留。")
            session.update(status="completed", message="验证已通过，归档将自动继续。", completed=True)
        from app.api.dependencies import crawler

        crawler.manager.ensure_worker_for_pending(force=True)
        return True

    def _finish_page(self, session: _Session, output: dict[str, Any]) -> bool:
        if session.target or output.get("status") not in {"ready", "page_ready"}:
            return False
        synced = self._sync_snapshot(session, output, authorized=False)
        with session.lock:
            if session.stopped.is_set() or time.monotonic() >= session.deadline or not synced:
                return False
            session.update(status="completed", message="页面验证完成，账号状态正在复核。", completed=True)
            return True

    def _worker(self, session: _Session) -> None:
        attempt = None
        operation = ExitStack()
        pending_future = None
        try:
            operation.enter_context(browser._profile_operation(session.platform, session.account.browser_profile_id, detail="prepare-login"))
            with session.lock:
                session.entered.set()
                if session.stopped.is_set() or time.monotonic() >= session.deadline:
                    return
            if not _same_login(self.store, session.account):
                raise ManualVerificationError("账号已变化，请重新打开验证窗口。")
            if _queue_blockers(session.platform).get("active"):
                raise ManualVerificationError("归档任务正在运行，请稍后重新打开验证窗口。")
            profile = browser._profile_from_payload(browser._request(
                "GET", f"/api/profiles/{quote(session.account.browser_profile_id, safe='')}",
            ))
            if profile is None or profile.id != session.account.browser_profile_id:
                raise ManualVerificationError("关联的浏览器已不存在，请重新关联。")
            running, _launched = browser.launch_profile(profile)
            if session.stopped.is_set() or time.monotonic() >= session.deadline:
                return
            session.bridge = _ManualBridge(session.deadline, session.stopped)
            output = session.bridge.command(
                "init", cdp_url=f"{browser._configured_url()}/api/profiles/{quote(running.id, safe='')}/cdp",
                auth_token=browser._auth_token(), platform=session.platform,
                model_url=session.target.get("model_url") or f"{browser.PLATFORM_ORIGINS[session.platform]}/zh",
                instance_id=session.target.get("instance_id", ""),
                lifetime_ms=max(1, int((session.deadline - time.monotonic()) * 1000)),
            )
            self._safe_frame(session, output)
            attempt = self._dispatch(session, output)
            while not session.stopped.is_set() and time.monotonic() < session.deadline:
                try:
                    action, events, future = session.commands.get(timeout=0.5)
                    pending_future = future
                except queue.Empty:
                    continue
                if not _same_login(self.store, session.account):
                    raise ManualVerificationError("账号已变化，请重新打开验证窗口。")
                browser.touch_profile_activity(session.platform, detail="prepare-login")
                payload = {"events": events} if action == "input" else {"snapshot": True} if action == "finish" else {}
                output = session.bridge.command("frame" if action == "finish" else action, **payload)
                self._safe_frame(session, output)
                if attempt is None:
                    attempt = self._dispatch(session, output)
                if self._complete_authorization(session, output, attempt):
                    future.set_result(session.public())
                    break
                if action == "finish":
                    if self._finish_page(session, output):
                        future.set_result(session.public())
                        break
                    if not session.authorization_blocked and not session.stopped.is_set():
                        session.update(message="请先在画面中完成验证，下载权限确认后会自动继续。")
                future.set_result(session.public())
                pending_future = None
        except ManualVerificationError as exc:
            with session.lock:
                if session.status not in _TERMINAL_STATUSES:
                    session.update(status="failed", message=str(exc))
        except Exception:
            with session.lock:
                if session.status not in _TERMINAL_STATUSES:
                    session.update(status="failed", message="验证窗口暂时不可用，请重新打开。")
        finally:
            with session.lock:
                if time.monotonic() >= session.deadline and session.status not in _TERMINAL_STATUSES:
                    session.update(status="expired", message="验证窗口已过期，请重新打开。")
            try:
                if session.bridge is not None:
                    session.bridge.close()
            except Exception:
                pass
            finally:
                try:
                    if session.attempt is not None:
                        session.attempt.__exit__(None, None, None)
                except Exception:
                    pass
                finally:
                    operation.close()
                    if session.lease:
                        session.lease.close()
                    session.done.set()
            if pending_future is not None and not pending_future.done():
                pending_future.set_result(session.public())
            while not session.commands.empty():
                _action, _events, future = session.commands.get_nowait()
                future.set_result(session.public())

    def shutdown(self) -> None:
        with self.lock:
            sessions = list(self.sessions.values())
            for session in sessions:
                with session.lock:
                    if session.status not in _TERMINAL_STATUSES:
                        session.update(status="cancelled", message="验证窗口已关闭。")
                        session.stopped.set()
        deadline = time.monotonic() + 4
        for session in sessions:
            session.done.wait(timeout=max(0, deadline - time.monotonic()))


manual_verification_manager = ManualVerificationManager()
