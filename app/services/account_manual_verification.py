from __future__ import annotations

from concurrent.futures import CancelledError
from dataclasses import dataclass, field
from datetime import timedelta
import hashlib
from pathlib import Path
import threading
import time
from typing import Any
import uuid

from app.core.settings import STATE_DIR
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
from app.services.makerworld_captcha import challenge_config, encode_proof, fetch_authorization
from app.services.state_events import publish_state_event
from app.services.three_mf_pacing import ThreeMfAuthorizationAttempt
from app.services.three_mf_quota import reserve_three_mf_download_slot


SESSION_SECONDS = 10 * 60
BROWSER_WAIT_SECONDS = 60
_TERMINAL_STATUSES = {"completed", "cancelled", "expired", "failed"}


class ManualVerificationError(RuntimeError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


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
    status: str = "preparing"
    message: str = "正在获取 MW 验证码。"
    challenge: dict[str, Any] | None = None
    completed: bool = False
    quota_reserved: bool = False
    busy: bool = False
    submissions: set[str] = field(default_factory=set)
    stopped: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def public(self) -> dict[str, Any]:
        with self.lock:
            return {
                "session_id": self.session_id, "platform": self.platform, "status": self.status,
                "message": self.message, "expires_at": self.expires_at, "completed": self.completed,
                "challenge": dict(self.challenge) if self.challenge else None,
            }


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

    @staticmethod
    def _release(session: _Session) -> None:
        if session.lease is not None:
            session.lease.close()
            session.lease = None
        session.done.set()

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
            account = next((item for item in self.store.load().cookies if item.platform == platform), None)
            if account is None or not str(account.browser_profile_id or "").strip() or not extract_auth_token(account.cookie):
                raise ManualVerificationError("请先在设置中关联并同步该平台的账号。", 400)
            if account.browser_status == "account_mismatch":
                raise ManualVerificationError("浏览器账号不一致，请先重新关联账号。")
            try:
                if any(_queue_blockers(platform).values()):
                    raise ManualVerificationError("归档任务正在运行或已手动暂停，请稍后验证。")
                snapshot = get_account_health(platform)
                target = _find_target(platform, snapshot)
            except ManualVerificationError:
                raise
            except Exception:
                raise ManualVerificationError("归档状态暂时不可用，请稍后重试。", 503) from None
            if snapshot.get("status") in _UNSAFE_STATUSES:
                raise ManualVerificationError("账号当前无法下载，请先处理登录、额度或手动暂停状态。")
            if snapshot.get("three_mf_gate") not in {"verification_required", "cloudflare"}:
                raise ManualVerificationError("当前账号没有等待处理的下载验证，请先刷新账号状态。")
            if not target:
                raise ManualVerificationError("没有可用于验证的下载目标，请先重试对应模型的归档。", 400)
            session = _Session(
                session_id=uuid.uuid4().hex, platform=platform, owner=owner,
                account=account.model_copy(deep=True), snapshot=snapshot, target=target,
                expires_at=(china_now() + timedelta(seconds=SESSION_SECONDS)).isoformat(timespec="seconds"),
                deadline=time.monotonic() + SESSION_SECONDS, lease=self._lease(),
            )
            self.sessions[session.session_id] = session
            self._start(session)
            return session.public()

    def _start(self, session: _Session, proof_header: str | None = None) -> None:
        session.busy = True
        try:
            threading.Thread(
                target=self._prepare if proof_header is None else self._submit,
                args=(session,) if proof_header is None else (session, proof_header),
                daemon=True, name=f"makerhub-manual-verification-{session.platform}",
            ).start()
        except Exception:
            session.busy = False
            session.status = "failed"
            session.message = "暂时无法打开验证码，请稍后重试。"
            self._release(session)
            raise ManualVerificationError(session.message, 503) from None

    def _prune(self) -> None:
        for session_id, session in list(self.sessions.items()):
            if time.monotonic() >= session.deadline:
                with session.lock:
                    if session.status not in _TERMINAL_STATUSES:
                        session.status, session.message = "expired", "验证窗口已过期，请重新打开。"
                        session.challenge = None
                        session.stopped.set()
                    if not session.busy:
                        self._release(session)
                if session.done.is_set() and time.monotonic() - session.deadline > SESSION_SECONDS:
                    self.sessions.pop(session_id, None)

    def prune_expired(self) -> None:
        with self.lock:
            self._prune()

    def _get(self, platform: str, session_id: str, owner_session_id: str) -> _Session:
        owner = self._owner(owner_session_id)
        with self.lock:
            self._prune()
            session = self.sessions.get(session_id)
            if session is None or session.platform != platform or session.owner != owner:
                raise ManualVerificationError("验证窗口不存在或不属于当前登录会话。", 404)
            return session

    def execute(self, platform: str, session_id: str, owner_session_id: str, action: str,
                *, challenge_id: str = "", proof: dict[str, Any] | None = None) -> dict[str, Any]:
        session = self._get(platform, session_id, owner_session_id)
        with session.lock:
            if action == "close":
                if session.status not in _TERMINAL_STATUSES:
                    session.status, session.message = "cancelled", "验证窗口已关闭。"
                    session.challenge = None
                    session.stopped.set()
                if not session.busy:
                    self._release(session)
                return session.public()
            if action == "status" or session.status in _TERMINAL_STATUSES:
                return session.public()
            if action != "complete":
                raise ManualVerificationError("不支持的验证操作。", 400)
            if session.status == "verifying" and challenge_id in session.submissions:
                return session.public()
            if (session.status != "challenge" or session.busy or not session.challenge
                    or challenge_id != session.challenge["challenge_id"]):
                raise ManualVerificationError("验证码已更新，请使用当前窗口重新验证。")
            try:
                proof_header = encode_proof(proof, session.challenge["captcha_id"])
            except ValueError as exc:
                raise ManualVerificationError(str(exc), 400) from None
            session.submissions.add(challenge_id)
            session.challenge = None
            session.status, session.message = "verifying", "正在确认 MW 下载权限。"
            self._start(session, proof_header)
            return session.public()

    def _check(self, session: _Session) -> None:
        if session.stopped.is_set():
            raise CancelledError()
        if time.monotonic() >= session.deadline:
            raise TimeoutError()
        latest = get_account_health(session.platform)
        if (not _same_login(self.store, session.account) or latest.get("status") in _UNSAFE_STATUSES
                or any(latest.get(key, "") != session.snapshot.get(key, "") for key in _GATE_FIELDS)):
            raise ManualVerificationError("账号或验证状态已变化，请重新打开验证窗口。")
        if any(_queue_blockers(session.platform).values()):
            raise ManualVerificationError("归档任务正在运行或已手动暂停，当前暂停状态已保留。")

    def _identity(self, session: _Session) -> str:
        cookie = browser.collect_browser_session(session.platform, session.account.browser_profile_id).cookie
        token = extract_auth_token(cookie)
        if not token or token != extract_auth_token(session.account.cookie):
            raise ManualVerificationError("浏览器登录态已变化，请先在设置中同步账号，再重新验证。")
        return cookie

    def _sync_cookie(self, session: _Session, cookie: str) -> bool:
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
            current.browser_message = "人工验证码通过，下载权限已确认。"
            current.browser_synced_at = china_now_iso()
            applied = True
            return config

        self.store.update(sync)
        if applied:
            session.account.cookie = cookie
            publish_state_event("online_accounts", "state.changed", {"platform": session.platform, "status": "synced"})
        return applied

    def _prepare(self, session: _Session) -> None:
        self._run(session)

    def _submit(self, session: _Session, proof_header: str) -> None:
        self._run(session, proof_header)

    def _run(self, session: _Session, proof_header: str = "") -> None:
        pending_challenge = None
        try:
            self._check(session)
            # 只在请求期间占锁；用户解题期间没有页签、截图桥接或等待线程。
            with browser._profile_operation(
                session.platform, session.account.browser_profile_id, detail="captcha",
                timeout_seconds=min(BROWSER_WAIT_SECONDS, max(0, session.deadline - time.monotonic())),
                cancel_event=session.stopped,
            ):
                self._check(session)
                self._identity(session)
                preflight = browser.browser_prepare_3mf_download(
                    session.platform, profile_id=session.account.browser_profile_id, model_url=session.target["model_url"],
                )
                target = _fresh_target(session.platform, session.target, preflight)
                if not target or target["instance_id"] != session.target["instance_id"]:
                    reason = preflight.get("reason") if isinstance(preflight, dict) else ""
                    message = ("CF 页面验证暂未自动通过，请稍后重试。" if reason == "verification_required" else
                               "请先在设置中重新登录并同步账号。" if reason == "auth_required" else
                               "当前模型或打印配置不可下载，请重新选择归档目标。")
                    raise ManualVerificationError(message)
                with ThreeMfAuthorizationAttempt(
                    session.platform, session.account.browser_profile_id, self.state_dir,
                    model_url=target["model_url"], instance_id=target["instance_id"],
                    verification_probe=True, update_gate=False, cancel_event=session.stopped,
                ) as attempt:
                    attempt.request_started = False
                    if attempt.response is not None:
                        raise ManualVerificationError("下载授权正在冷却或今日额度已满，请稍后重试。")
                    self._check(session)
                    # 等待节奏期间浏览器也可能被用户直接切换账号。
                    self._identity(session)
                    if not session.quota_reserved:
                        config = self.store.load()
                        quota = reserve_three_mf_download_slot(
                            source=session.platform, url=target["model_url"],
                            limit=getattr(config.three_mf_limits, f"{session.platform}_daily_limit"), **target,
                        )
                        if quota.get("allowed") is not True:
                            raise ManualVerificationError("已达到今日 3MF 下载额度，请明日重试。")
                        session.quota_reserved = True
                    self._check(session)
                    attempt.request_started = True
                    response = fetch_authorization(
                        session.platform, session.account.browser_profile_id, target,
                        proxy_config=self.store.load().proxy, proof_header=proof_header,
                    )
                    attempt.response = response
                    cookie = self._identity(session)
                self._check(session)
                status_code = response.get("status_code")
                payload = response.get("payload")
                if type(status_code) is int and 200 <= status_code < 300 and _valid_download(payload):
                    # 取消与最终提交互斥；迟到响应不能清除门禁。
                    with session.lock:
                        self._check(session)
                        if not self._sync_cookie(session, cookie) or not _same_login(self.store, session.account):
                            raise ManualVerificationError("账号已变化，当前暂停状态已保留。")
                        if not _clear_verified_gate(session.platform, session.snapshot, session.target):
                            raise ManualVerificationError("下载状态已变化，当前暂停状态已保留。")
                        session.status, session.message = "completed", "验证已通过，归档将自动继续。"
                        session.completed = True
                    from app.api.dependencies import crawler

                    crawler.manager.ensure_worker_for_pending(force=True)
                    return
                config = challenge_config(payload) if status_code == 418 else None
                if not config:
                    if isinstance(payload, dict) and payload.get("code") == "MAKERHUB_CLOUDFLARE":
                        raise ManualVerificationError("CF 页面验证暂未自动通过，请稍后重试。")
                    raise ManualVerificationError("尚未确认 MW 下载权限，当前暂停状态已保留，请稍后重试。")
            self._check(session)
            pending_challenge = {"challenge_id": uuid.uuid4().hex, **config}
        except CancelledError:
            self._fail(session, "cancelled", "验证窗口已关闭。")
        except TimeoutError:
            expired = time.monotonic() >= session.deadline
            self._fail(session, "expired" if expired else "failed",
                       "验证窗口已过期，请重新打开。" if expired else "浏览器仍忙，请稍后重新打开验证。")
        except ManualVerificationError as exc:
            self._fail(session, "failed", str(exc))
        except Exception:
            self._fail(session, "failed", "MW 验证暂时不可用，请重新打开。")
        finally:
            with session.lock:
                # 释放操作标记与发布 challenge 同步，避免极快回调撞上旧 busy。
                session.busy = False
                if pending_challenge and session.status not in _TERMINAL_STATUSES:
                    if session.stopped.is_set():
                        self._fail(session, "cancelled", "验证窗口已关闭。")
                    elif time.monotonic() >= session.deadline:
                        self._fail(session, "expired", "验证窗口已过期，请重新打开。")
                    else:
                        session.challenge = pending_challenge
                        session.status, session.message = "challenge", "请完成 MW 图形验证码，通过后将自动继续。"
                if session.status in _TERMINAL_STATUSES:
                    session.challenge = None
                    self._release(session)

    @staticmethod
    def _fail(session: _Session, status: str, message: str) -> None:
        with session.lock:
            if session.status not in _TERMINAL_STATUSES:
                session.status, session.message = status, message
                session.challenge = None

    def shutdown(self) -> None:
        with self.lock:
            sessions = list(self.sessions.values())
            for session in sessions:
                with session.lock:
                    if session.status not in _TERMINAL_STATUSES:
                        session.status, session.message = "cancelled", "验证窗口已关闭。"
                        session.challenge = None
                        session.stopped.set()
                    if not session.busy:
                        self._release(session)
        deadline = time.monotonic() + 4
        for session in sessions:
            session.done.wait(timeout=max(0, deadline - time.monotonic()))


manual_verification_manager = ManualVerificationManager()
