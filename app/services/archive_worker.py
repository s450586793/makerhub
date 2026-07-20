import hmac
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from app.core.database_json_state import load_database_json_state, save_database_json_state
from app.core.settings import ARCHIVE_DIR, BACKGROUND_TASKS_ENABLED, LOGS_DIR, STATE_DIR, ensure_app_dirs
from app.core.store import JsonStore
from app.core.timezone import now as china_now, parse_datetime
from app.services.cloakbrowser_session import CloakBrowserError, cloakbrowser_configured, collect_browser_session
from app.services.cookie_utils import extract_auth_token, sanitize_cookie_header
from app.services.batch_discovery import (
    extract_model_id,
    normalize_model_url,
    normalize_source_url,
    resolve_batch_source_name,
)
from app.services.account_health import (
    get_account_health,
    mark_account_checking,
    mark_account_ok,
    open_three_mf_gate,
    update_three_mf_gate,
)
from app.services.business_logs import append_business_log, append_structured_log
from app.services.catalog import (
    get_archive_snapshot,
    invalidate_archive_snapshot,
    invalidate_model_detail_cache,
    upsert_archive_snapshot_model,
)
from app.services.process_jobs import run_archive_model_job, run_discover_batch_urls_job
from app.services.proxy_policy import temporary_proxy_env
from app.services.resource_limiter import resource_slot
from app.services.task_state import TaskStateStore
from app.services.state_events import publish_state_event
from app.services.three_mf import (
    describe_three_mf_failure,
    normalize_makerworld_source,
    normalize_three_mf_failure_state,
)
from app.services.three_mf_quota import reset_three_mf_daily_quota

BATCH_TASK_MODES = {"author_upload", "collection_models"}
BATCH_QUEUE_LOG_PATH = LOGS_DIR / "batch_queue.log"
MAX_BATCH_CHILD_REQUEUE_ATTEMPTS = 3
MAX_BATCH_CHILD_TRANSIENT_REQUEUE_ATTEMPTS = 5
ACTIVE_BATCH_IDLE_POLL_SECONDS = 2.0
COLLECTION_DETAIL_RE = re.compile(r"/(?:[a-z]{2}/)?collections/\d+(?:-[^/?#]+)?(?:[/?#]|$)", re.I)
THREE_MF_LIMIT_GUARD_PATH = STATE_DIR / "three_mf_limit_guard.json"
THREE_MF_LIMIT_GUARD_KEY = "three_mf_limit_guard"
THREE_MF_LIMIT_DEFAULT_MESSAGE = "已达到 MakerWorld 每日下载上限，今日暂停自动重试。"
DEFAULT_THREE_MF_DAILY_LIMIT = 100
BATCH_CHILD_TRANSIENT_CURL_ERROR_CODES = {5, 6, 7, 28, 35, 52, 55, 56, 92}
BATCH_CHILD_TRANSIENT_FAILURE_TOKENS = (
    "could not resolve host",
    "name or service not known",
    "ns_error_unknown_host",
    "connection timed out",
    "operation timed out",
    "failed to connect",
    "connection reset",
    "connection refused",
    "temporarily unavailable",
    "timed out",
)
CLOAKBROWSER_AUTO_RECOVERY_COOLDOWN_SECONDS = 10 * 60
CLOAKBROWSER_BROWSER_CONFIRMATION_MESSAGE = (
    "指纹浏览器登录态已同步，但 MakerWorld 仍拒绝 3MF 下载；请在官网完成验证后再继续归档。"
)
CLOAKBROWSER_SESSION_REFRESHED_MESSAGE = "指纹浏览器登录态已更新，正在重试当前受阻的 3MF 下载。"


def _archive_worker_concurrency(config: Any = None) -> int:
    runtime_config = getattr(config, "runtime", None)
    configured = getattr(runtime_config, "worker_concurrency", None)
    raw = str(configured if configured is not None else os.environ.get("MAKERHUB_WORKER_CONCURRENCY") or "2").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 2
    return max(1, min(value, 4))


def _normalize_three_mf_daily_limit(value: Any, fallback: int = DEFAULT_THREE_MF_DAILY_LIMIT) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(0, limit)


def _meta_bool(meta: dict[str, Any], key: str, default: bool) -> bool:
    if key not in meta:
        return default
    value = meta.get(key)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def detect_archive_mode(url: str) -> str:
    lowered = (url or "").lower()
    path = urlparse(url or "").path or ""
    if "/collections/models" in lowered or COLLECTION_DETAIL_RE.search(path):
        return "collection_models"
    if "/upload" in lowered and "/@" in lowered:
        return "author_upload"
    if "/models/" in lowered:
        return "single_model"
    return "unknown"


def _select_cookie(url: str, config) -> str:
    platform = normalize_makerworld_source(url=url) or "cn"
    return _select_cookie_for_platform(platform, config)


def _select_cookie_for_platform(platform: str, config) -> str:
    clean_platform = normalize_makerworld_source(source=platform) or "cn"
    cookie_map = {
        str(getattr(item, "platform", "") or ""): getattr(item, "cookie", "")
        for item in getattr(config, "cookies", []) or []
    }
    return sanitize_cookie_header(cookie_map.get(clean_platform) or "")


def _is_cookie_stale_for_platform(store: Any, platform: str, job_cookie: str) -> bool:
    clean_platform = normalize_makerworld_source(source=platform) or "cn"
    clean_job_cookie = sanitize_cookie_header(job_cookie)
    if not clean_job_cookie:
        return False
    try:
        current_config = store.load()
        current_cookie = _select_cookie_for_platform(clean_platform, current_config)
    except Exception as exc:
        _log_archive(
            "cookie_staleness_check_failed",
            "检查 Cookie 是否已更新失败，继续按当前任务结果同步状态。",
            level="warning",
            platform=clean_platform,
            error=str(exc)[:240],
        )
        return False
    return bool(current_cookie and current_cookie != clean_job_cookie)


def _three_mf_daily_limits(config) -> tuple[int, int]:
    limits = getattr(config, "three_mf_limits", None)
    cn_limit = _normalize_three_mf_daily_limit(
        getattr(limits, "cn_daily_limit", DEFAULT_THREE_MF_DAILY_LIMIT)
    )
    global_limit = _normalize_three_mf_daily_limit(
        getattr(limits, "global_daily_limit", DEFAULT_THREE_MF_DAILY_LIMIT)
    )
    return cn_limit, global_limit


def _task_key(url: str) -> str:
    model_id = extract_model_id(url)
    if model_id:
        return f"model:{model_id}"
    return normalize_source_url(url)


def _source_item_url(item: Any) -> str:
    if isinstance(item, dict):
        return normalize_source_url(str(item.get("url") or ""))
    return normalize_source_url(str(item or ""))


def _missing_3mf_fallback_base(source: str) -> str:
    return "https://makerworld.com" if normalize_makerworld_source(source=source) == "global" else "https://makerworld.com.cn"


def _resolve_archive_result_model_dir(result: dict[str, Any]) -> str:
    work_dir = str(result.get("work_dir") or "").strip()
    if not work_dir:
        return ""
    try:
        return Path(work_dir).resolve().relative_to(ARCHIVE_DIR.resolve()).as_posix()
    except ValueError:
        return ""


def _queue_item_key(item: dict) -> str:
    return _task_key(item.get("url") or item.get("title") or "")


def _queue_missing_3mf_retry_key(url: str, meta: Optional[dict[str, Any]] = None) -> str:
    meta = meta if isinstance(meta, dict) else {}
    model_id = str(meta.get("model_id") or extract_model_id(url) or "").strip()
    if not model_id:
        return ""
    return f"missing_3mf_retry:model:{model_id}"


def _queue_item_missing_3mf_retry_key(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    if not meta.get("missing_3mf_retry"):
        return ""
    return _queue_missing_3mf_retry_key(item.get("url") or item.get("title") or "", meta)


def _is_batch_parent_waiting_for_children(item: dict[str, Any]) -> bool:
    if str(item.get("mode") or "") not in BATCH_TASK_MODES:
        return False
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    return bool(meta.get("batch_expected_items"))


def _is_transient_batch_child_failure(message: str) -> bool:
    lowered = str(message or "").strip().lower()
    if not lowered:
        return False

    for match in re.finditer(r"code=(\d+)", lowered):
        try:
            if int(match.group(1)) in BATCH_CHILD_TRANSIENT_CURL_ERROR_CODES:
                return True
        except ValueError:
            continue

    return any(token in lowered for token in BATCH_CHILD_TRANSIENT_FAILURE_TOKENS)


def _failure_message_from_queue_item(item: Optional[dict[str, Any]]) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("message") or item.get("detail") or "").strip()


def _clean_instance_ids(value: Any) -> list[str]:
    return [
        str(item or "").strip()
        for item in (value or [])
        if str(item or "").strip()
    ]


def _is_three_mf_failure_item(item: Optional[dict[str, Any]]) -> bool:
    if not isinstance(item, dict):
        return False
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    return bool(meta.get("three_mf_download") or meta.get("missing_3mf_retry"))


def _is_three_mf_only_task(item: Optional[dict[str, Any]]) -> bool:
    return _is_three_mf_failure_item(item)


def _append_batch_queue_log(event: str, **payload: Any) -> None:
    append_structured_log(
        BATCH_QUEUE_LOG_PATH.name,
        event,
        category="archive",
        time_text=china_now().isoformat(),
        **payload,
    )


def _log_archive(event: str, message: str = "", level: str = "info", **payload: Any) -> None:
    append_business_log("archive", event, message, level=level, **payload)


def _three_mf_task_completion(result_name: str, missing_items: list[dict[str, Any]]) -> tuple[str, str]:
    if missing_items:
        return (
            "three_mf_download_incomplete",
            f"新增 3MF 下载未完成：{result_name}，仍缺 {len(missing_items)} 个 3MF。",
        )
    return "three_mf_download_completed", f"新增 3MF 下载完成：{result_name}"


def _is_not_found_archive_error(message: Any, url: str = "") -> bool:
    return normalize_three_mf_failure_state("", message, url=url) == "not_found"


def _base_three_mf_limit_guard() -> dict[str, Any]:
    return {
        "active": False,
        "limited_until": "",
        "last_hit_at": "",
        "message": "",
        "reason": "",
        "model_id": "",
        "model_url": "",
        "instance_id": "",
    }


def _three_mf_limit_now(reference: Optional[datetime] = None) -> datetime:
    return china_now()


def _parse_three_mf_limit_time(value: str) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    return parse_datetime(raw)


def _write_three_mf_limit_guard(payload: dict[str, Any]) -> dict[str, Any]:
    ensure_app_dirs()
    current = _base_three_mf_limit_guard()
    current.update(load_database_json_state(THREE_MF_LIMIT_GUARD_KEY, current))

    current.update(
        {
            "active": bool(payload.get("active", current.get("active"))),
            "limited_until": str(payload.get("limited_until", current.get("limited_until")) or ""),
            "last_hit_at": str(payload.get("last_hit_at", current.get("last_hit_at")) or ""),
            "message": str(payload.get("message", current.get("message")) or ""),
            "reason": str(payload.get("reason", current.get("reason")) or ""),
            "model_id": str(payload.get("model_id", current.get("model_id")) or ""),
            "model_url": str(payload.get("model_url", current.get("model_url")) or ""),
            "instance_id": str(payload.get("instance_id", current.get("instance_id")) or ""),
        }
    )
    return save_database_json_state(THREE_MF_LIMIT_GUARD_KEY, current)


def _read_three_mf_limit_guard() -> dict[str, Any]:
    ensure_app_dirs()
    state = _base_three_mf_limit_guard()
    state.update(load_database_json_state(THREE_MF_LIMIT_GUARD_KEY, state))

    if bool(state.get("active")):
        limited_until = str(state.get("limited_until") or "").strip()
        parsed_until = _parse_three_mf_limit_time(limited_until)
        if parsed_until is None:
            return _write_three_mf_limit_guard({"active": False, "limited_until": "", "message": "", "reason": ""})
        if parsed_until <= _three_mf_limit_now(parsed_until):
            return _write_three_mf_limit_guard({"active": False, "limited_until": "", "message": "", "reason": ""})
    return state


def _is_three_mf_limit_guard_active(state: Optional[dict[str, Any]] = None) -> bool:
    current = _read_three_mf_limit_guard() if state is None else state
    return bool(current.get("active"))


def _is_three_mf_limit_guard_active_for_url(url: str, state: Optional[dict[str, Any]] = None) -> bool:
    current = _read_three_mf_limit_guard() if state is None else state
    if not _is_three_mf_limit_guard_active(current):
        return False
    guard_source = normalize_makerworld_source(url=current.get("model_url"))
    target_source = normalize_makerworld_source(url=url)
    if not guard_source or not target_source:
        return True
    return guard_source == target_source


def _three_mf_limit_until() -> str:
    now = _three_mf_limit_now()
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def _three_mf_limit_message(state: Optional[dict[str, Any]] = None) -> str:
    current = _read_three_mf_limit_guard() if state is None else state
    base_message = str(current.get("message") or "").strip() or THREE_MF_LIMIT_DEFAULT_MESSAGE
    if "自动重试暂停至" in base_message:
        base_message = base_message.split("自动重试暂停至", 1)[0].rstrip("，,。 ")
    limited_until = str(current.get("limited_until") or "").strip()
    if not limited_until:
        return base_message
    try:
        parsed_until = _parse_three_mf_limit_time(limited_until)
        if parsed_until is None:
            return base_message
        until_text = parsed_until.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return base_message
    return f"{base_message.rstrip('。')}，自动重试暂停至 {until_text}。"


def _activate_three_mf_limit_guard(
    *,
    message: str = "",
    model_id: str = "",
    model_url: str = "",
    instance_id: str = "",
) -> dict[str, Any]:
    return _write_three_mf_limit_guard(
        {
            "active": True,
            "limited_until": _three_mf_limit_until(),
            "last_hit_at": _three_mf_limit_now().isoformat(timespec="seconds"),
            "message": str(message or "").strip() or THREE_MF_LIMIT_DEFAULT_MESSAGE,
            "reason": "download_limited",
            "model_id": str(model_id or "").strip(),
            "model_url": str(model_url or "").strip(),
            "instance_id": str(instance_id or "").strip(),
        }
    )


def _clear_three_mf_limit_guard_for_manual_retry(url: str = "") -> bool:
    guard_state = _read_three_mf_limit_guard()
    if not _is_three_mf_limit_guard_active(guard_state):
        return False
    if url and not _is_three_mf_limit_guard_active_for_url(url, guard_state):
        return False

    _write_three_mf_limit_guard(
        {
            "active": False,
            "limited_until": "",
            "message": "",
            "reason": "manual_missing_3mf_retry",
            "model_id": "",
            "model_url": "",
            "instance_id": "",
        }
    )
    append_business_log(
        "missing_3mf",
        "limit_guard_cleared_for_manual_retry",
        "手动重试缺失 3MF，已清除旧的每日上限暂停标记并重新检测。",
        model_url=normalize_source_url(url),
        previous_limited_until=guard_state.get("limited_until") or "",
    )
    return True


def _reset_three_mf_daily_quota_for_manual_retry(url: str = "") -> dict[str, Any]:
    result = reset_three_mf_daily_quota(url=url)
    if result.get("reset"):
        append_business_log(
            "missing_3mf",
            "daily_quota_reset_for_manual_retry",
            "手动重试缺失 3MF，已重置该站点今天的 MakerHub 自动下载计数。",
            model_url=normalize_source_url(url),
            source=result.get("source") or "",
            previous=result.get("previous") or {},
        )
    return result


def _missing_3mf_message_from_result(
    item: dict[str, Any],
    limit_guard: Optional[dict[str, Any]] = None,
    *,
    url: str = "",
) -> str:
    download_state = str(item.get("downloadState") or "").strip()
    download_message = str(item.get("downloadMessage") or "").strip()
    if download_state or download_message:
        return describe_three_mf_failure(
            download_state,
            download_message,
            url=url,
            limit_message=_three_mf_limit_message(limit_guard) if download_state == "download_limited" else "",
        )
    return "等待重新下载"


def _archive_stage_from_progress_payload(payload: dict[str, Any]) -> str:
    stage = str(payload.get("archive_stage") or payload.get("stage") or "").strip().lower()
    if stage:
        return stage
    message = str(payload.get("message") or "").strip().lower()
    try:
        percent = int(payload.get("percent") or 0)
    except (TypeError, ValueError):
        percent = 0
    if "附件" in message:
        return "attachments"
    if "评论" in message and "摘要、图片与评论整理完成" not in message:
        return "comments"
    if "3mf" in message or "实例" in message or "打印配置" in message:
        return "three_mf"
    if "归档目录" in message or "落盘" in message or "索引" in message or "元数据已生成" in message or "归档完成" in message:
        return "finalize"
    if "图片" in message or "摘要" in message or "头像" in message:
        return "media"
    if percent >= 78:
        return "finalize"
    if percent >= 55:
        return "three_mf"
    if percent >= 52:
        return "comments"
    if percent >= 50:
        return "attachments"
    if percent >= 40:
        return "media"
    return "metadata"


def _archive_stage_progress_from_payload(payload: dict[str, Any], stage: str) -> int:
    if payload.get("archive_stage_progress") is not None:
        try:
            return max(0, min(int(payload.get("archive_stage_progress") or 0), 100))
        except (TypeError, ValueError):
            return 0
    try:
        percent = max(0, min(int(payload.get("percent") or 0), 100))
    except (TypeError, ValueError):
        percent = 0
    ranges = {
        "metadata": (0, 40),
        "media": (40, 50),
        "attachments": (50, 52),
        "comments": (52, 55),
        "three_mf": (55, 78),
        "finalize": (78, 100),
    }
    start, end = ranges.get(stage, (0, 100))
    if percent <= start:
        return 0
    if percent >= end or end <= start:
        return 100
    return max(0, min(round((percent - start) * 100 / (end - start)), 100))


class _ArchiveProgressThrottle:
    def __init__(
        self,
        *,
        interval_seconds: float = 2.0,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.interval_seconds = max(float(interval_seconds or 0), 0.0)
        self._clock = clock or time.monotonic
        self._last_persisted_at: Optional[float] = None
        self._last_stage = ""

    def should_persist(self, *, percent: int, stage: str) -> bool:
        now = self._clock()
        clean_stage = str(stage or "").strip()
        should_persist = (
            self._last_persisted_at is None
            or int(percent or 0) >= 100
            or clean_stage != self._last_stage
            or now - self._last_persisted_at >= self.interval_seconds
        )
        if should_persist:
            self._last_persisted_at = now
            self._last_stage = clean_stage
        return should_persist


def _account_health_failure_from_missing_items(
    missing_items: list[dict[str, Any]],
) -> Optional[dict[str, str]]:
    for item in missing_items:
        status = str(item.get("status") or "").strip()
        if status in {"verification_required", "cloudflare", "auth_required", "cookie_invalid", "download_limited"}:
            return {
                "status": status,
                "detail": str(item.get("message") or "").strip(),
                "instance_id": str(item.get("instance_id") or "").strip(),
            }
    return None


def _preserve_browser_confirmation_gate(
    platform: str,
    classified_failure: Optional[dict[str, str]],
) -> Optional[dict[str, str]]:
    if (
        classified_failure is None
        or classified_failure.get("status") not in {"auth_required", "cookie_invalid"}
    ):
        return classified_failure
    try:
        current = get_account_health(platform)
    except Exception:
        return classified_failure
    if (
        str(current.get("three_mf_gate") or "").strip().lower() != "verification_required"
        or str(current.get("three_mf_reason") or "").strip() != "browser_session_unchanged"
    ):
        return classified_failure
    return {
        **classified_failure,
        "status": "verification_required",
        "detail": CLOAKBROWSER_BROWSER_CONFIRMATION_MESSAGE,
    }


def _sync_account_health_for_archive_result(
    *,
    platform: str,
    model_url: str,
    model_id: str,
    instance_id: str,
    missing_items: list[dict[str, Any]],
    missing_3mf_retry: bool,
    browser_session_recovery: bool = False,
) -> Optional[dict[str, str]]:
    classified_failure = _account_health_failure_from_missing_items(missing_items)
    if (
        classified_failure is not None
        and browser_session_recovery
        and classified_failure["status"] in {"auth_required", "cookie_invalid"}
    ):
        classified_failure = {
            **classified_failure,
            "status": "verification_required",
            "detail": CLOAKBROWSER_BROWSER_CONFIRMATION_MESSAGE,
        }
    classified_failure = _preserve_browser_confirmation_gate(platform, classified_failure)
    try:
        if classified_failure is not None:
            update_three_mf_gate(
                platform,
                gate=classified_failure["status"],
                reason="three_mf_download_failed",
                source="archive_download",
                detail=classified_failure["detail"],
                model_url=model_url,
                model_id=model_id,
                instance_id=classified_failure["instance_id"] or instance_id,
            )
            return classified_failure
        elif missing_3mf_retry and not missing_items:
            mark_account_ok(
                platform,
                source="missing_3mf_retry" if missing_3mf_retry else "archive_download",
                model_url=model_url,
                model_id=model_id,
                instance_id=instance_id,
            )
    except Exception as exc:
        _log_archive(
            "account_health_sync_failed",
            "账号健康状态同步失败，归档结果已保留。",
            level="warning",
            model_id=model_id,
            url=model_url,
            error=str(exc)[:240],
        )
    return None


def _sync_account_health_for_archive_exception(
    *,
    task_meta: dict[str, Any],
    model_url: str,
    model_id: str,
    detail: str,
) -> None:
    state = normalize_three_mf_failure_state(
        "",
        detail,
        source=task_meta.get("source"),
        url=model_url,
    )
    if state not in {"verification_required", "cloudflare", "auth_required", "cookie_invalid", "download_limited"}:
        return

    platform = normalize_makerworld_source(task_meta.get("source"), model_url)
    normalized_failure = _preserve_browser_confirmation_gate(
        platform,
        {"status": state, "detail": detail, "instance_id": str(task_meta.get("instance_id") or "").strip()},
    )
    if normalized_failure is not None:
        state = normalized_failure["status"]
        detail = normalized_failure["detail"]
    try:
        update_three_mf_gate(
            platform,
            gate=state,
            reason="archive_task_failed",
            source="archive_task",
            detail=detail,
            model_url=model_url,
            model_id=model_id,
            instance_id=str(task_meta.get("instance_id") or "").strip(),
        )
    except Exception as exc:
        _log_archive(
            "account_health_sync_failed",
            "账号健康状态同步失败，归档失败状态已保留。",
            level="warning",
            model_id=model_id,
            url=model_url,
            error=str(exc)[:240],
        )


def three_mf_gate_for_url(url: str, meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    meta = meta if isinstance(meta, dict) else {}
    platform = normalize_makerworld_source(meta.get("source"), url)
    if platform not in {"cn", "global"}:
        return {"open": True, "state": "open", "message": "", "platform": platform}
    try:
        snapshot = get_account_health(platform)
    except Exception as exc:
        _log_archive(
            "three_mf_gate_read_failed",
            "读取平台 3MF 状态失败，本次不暂停 3MF 下载。",
            level="warning",
            url=url,
            platform=platform,
            error=str(exc)[:240],
        )
        return {"open": True, "state": "open", "message": "", "platform": platform}

    gate = str(snapshot.get("three_mf_gate") or "open").strip().lower()
    if gate in {"", "open", "ok"}:
        return {"open": True, "state": "open", "message": "", "platform": platform}

    if gate == "daily_limit":
        limit_guard = _read_three_mf_limit_guard()
        if not _is_three_mf_limit_guard_active_for_url(url, limit_guard):
            try:
                open_three_mf_gate(
                    platform,
                    source="three_mf_limit_guard",
                    detail="MakerWorld 每日下载上限暂停已过期，恢复 3MF 下载。",
                )
            except Exception as exc:
                _log_archive(
                    "three_mf_gate_open_failed",
                    "每日上限暂停已过期，但清理平台 3MF 状态失败，本次先恢复下载。",
                    level="warning",
                    url=url,
                    platform=platform,
                    error=str(exc)[:240],
                )
            return {"open": True, "state": "open", "message": "", "platform": platform}
        message = _three_mf_limit_message(limit_guard)
        return {"open": False, "state": "daily_limit", "message": message, "platform": platform}

    detail = str(snapshot.get("three_mf_detail") or snapshot.get("detail") or "").strip()
    message = detail or describe_three_mf_failure(gate, source=platform, url=url)
    return {"open": False, "state": gate, "message": message, "platform": platform}


def _three_mf_skip_state_from_gate(gate_state: Any) -> str:
    normalized = str(gate_state or "").strip().lower()
    if normalized == "daily_limit":
        return "download_limited"
    return normalized


@contextmanager
def _temporary_proxy_env(config, target_url: str = ""):
    with temporary_proxy_env(config, target_url):
        yield


class ArchiveTaskManager:
    def __init__(self, *, background_enabled: Optional[bool] = None) -> None:
        self.store = JsonStore()
        self.task_store = TaskStateStore()
        self.background_enabled = BACKGROUND_TASKS_ENABLED if background_enabled is None else bool(background_enabled)
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._workers: list[threading.Thread] = []
        self._worker_sequence = 0
        self._worker_target_count = 0
        self._preview_lock = threading.Lock()
        self._batch_refresh_lock = threading.Lock()
        self._batch_previews: dict[str, dict] = {}
        self._last_pending_maintenance_at = 0.0
        self._blocked_queue_retry_at = 0.0
        self._cloakbrowser_recovery_lock = threading.Lock()
        self._cloakbrowser_recovery_attempted_at: dict[str, float] = {}

    def _persist_browser_recovery_session(
        self,
        platform: str,
        *,
        expected_cookie: str,
        cookie: str,
        profile_id: str,
        status: str,
        message: str,
    ) -> object | None:
        try:
            config = self.store.load()
            current = next((item for item in config.cookies if item.platform == platform), None)
        except Exception as exc:
            _log_archive(
                "cloakbrowser_auto_sync_config_read_failed",
                "读取账号配置失败，未写入指纹浏览器登录态。",
                level="warning",
                platform=platform,
                error=str(exc)[:240],
            )
            return None
        if current is None:
            return None

        current_cookie = sanitize_cookie_header(current.cookie)
        if current_cookie != sanitize_cookie_header(expected_cookie):
            return None

        next_cookie = sanitize_cookie_header(cookie) or current_cookie
        changed = next_cookie != current_cookie
        timestamp = china_now().isoformat()
        next_pair = current.model_copy(
            update={
                "cookie": next_cookie,
                "browser_profile_id": str(profile_id or current.browser_profile_id),
                "browser_status": str(status or "").strip(),
                "browser_message": str(message or "").strip()[:400],
                "browser_synced_at": timestamp,
                "updated_at": timestamp,
                "last_login_at": timestamp if changed else current.last_login_at,
            }
        )
        config.cookies = [next_pair if item.platform == platform else item for item in config.cookies]
        try:
            saved = self.store.save(config)
        except Exception as exc:
            _log_archive(
                "cloakbrowser_auto_sync_config_save_failed",
                "保存指纹浏览器登录态失败。",
                level="warning",
                platform=platform,
                error=str(exc)[:240],
            )
            return None
        publish_state_event(
            "online_accounts",
            "state.changed",
            {"platform": platform, "status": next_pair.browser_status},
        )
        return next((item for item in saved.cookies if item.platform == platform), next_pair)

    def _recover_browser_session_for_three_mf_gate(self, platform: str, *, primary: Optional[dict] = None) -> dict[str, str]:
        normalized_platform = normalize_makerworld_source(platform) or str(platform or "").strip().lower()
        if normalized_platform not in {"cn", "global"}:
            return {"outcome": "skipped", "message": "未识别 MakerWorld 平台。"}
        if not cloakbrowser_configured():
            return {"outcome": "skipped", "message": "指纹浏览器未配置。"}

        try:
            config = self.store.load()
            current = next((item for item in config.cookies if item.platform == normalized_platform), None)
        except Exception as exc:
            return {"outcome": "error", "message": str(exc)[:240]}
        if current is None or not sanitize_cookie_header(current.cookie):
            return {"outcome": "skipped", "message": "当前平台没有可检查的 Cookie。"}
        if not current.browser_profile_id:
            return {"outcome": "skipped", "message": "当前平台未关联指纹浏览器 profile。"}

        expected_cookie = sanitize_cookie_header(current.cookie)
        try:
            browser_result = collect_browser_session(normalized_platform, current.browser_profile_id)
        except CloakBrowserError as exc:
            self._persist_browser_recovery_session(
                normalized_platform,
                expected_cookie=expected_cookie,
                cookie=expected_cookie,
                profile_id=current.browser_profile_id,
                status="action_required",
                message=f"自动读取指纹浏览器登录态失败：{str(exc)[:240]}",
            )
            return {"outcome": "error", "message": str(exc)[:240]}

        browser_cookie = sanitize_cookie_header(browser_result.cookie)
        browser_token = extract_auth_token(browser_cookie)
        current_token = extract_auth_token(expected_cookie)
        if not browser_token:
            self._persist_browser_recovery_session(
                normalized_platform,
                expected_cookie=expected_cookie,
                cookie=expected_cookie,
                profile_id=browser_result.profile_id,
                status="action_required",
                message="指纹浏览器尚未登录 MakerWorld，已保留 MakerHub 当前登录态。",
            )
            return {"outcome": "not_logged_in", "message": "指纹浏览器未发现 MakerWorld 登录 token。"}
        if not current_token or not hmac.compare_digest(current_token, browser_token):
            self._persist_browser_recovery_session(
                normalized_platform,
                expected_cookie=expected_cookie,
                cookie=expected_cookie,
                profile_id=browser_result.profile_id,
                status="account_mismatch",
                message="指纹浏览器登录态与 MakerHub 当前账号不一致，已阻止自动覆盖。",
            )
            return {"outcome": "account_mismatch", "message": "浏览器 token 与当前账号不一致。"}

        if browser_cookie == expected_cookie:
            self._persist_browser_recovery_session(
                normalized_platform,
                expected_cookie=expected_cookie,
                cookie=expected_cookie,
                profile_id=browser_result.profile_id,
                status="synced",
                message="指纹浏览器登录态已同步，但 3MF 下载仍被拒绝。",
            )
            update_three_mf_gate(
                normalized_platform,
                gate="verification_required",
                reason="browser_session_unchanged",
                source="cloakbrowser_auto_sync",
                detail=CLOAKBROWSER_BROWSER_CONFIRMATION_MESSAGE,
            )
            append_business_log(
                "settings",
                "cloakbrowser_auto_sync_unchanged",
                "指纹浏览器登录态与 MakerHub 一致，3MF 授权仍被拒绝。",
                level="warning",
                platform=normalized_platform,
                profile_id=browser_result.profile_id,
            )
            return {"outcome": "unchanged", "message": CLOAKBROWSER_BROWSER_CONFIRMATION_MESSAGE}

        saved = self._persist_browser_recovery_session(
            normalized_platform,
            expected_cookie=expected_cookie,
            cookie=browser_cookie,
            profile_id=browser_result.profile_id,
            status="synced",
            message="指纹浏览器登录态已自动同步。",
        )
        if saved is None:
            return {"outcome": "stale", "message": "MakerHub 登录态已变化，跳过浏览器同步结果。"}

        try:
            mark_account_checking(
                normalized_platform,
                source="cloakbrowser_auto_sync",
                detail=CLOAKBROWSER_SESSION_REFRESHED_MESSAGE,
            )
        except Exception as exc:
            _log_archive(
                "cloakbrowser_auto_sync_health_update_failed",
                "指纹浏览器登录态已更新，但账号检测状态写入失败。",
                level="warning",
                platform=normalized_platform,
                error=str(exc)[:240],
            )

        primary_item = primary if isinstance(primary, dict) else {}
        primary_url = normalize_source_url(str(primary_item.get("model_url") or ""))
        resumed = False
        if primary_url:
            resumed = self._resume_browser_session_recovery_task(
                model_url=primary_url,
                model_id=str(primary_item.get("model_id") or extract_model_id(primary_url) or ""),
                source=normalized_platform,
                title=str(primary_item.get("title") or ""),
                instance_id=str(primary_item.get("instance_id") or ""),
            )
        if primary_url and not resumed:
            self.retry_missing_3mf(
                model_url=primary_url,
                model_id=str(primary_item.get("model_id") or extract_model_id(primary_url) or ""),
                source=normalized_platform,
                title=str(primary_item.get("title") or ""),
                instance_id=str(primary_item.get("instance_id") or ""),
                browser_session_recovery=True,
            )
        append_business_log(
            "settings",
            "cloakbrowser_auto_sync_updated",
            "指纹浏览器登录态已更新，已安排当前受阻 3MF 重试。",
            platform=normalized_platform,
            profile_id=browser_result.profile_id,
            retried=bool(primary_url),
        )
        return {"outcome": "updated", "message": CLOAKBROWSER_SESSION_REFRESHED_MESSAGE}

    def _resume_browser_session_recovery_task(
        self,
        *,
        model_url: str,
        model_id: str,
        source: str,
        title: str,
        instance_id: str,
    ) -> bool:
        resume_task = getattr(self.task_store, "resume_browser_session_recovery_task", None)
        if not callable(resume_task):
            return False
        result = resume_task(
            model_id=str(model_id or "").strip(),
            model_url=normalize_source_url(model_url),
            platform=normalize_makerworld_source(source, model_url),
        )
        if int(result.get("resumed_count") or 0) <= 0:
            return False
        update_status = getattr(self.task_store, "update_missing_3mf_status", None)
        if callable(update_status):
            update_status(
                model_id=str(model_id or "").strip(),
                title=title,
                instance_id=instance_id,
                model_url=normalize_source_url(model_url),
                status="queued",
                message="正在使用最新浏览器登录态验证 3MF 下载权限",
            )
        self._ensure_worker()
        return True

    def _schedule_browser_session_recovery_for_three_mf_gate(
        self,
        platform: str,
        *,
        primary: Optional[dict] = None,
    ) -> bool:
        normalized_platform = normalize_makerworld_source(platform) or str(platform or "").strip().lower()
        if normalized_platform not in {"cn", "global"} or not self.background_enabled:
            return False
        now = time.monotonic()
        with self._cloakbrowser_recovery_lock:
            previous = self._cloakbrowser_recovery_attempted_at.get(normalized_platform, 0.0)
            if now - previous < CLOAKBROWSER_AUTO_RECOVERY_COOLDOWN_SECONDS:
                return False
            self._cloakbrowser_recovery_attempted_at[normalized_platform] = now
        snapshot = dict(primary) if isinstance(primary, dict) else {}

        def _worker() -> None:
            try:
                self._recover_browser_session_for_three_mf_gate(normalized_platform, primary=snapshot)
            except Exception as exc:
                _log_archive(
                    "cloakbrowser_auto_sync_failed",
                    "自动读取指纹浏览器登录态失败。",
                    level="warning",
                    platform=normalized_platform,
                    error=str(exc)[:240],
                )

        threading.Thread(
            target=_worker,
            name=f"makerhub-cloakbrowser-recovery-{normalized_platform}",
            daemon=True,
        ).start()
        return True

    def submit(self, url: str, force: bool = False, preview_token: str = "", meta: Optional[dict] = None) -> dict:
        clean_url = normalize_source_url(url)
        if not clean_url:
            return {
                "accepted": False,
                "message": "请先输入归档链接。",
            }

        mode = detect_archive_mode(clean_url)
        if mode == "single_model":
            return self._submit_single(clean_url, force=force, meta=meta)
        if mode in {"author_upload", "collection_models"}:
            return self._submit_batch(clean_url, mode, preview_token=preview_token)
        return {
            "accepted": False,
            "message": "无法识别该链接类型，请输入单模型、作者上传页或收藏夹页面。",
            "mode": mode,
            "url": clean_url,
        }

    def preview(self, url: str) -> dict:
        clean_url = normalize_source_url(url)
        if not clean_url:
            return {
                "accepted": False,
                "message": "请先输入归档链接。",
            }

        mode = detect_archive_mode(clean_url)
        if mode == "single_model":
            return {
                "accepted": True,
                "mode": mode,
                "url": clean_url,
                "requires_confirmation": False,
                "message": "识别为单模型链接。",
            }
        if mode not in {"author_upload", "collection_models"}:
            return {
                "accepted": False,
                "message": "无法识别该链接类型，请输入单模型、作者上传页或收藏夹页面。",
                "mode": mode,
                "url": clean_url,
            }
        return self._preview_batch(clean_url, mode)

    def _queued_task_keys(self) -> set[str]:
        queue = self.task_store.load_archive_queue()
        items = (queue.get("active") or []) + (queue.get("queued") or [])
        return {_queue_item_key(item) for item in items if item.get("url") or item.get("title")}

    def _queued_missing_3mf_retry_keys(self) -> set[str]:
        queue = self.task_store.load_archive_queue()
        items = (queue.get("active") or []) + (queue.get("queued") or [])
        return {
            key
            for key in (_queue_item_missing_3mf_retry_key(item) for item in items)
            if key
        }

    def _archived_task_keys(self) -> set[str]:
        snapshot = get_archive_snapshot()
        return set(snapshot.get("archived_keys") or [])

    def _deleted_task_lookup(self) -> dict[str, dict[str, str]]:
        deleted_dirs = set(self.task_store.load_model_flags().get("deleted") or [])
        if not deleted_dirs:
            return {}

        snapshot = get_archive_snapshot()
        lookup: dict[str, dict[str, str]] = {}
        for item in snapshot.get("models") or []:
            if str(item.get("source") or "").strip().lower() == "local":
                continue
            model_dir = str(item.get("model_dir") or "").strip().strip("/")
            if not model_dir or model_dir not in deleted_dirs:
                continue
            payload = {
                "model_dir": model_dir,
                "title": str(item.get("title") or model_dir),
            }
            model_id = str(item.get("id") or "").strip()
            if model_id:
                lookup[f"model:{model_id}"] = payload
            origin_url = normalize_source_url(str(item.get("origin_url") or ""))
            if origin_url:
                lookup[origin_url] = payload
        return lookup

    def _queue_state_snapshot(self) -> dict[str, Any]:
        queue = self.task_store.load_archive_queue()
        active = list(queue.get("active") or [])
        queued = list(queue.get("queued") or [])
        recent_failures = list(queue.get("recent_failures") or [])
        return {
            "queue": queue,
            "active": active,
            "queued": queued,
            "recent_failures": recent_failures,
            "active_by_key": {
                _queue_item_key(item): item
                for item in active
                if _queue_item_key(item)
            },
            "queued_by_key": {
                _queue_item_key(item): item
                for item in queued
                if _queue_item_key(item)
            },
            "failed_by_key": {
                _queue_item_key(item): item
                for item in recent_failures
                if _queue_item_key(item)
            },
            "active_missing_3mf_retry_by_key": {
                _queue_item_missing_3mf_retry_key(item): item
                for item in active
                if _queue_item_missing_3mf_retry_key(item)
            },
            "queued_missing_3mf_retry_by_key": {
                _queue_item_missing_3mf_retry_key(item): item
                for item in queued
                if _queue_item_missing_3mf_retry_key(item)
            },
        }

    def _normalize_batch_expected_items(self, items: Any) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for raw in items or []:
            if isinstance(raw, str):
                url = normalize_source_url(raw)
                key = _task_key(url)
                normalized.append(
                    {
                        "url": url,
                        "task_key": key,
                        "model_id": extract_model_id(url),
                        "attempts": 1,
                        "status": "",
                        "last_task_id": "",
                        "last_failure_message": "",
                    }
                )
                continue

            if not isinstance(raw, dict):
                continue

            url = normalize_source_url(str(raw.get("url") or ""))
            key = str(raw.get("task_key") or "").strip() or _task_key(url)
            normalized.append(
                {
                    "url": url,
                    "task_key": key,
                    "model_id": str(raw.get("model_id") or extract_model_id(url) or "").strip(),
                    "attempts": max(int(raw.get("attempts") or 1), 1),
                    "status": str(raw.get("status") or "").strip(),
                    "last_task_id": str(raw.get("last_task_id") or raw.get("child_task_id") or "").strip(),
                    "last_failure_message": str(raw.get("last_failure_message") or "").strip()[:400],
                }
            )
        return normalized

    def _requeue_batch_child(
        self,
        *,
        batch_id: str,
        batch_url: str,
        item: dict[str, Any],
        attempts: int,
        retry_limit: int,
        transient: bool = False,
        failure_message: str = "",
    ) -> str:
        if transient:
            self.task_store.remove_recent_failures_for_model(
                str(item.get("model_id") or ""),
                str(item.get("url") or ""),
            )

        child_task_id = self._enqueue_single_task(
            item.get("url") or "",
            message=f"批量任务补回：{batch_url}",
            mode="single_model",
            meta={
                "batch_parent_id": batch_id,
                "batch_source_url": batch_url,
                "batch_requeued": True,
            },
        )
        item["attempts"] = attempts + 1
        item["last_task_id"] = child_task_id
        item["status"] = "queued"
        if failure_message:
            item["last_failure_message"] = failure_message[:400]
        _append_batch_queue_log(
            "child_requeued",
            batch_task_id=batch_id,
            batch_url=batch_url,
            child_task_id=child_task_id,
            model_url=item.get("url") or "",
            model_id=item.get("model_id") or "",
            task_key=item.get("task_key") or "",
            attempts=item["attempts"],
            retry_limit=retry_limit,
            transient=transient,
            failure_message=(failure_message or "")[:400],
        )
        return child_task_id

    def _batch_parent_key(self, item: dict[str, Any]) -> tuple[str, str]:
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        if not meta.get("batch_expected_items"):
            return ("", "")

        source_url = normalize_source_url(str(item.get("url") or item.get("title") or ""))
        if not source_url:
            return ("", "")

        mode = str(item.get("mode") or "").strip() or detect_archive_mode(source_url)
        if mode not in BATCH_TASK_MODES:
            mode = detect_archive_mode(source_url)
        if mode not in BATCH_TASK_MODES:
            return ("", "")
        return (mode, source_url)

    def _expected_item_key(self, item: dict[str, Any]) -> str:
        return str(item.get("task_key") or "").strip() or _task_key(str(item.get("url") or ""))

    def _expected_item_from_child(self, item: dict[str, Any], fallback_status: str) -> Optional[dict[str, Any]]:
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        child_url = normalize_source_url(str(item.get("url") or item.get("title") or ""))
        key = str(meta.get("batch_task_key") or "").strip() or _task_key(child_url)
        if not child_url or not key:
            return None
        try:
            attempts = max(int(meta.get("batch_attempts") or item.get("attempt_count") or item.get("attempts") or 1), 1)
        except (TypeError, ValueError):
            attempts = 1
        return {
            "url": child_url,
            "task_key": key,
            "model_id": str(meta.get("model_id") or extract_model_id(child_url) or "").strip(),
            "attempts": attempts,
            "status": str(item.get("status") or fallback_status).strip() or fallback_status,
            "last_task_id": str(item.get("id") or "").strip(),
            "last_failure_message": _failure_message_from_queue_item(item)[:400],
        }

    def _merge_batch_expected_items(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for candidate in candidates:
            normalized_items = self._normalize_batch_expected_items([candidate])
            if not normalized_items:
                continue
            normalized = normalized_items[0]
            key = self._expected_item_key(normalized)
            if not key:
                continue
            if key not in merged:
                order.append(key)
            merged[key] = {**merged.get(key, {}), **normalized}
        return [merged[key] for key in order]

    def _merge_duplicate_batch_parents(self) -> int:
        queue = self.task_store.load_archive_queue()
        active_items = list(queue.get("active") or [])
        queued_items = list(queue.get("queued") or [])
        failed_items = list(queue.get("recent_failures") or [])

        kept_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        duplicate_parent_ids: dict[str, str] = {}
        duplicate_expected: dict[tuple[str, str], list[dict[str, Any]]] = {}

        def _collect_parent(item: dict[str, Any]) -> bool:
            key = self._batch_parent_key(item)
            if not all(key):
                return False

            kept = kept_by_key.get(key)
            if kept is None:
                kept_by_key[key] = item
                return False

            duplicate_id = str(item.get("id") or "").strip()
            kept_id = str(kept.get("id") or "").strip()
            if duplicate_id and kept_id:
                duplicate_parent_ids[duplicate_id] = kept_id

            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            duplicate_expected.setdefault(key, []).extend(self._normalize_batch_expected_items(meta.get("batch_expected_items")))
            return True

        duplicate_ids: set[str] = set()
        for section in (active_items, queued_items, failed_items):
            for item in section:
                if isinstance(item, dict) and _collect_parent(item):
                    duplicate_id = str(item.get("id") or "").strip()
                    if duplicate_id:
                        duplicate_ids.add(duplicate_id)

        if not duplicate_ids and not duplicate_parent_ids:
            return 0

        def _rewrite_child(item: dict[str, Any], fallback_status: str) -> tuple[dict[str, Any], bool]:
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            batch_source_url = normalize_source_url(str(meta.get("batch_source_url") or ""))
            if not batch_source_url:
                return item, False

            mode = detect_archive_mode(batch_source_url)
            if mode not in BATCH_TASK_MODES:
                mode = "author_upload"
            parent = kept_by_key.get((mode, batch_source_url))
            if not parent:
                return item, False

            kept_id = str(parent.get("id") or "").strip()
            if not kept_id:
                return item, False

            current_parent_id = str(meta.get("batch_parent_id") or "").strip()
            if current_parent_id == kept_id:
                return item, False

            updated = dict(item)
            updated_meta = dict(meta)
            updated_meta["batch_parent_id"] = kept_id
            updated["meta"] = updated_meta
            expected = self._expected_item_from_child(updated, fallback_status)
            if expected:
                duplicate_expected.setdefault((mode, batch_source_url), []).append(expected)
            return updated, True

        changed = len(duplicate_ids)

        def _rewrite_section(items: list[dict[str, Any]], fallback_status: str) -> list[dict[str, Any]]:
            nonlocal changed
            rewritten = []
            for item in items:
                if not isinstance(item, dict):
                    rewritten.append(item)
                    continue
                if str(item.get("id") or "").strip() in duplicate_ids:
                    continue
                updated, did_change = _rewrite_child(item, fallback_status)
                if did_change:
                    changed += 1
                rewritten.append(updated)
            return rewritten

        active_items = _rewrite_section(active_items, "running")
        queued_items = _rewrite_section(queued_items, "queued")
        failed_items = _rewrite_section(failed_items, "failed")

        for key, parent in kept_by_key.items():
            additions = duplicate_expected.get(key) or []
            if not additions:
                continue
            meta = dict(parent.get("meta") or {})
            merged = self._merge_batch_expected_items(
                self._normalize_batch_expected_items(meta.get("batch_expected_items")) + additions
            )
            if merged == self._normalize_batch_expected_items(meta.get("batch_expected_items")):
                continue
            meta["batch_expected_items"] = merged
            parent["meta"] = meta
            parent["message"] = "批量任务已合并重复父任务，正在同步子任务状态。"
            parent["updated_at"] = china_now().isoformat()
            changed += 1

        if changed <= 0:
            return 0

        self.task_store.save_archive_queue(
            {
                "active": active_items,
                "queued": queued_items,
                "recent_failures": failed_items,
            }
        )
        return changed

    def _restore_queued_batch_parents(self) -> int:
        restored_count = 0

        def _mutate(payload: dict) -> dict:
            nonlocal restored_count
            active_items = [dict(item) for item in (payload.get("active") or []) if isinstance(item, dict)]
            active_ids = {str(item.get("id") or "").strip() for item in active_items}
            queued_items = []
            now_text = china_now().isoformat()

            for raw_item in payload.get("queued") or []:
                if not isinstance(raw_item, dict):
                    continue
                item = dict(raw_item)
                meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
                task_id = str(item.get("id") or "").strip()
                mode = str(item.get("mode") or "").strip()
                expected_items = self._normalize_batch_expected_items(meta.get("batch_expected_items"))
                if task_id in active_ids or mode not in BATCH_TASK_MODES or not expected_items:
                    queued_items.append(item)
                    continue

                item["status"] = "waiting_children"
                item["message"] = "服务启动后恢复批量子任务状态跟踪。"
                item["updated_at"] = now_text
                active_items.append(item)
                active_ids.add(task_id)
                restored_count += 1

            payload["active"] = active_items
            payload["queued"] = queued_items
            return payload

        self.task_store._update_archive_queue(_mutate)
        return restored_count

    def _restore_orphaned_batch_parents(self) -> int:
        restored_count = 0

        def _mutate(payload: dict) -> dict:
            nonlocal restored_count
            active_items = list(payload.get("active") or [])
            queued_items = list(payload.get("queued") or [])
            failed_items = list(payload.get("recent_failures") or [])
            existing_task_ids = {
                str(item.get("id") or "").strip()
                for item in active_items + queued_items
                if str(item.get("id") or "").strip()
            }

            groups: dict[str, dict[str, Any]] = {}
            for section, status in (
                (active_items, "running"),
                (queued_items, "queued"),
                (failed_items, "failed"),
            ):
                for child in section:
                    meta = child.get("meta") if isinstance(child.get("meta"), dict) else {}
                    batch_parent_id = str(meta.get("batch_parent_id") or "").strip()
                    if not batch_parent_id or batch_parent_id in existing_task_ids:
                        continue

                    batch_source_url = normalize_source_url(str(meta.get("batch_source_url") or ""))
                    if not batch_source_url:
                        continue

                    group = groups.setdefault(
                        batch_parent_id,
                        {
                            "batch_url": batch_source_url,
                            "children": [],
                        },
                    )
                    if not group.get("batch_url"):
                        group["batch_url"] = batch_source_url

                    child_url = normalize_source_url(str(child.get("url") or child.get("title") or ""))
                    key = str(meta.get("batch_task_key") or "").strip() or _task_key(child_url)
                    if not child_url or not key:
                        continue
                    try:
                        attempts = max(int(meta.get("batch_attempts") or 1), 1)
                    except (TypeError, ValueError):
                        attempts = 1

                    group["children"].append(
                        {
                            "url": child_url,
                            "task_key": key,
                            "model_id": str(meta.get("model_id") or extract_model_id(child_url) or "").strip(),
                            "attempts": attempts,
                            "status": status,
                            "last_task_id": str(child.get("id") or "").strip(),
                            "last_failure_message": _failure_message_from_queue_item(child)[:400],
                        }
                    )

            restored_parents: list[dict[str, Any]] = []
            now_text = china_now().isoformat()
            for batch_parent_id, group in groups.items():
                children = group.get("children") or []
                if not children:
                    continue
                if not any(item.get("status") in {"queued", "running"} for item in children):
                    continue

                batch_url = normalize_source_url(str(group.get("batch_url") or ""))
                mode = detect_archive_mode(batch_url)
                if mode not in BATCH_TASK_MODES:
                    mode = "author_upload"

                restored_parents.append(
                    {
                        "id": batch_parent_id,
                        "url": batch_url,
                        "title": batch_url,
                        "mode": mode,
                        "status": "running",
                        "progress": 60,
                        "message": "批量任务已恢复，正在同步子任务状态。",
                        "updated_at": now_text,
                        "meta": {
                            "batch_expected_items": children,
                            "batch_restored": True,
                            "batch_restored_at": now_text,
                            "batch_progress": {
                                "total": len(children),
                                "completed": 0,
                                "failed": sum(1 for item in children if item.get("status") == "failed"),
                                "running": sum(1 for item in children if item.get("status") == "running"),
                                "queued": sum(1 for item in children if item.get("status") == "queued"),
                                "remaining": len(children),
                            },
                        },
                    }
                )
                _append_batch_queue_log(
                    "batch_parent_restored",
                    batch_task_id=batch_parent_id,
                    batch_url=batch_url,
                    total=len(children),
                    running=sum(1 for item in children if item.get("status") == "running"),
                    queued=sum(1 for item in children if item.get("status") == "queued"),
                    failed=sum(1 for item in children if item.get("status") == "failed"),
                )
                _log_archive(
                    "batch_parent_restored",
                    "检测到批量子任务仍在队列中，已恢复批量父任务进度跟踪。",
                    task_id=batch_parent_id,
                    url=batch_url,
                    total=len(children),
                )

            if not restored_parents:
                restored_count = 0
                return payload

            restored_count = len(restored_parents)
            payload["active"] = restored_parents + active_items
            restored_parent_ids = {str(item.get("id") or "") for item in restored_parents}
            payload["recent_failures"] = [
                item
                for item in failed_items
                if str(item.get("id") or "") not in restored_parent_ids
            ]
            return payload

        self.task_store._update_archive_queue(_mutate)
        return restored_count

    def _refresh_batch_tasks(self) -> bool:
        with self._batch_refresh_lock:
            return self._refresh_batch_tasks_locked()

    def _refresh_batch_tasks_locked(self) -> bool:
        queued_parent_count = self._restore_queued_batch_parents()
        restored_count = self._restore_orphaned_batch_parents()
        merged_count = self._merge_duplicate_batch_parents()
        snapshot = self._queue_state_snapshot()
        active_items = snapshot["active"]
        batch_tasks = [
            item
            for item in active_items
            if str(item.get("mode") or "") in BATCH_TASK_MODES
            and self._normalize_batch_expected_items((item.get("meta") or {}).get("batch_expected_items"))
        ]
        if not batch_tasks:
            return queued_parent_count > 0 or restored_count > 0 or merged_count > 0

        active_batch_ids = {str(item.get("id") or "") for item in batch_tasks}
        active_child_by_key = {
            _queue_item_key(item): item
            for item in active_items
            if str(item.get("id") or "") not in active_batch_ids
            and not _is_three_mf_only_task(item)
            and _queue_item_key(item)
        }
        queued_by_key = {
            _queue_item_key(item): item
            for item in snapshot["queued"]
            if not _is_three_mf_only_task(item) and _queue_item_key(item)
        }
        failed_by_key = {
            _queue_item_key(item): item
            for item in snapshot["recent_failures"]
            if not _is_three_mf_only_task(item) and _queue_item_key(item)
        }
        archived_keys = self._archived_task_keys()

        for batch_task in batch_tasks:
            batch_id = str(batch_task.get("id") or "")
            batch_url = str(batch_task.get("url") or "")
            meta = dict(batch_task.get("meta") or {})
            expected_items = self._normalize_batch_expected_items(meta.get("batch_expected_items"))
            if not expected_items:
                continue
            previous_batch_progress = (
                dict(meta.get("batch_progress") or {})
                if isinstance(meta.get("batch_progress"), dict)
                else {}
            )

            completed_count = 0
            running_count = 0
            queued_count = 0
            failed_count = 0
            updated = False

            for item in expected_items:
                key = str(item.get("task_key") or "").strip() or _task_key(item.get("url") or "")
                item["task_key"] = key
                status = str(item.get("status") or "").strip()

                if key in active_child_by_key:
                    if status != "running":
                        item["status"] = "running"
                        updated = True
                    running_count += 1
                    continue

                if key in queued_by_key:
                    if status != "queued":
                        item["status"] = "queued"
                        updated = True
                    queued_count += 1
                    continue

                failed_item = failed_by_key.get(key)
                if failed_item is not None and (key not in archived_keys or _is_three_mf_failure_item(failed_item)):
                    failure_message = _failure_message_from_queue_item(failed_by_key.get(key))
                    if failure_message and item.get("last_failure_message") != failure_message[:400]:
                        item["last_failure_message"] = failure_message[:400]
                        updated = True
                    attempts = max(int(item.get("attempts") or 1), 1)
                    if (
                        _is_transient_batch_child_failure(failure_message)
                        and attempts < MAX_BATCH_CHILD_TRANSIENT_REQUEUE_ATTEMPTS
                    ):
                        child_task_id = self._requeue_batch_child(
                            batch_id=batch_id,
                            batch_url=batch_url,
                            item=item,
                            attempts=attempts,
                            retry_limit=MAX_BATCH_CHILD_TRANSIENT_REQUEUE_ATTEMPTS,
                            transient=True,
                            failure_message=failure_message,
                        )
                        queued_by_key[key] = {"id": child_task_id, "url": item.get("url") or ""}
                        queued_count += 1
                        updated = True
                        continue
                    if status != "failed":
                        item["status"] = "failed"
                        updated = True
                    failed_count += 1
                    continue

                if key in archived_keys:
                    if status != "archived":
                        item["status"] = "archived"
                        updated = True
                    completed_count += 1
                    continue

                attempts = max(int(item.get("attempts") or 1), 1)
                if attempts < MAX_BATCH_CHILD_REQUEUE_ATTEMPTS:
                    child_task_id = self._requeue_batch_child(
                        batch_id=batch_id,
                        batch_url=batch_url,
                        item=item,
                        attempts=attempts,
                        retry_limit=MAX_BATCH_CHILD_REQUEUE_ATTEMPTS,
                    )
                    queued_by_key[key] = {"id": child_task_id, "url": item.get("url") or ""}
                    queued_count += 1
                    updated = True
                    continue

                failure_message = str(item.get("last_failure_message") or "").strip()
                if (
                    _is_transient_batch_child_failure(failure_message)
                    and attempts < MAX_BATCH_CHILD_TRANSIENT_REQUEUE_ATTEMPTS
                ):
                    child_task_id = self._requeue_batch_child(
                        batch_id=batch_id,
                        batch_url=batch_url,
                        item=item,
                        attempts=attempts,
                        retry_limit=MAX_BATCH_CHILD_TRANSIENT_REQUEUE_ATTEMPTS,
                        transient=True,
                        failure_message=failure_message,
                    )
                    queued_by_key[key] = {"id": child_task_id, "url": item.get("url") or ""}
                    queued_count += 1
                    updated = True
                    continue

                if status != "failed":
                    item["status"] = "failed"
                    updated = True
                    _append_batch_queue_log(
                        "child_lost",
                        batch_task_id=batch_id,
                        batch_url=batch_url,
                        model_url=item.get("url") or "",
                        model_id=item.get("model_id") or "",
                        task_key=key,
                        attempts=attempts,
                    )
                failed_count += 1

            total_expected = len(expected_items)
            if total_expected <= 0:
                continue

            done_count = completed_count + failed_count
            remaining_count = max(total_expected - done_count, 0)
            progress = 60 + int((done_count / total_expected) * 40)

            meta["batch_expected_items"] = expected_items
            meta["batch_progress"] = {
                "total": total_expected,
                "completed": completed_count,
                "failed": failed_count,
                "running": running_count,
                "queued": queued_count,
                "remaining": remaining_count,
            }
            progress_changed = previous_batch_progress != meta["batch_progress"]

            if done_count >= total_expected and running_count == 0 and queued_count == 0:
                summary = (
                    f"批量归档完成：成功 {completed_count} 个，失败 {failed_count} 个。"
                )
                self.task_store.complete_archive_task(
                    batch_id,
                    progress=100,
                    message=summary,
                    meta=meta,
                )
                _append_batch_queue_log(
                    "batch_completed",
                    batch_task_id=batch_id,
                    batch_url=batch_url,
                    completed=completed_count,
                    failed=failed_count,
                    total=total_expected,
                )
                continue

            message = (
                f"批量归档执行中：成功 {completed_count}/{total_expected}，"
                f"运行中 {running_count}，排队中 {queued_count}，失败 {failed_count}。"
            )
            if updated or progress_changed or str(batch_task.get("message") or "") != message:
                self.task_store.update_active_task(
                    batch_id,
                    progress=min(progress, 99),
                    message=message,
                    meta=meta,
                )

        return True

    def _prune_batch_previews(self) -> None:
        cutoff = time.time() - 15 * 60
        with self._preview_lock:
            expired_keys = [
                key
                for key, item in self._batch_previews.items()
                if float(item.get("created_at") or 0) < cutoff
            ]
            for key in expired_keys:
                self._batch_previews.pop(key, None)

    def _store_batch_preview(self, clean_url: str, mode: str, discovered: dict) -> str:
        self._prune_batch_previews()
        preview_token = uuid.uuid4().hex
        payload = {
            "created_at": time.time(),
            "url": clean_url,
            "mode": mode,
            "discovered_items": [_source_item_url(item) for item in discovered.get("items") or [] if _source_item_url(item)],
            "expected_total": discovered.get("expected_total"),
            "pages_scanned": discovered.get("pages_scanned"),
            "scan_mode": discovered.get("mode") or "",
            "source_name": str(discovered.get("source_name") or "").strip(),
        }
        with self._preview_lock:
            self._batch_previews[preview_token] = payload
        return preview_token

    def _consume_batch_preview(self, preview_token: str, clean_url: str, mode: str) -> Optional[dict]:
        if not preview_token:
            return None
        self._prune_batch_previews()
        with self._preview_lock:
            preview = self._batch_previews.pop(preview_token, None)
        if not preview:
            return None
        if preview.get("url") != clean_url or preview.get("mode") != mode:
            return None
        return preview

    def peek_batch_preview(self, preview_token: str, url: str, mode: Optional[str] = None) -> Optional[dict]:
        clean_url = normalize_source_url(url)
        clean_mode = mode or detect_archive_mode(clean_url)
        if not preview_token or clean_mode not in BATCH_TASK_MODES:
            return None
        self._prune_batch_previews()
        with self._preview_lock:
            preview = self._batch_previews.get(preview_token)
            if not preview:
                return None
            if preview.get("url") != clean_url or preview.get("mode") != clean_mode:
                return None
            return dict(preview)

    def _enqueue_single_task_with_queue(
        self,
        url: str,
        message: str = "等待归档",
        mode: str = "",
        meta: Optional[dict] = None,
    ) -> tuple[str, dict]:
        task = self._new_archive_queue_item(url, message=message, mode=mode, meta=meta)
        task_id = str(task["id"])
        queue = self.task_store.enqueue_archive_task(task)
        self._blocked_queue_retry_at = 0.0
        queue = queue if isinstance(queue, dict) else {}
        if queue.get("enqueued") is False and str(queue.get("existing_task_id") or "").strip():
            return str(queue.get("existing_task_id") or "").strip(), queue
        return task_id, queue

    @staticmethod
    def _new_archive_queue_item(
        url: str,
        *,
        message: str = "等待归档",
        mode: str = "",
        meta: Optional[dict] = None,
    ) -> dict[str, Any]:
        return {
            "id": uuid.uuid4().hex,
            "url": url,
            "title": url,
            "mode": mode,
            "meta": meta or {},
            "status": "queued",
            "progress": 0,
            "message": message,
            "updated_at": china_now().isoformat(),
        }

    def _enqueue_single_task(self, url: str, message: str = "等待归档", mode: str = "", meta: Optional[dict] = None) -> str:
        task_id, _queue = self._enqueue_single_task_with_queue(url, message=message, mode=mode, meta=meta)
        return task_id

    def _enqueue_three_mf_stage_task_from_result(self, url: str, result: dict[str, Any], meta: dict[str, Any]) -> str:
        instances = result.get("instances") if isinstance(result.get("instances"), list) else []
        task_id = self._enqueue_single_task(
            normalize_source_url(url),
            message="等待下载 3MF",
            mode="single_model",
            meta={
                "three_mf_download": True,
                "download_assets": False,
                "download_comment_assets": False,
                "collect_comments_data": False,
                "model_id": str(result.get("model_id") or meta.get("model_id") or extract_model_id(url) or "").strip(),
                "model_url": normalize_source_url(url),
                "title": str(result.get("base_name") or meta.get("title") or "").strip(),
                "instance_ids": [
                    str(item.get("id") or item.get("profileId") or item.get("instanceId") or "").strip()
                    for item in instances
                    if isinstance(item, dict)
                    and str(item.get("id") or item.get("profileId") or item.get("instanceId") or "").strip()
                ],
            },
        )
        _log_archive(
            "three_mf_stage_submitted",
            "3MF 下载子任务已入队。",
            url=normalize_source_url(url),
            task_id=task_id,
            model_id=str(result.get("model_id") or ""),
            instances=len(instances),
        )
        return task_id

    def _submit_single(self, clean_url: str, force: bool = False, meta: Optional[dict] = None) -> dict:
        task_key = _task_key(clean_url)
        task_meta = dict(meta or {})
        missing_retry_key = (
            _queue_missing_3mf_retry_key(clean_url, task_meta)
            if force or task_meta.get("missing_3mf_retry")
            else ""
        )
        deleted_item = self._deleted_task_lookup().get(task_key)
        if deleted_item is not None:
            message = f"该模型已在 MakerHub 端删除，默认不会再次归档：{deleted_item.get('title') or clean_url}"
            _log_archive(
                "single_submit_skipped",
                message,
                url=clean_url,
                task_key=task_key,
                model_dir=deleted_item.get("model_dir") or "",
            )
            return {
                "accepted": False,
                "message": message,
                "mode": "single_model",
                "url": clean_url,
            }
        limit_guard = _read_three_mf_limit_guard()
        if force and _is_three_mf_limit_guard_active_for_url(clean_url, limit_guard):
            return {
                "accepted": False,
                "message": _three_mf_limit_message(limit_guard),
                "mode": "single_model",
                "url": clean_url,
            }
        queued_duplicate = False if missing_retry_key else task_key in self._queued_task_keys()
        if queued_duplicate:
            _log_archive("single_submit_skipped", "单模型已在归档队列中。", url=clean_url, task_key=task_key)
            return {
                "accepted": False,
                "message": "该模型已经在归档队列中。",
                "mode": "single_model",
                "url": clean_url,
            }
        if not force and task_key in self._archived_task_keys():
            _log_archive("single_submit_skipped", "单模型已归档，跳过重复提交。", url=clean_url, task_key=task_key)
            return {
                "accepted": False,
                "message": "该模型已归档，无需重复加入。",
                "mode": "single_model",
                "url": clean_url,
            }

        queue_message = "等待归档" if not force else "等待重新下载缺失 3MF"
        if force:
            task_meta.setdefault("missing_3mf_retry", True)
            instance_ids = _clean_instance_ids(task_meta.get("instance_ids"))
            instance_id = str(task_meta.get("instance_id") or "").strip()
            if instance_id and instance_id not in instance_ids:
                instance_ids.append(instance_id)
            if instance_ids:
                task_meta["instance_ids"] = instance_ids
        task_id, enqueue_queue = self._enqueue_single_task_with_queue(
            clean_url,
            message=queue_message,
            mode="single_model",
            meta=task_meta,
        )
        self._ensure_worker()
        task_merged = bool(enqueue_queue.get("merged"))
        task_enqueued = bool(enqueue_queue.get("enqueued", True))
        existing_task_id = str(enqueue_queue.get("existing_task_id") or task_id).strip()
        if force and not task_enqueued:
            log_message = (
                "缺失 3MF 重试已在队列中，已合并缺失实例。"
                if task_merged
                else "缺失 3MF 重试已在队列中。"
            )
            _log_archive(
                "single_submit_skipped",
                log_message,
                url=clean_url,
                task_id=existing_task_id,
                existing_task_id=existing_task_id,
                task_key=task_key,
                force=force,
                merged=task_merged,
                enqueued=task_enqueued,
            )
            if task_merged:
                return {
                    "accepted": False,
                    "merged": True,
                    "queued": True,
                    "task_id": existing_task_id,
                    "mode": "single_model",
                    "url": clean_url,
                    "message": "该模型的缺失 3MF 重试已在队列中，已合并缺失实例。",
                }
            return {
                "accepted": False,
                "merged": False,
                "queued": True,
                "task_id": existing_task_id,
                "mode": "single_model",
                "url": clean_url,
                "message": "该模型的缺失 3MF 重试已在队列中。",
            }
        _log_archive(
            "single_submitted",
            "单模型归档任务已入队。" if not force else "缺失 3MF 重新下载任务已入队。",
            url=clean_url,
            task_id=task_id,
            task_key=task_key,
            force=force,
            merged=task_merged,
            enqueued=task_enqueued,
        )
        return {
            "accepted": True,
            "task_id": task_id,
            "merged": task_merged,
            "existing_task_id": existing_task_id if task_merged else "",
            "mode": "single_model",
            "url": clean_url,
            "message": "归档任务已加入队列。" if not force else "缺失 3MF 重新下载任务已加入队列。",
        }

    def submit_three_mf_download(self, url: str, model_id: str = "", title: str = "", instance_ids: Optional[list[str]] = None) -> dict:
        clean_url = normalize_source_url(url)
        if not clean_url:
            return {
                "accepted": False,
                "message": "缺少可用模型链接，无法下载新增 3MF。",
            }

        task_key = _task_key(clean_url)
        deleted_item = self._deleted_task_lookup().get(task_key)
        if deleted_item is not None:
            message = f"该模型已在 MakerHub 端删除，不会自动下载新增 3MF：{deleted_item.get('title') or clean_url}"
            _log_archive(
                "three_mf_download_skipped_deleted",
                message,
                url=clean_url,
                task_key=task_key,
                model_dir=deleted_item.get("model_dir") or "",
            )
            return {
                "accepted": False,
                "message": message,
                "mode": "single_model",
                "url": clean_url,
            }

        queue_snapshot = self._queue_state_snapshot()
        queued_match = (
            queue_snapshot.get("active_by_key", {}).get(task_key)
            or queue_snapshot.get("queued_by_key", {}).get(task_key)
        )
        if queued_match:
            _log_archive("three_mf_download_skipped_queued", "新增 3MF 下载任务已在归档队列中。", url=clean_url, task_key=task_key)
            return {
                "accepted": False,
                "queued": True,
                "message": "该模型已经在归档队列中，等待队列任务下载新增 3MF。",
                "mode": "single_model",
                "url": clean_url,
            }

        limit_guard = _read_three_mf_limit_guard()
        if _is_three_mf_limit_guard_active_for_url(clean_url, limit_guard):
            return {
                "accepted": False,
                "paused": True,
                "message": _three_mf_limit_message(limit_guard),
                "mode": "single_model",
                "url": clean_url,
            }

        platform = normalize_makerworld_source(url=clean_url)
        platform_gate = three_mf_gate_for_url(clean_url, {"source": platform})
        if not bool(platform_gate.get("open")):
            return {
                "accepted": False,
                "paused": True,
                "state": platform_gate.get("state") or "",
                "message": str(platform_gate.get("message") or "当前平台 3MF 下载暂停，等待账号状态恢复。"),
                "mode": "single_model",
                "url": clean_url,
            }

        clean_instance_ids = _clean_instance_ids(instance_ids)
        task_id = self._enqueue_single_task(
            clean_url,
            message="等待下载新增 3MF",
            mode="single_model",
            meta={
                "three_mf_download": True,
                "download_assets": False,
                "download_comment_assets": False,
                "collect_comments_data": False,
                "model_id": str(model_id or extract_model_id(clean_url) or "").strip(),
                "model_url": clean_url,
                "title": str(title or "").strip(),
                "instance_ids": clean_instance_ids,
            },
        )
        self._ensure_worker()
        _log_archive(
            "three_mf_download_submitted",
            "新增 3MF 下载任务已入队。",
            url=clean_url,
            task_id=task_id,
            task_key=task_key,
            model_id=model_id,
            instance_ids=clean_instance_ids,
        )
        return {
            "accepted": True,
            "task_id": task_id,
            "mode": "single_model",
            "url": clean_url,
            "message": "新增 3MF 下载任务已加入队列。",
        }

    def _is_missing_3mf_retry_task(self, item: dict) -> bool:
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        if meta.get("missing_3mf_retry"):
            return True
        message = str(item.get("message") or "")
        return "缺失 3MF" in message

    def _pause_missing_3mf_retry_tasks_for_limit(self, state: Optional[dict[str, Any]] = None) -> int:
        guard_state = _read_three_mf_limit_guard() if state is None else state
        if not _is_three_mf_limit_guard_active(guard_state):
            return 0

        message = _three_mf_limit_message(guard_state)
        queue = self.task_store.load_archive_queue()
        queued_items = list(queue.get("queued") or [])
        kept_items: list[dict] = []
        paused_items: list[dict] = []

        for item in queued_items:
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            model_url = normalize_source_url(
                str(meta.get("model_url") or item.get("url") or "")
            )
            if (
                self._is_missing_3mf_retry_task(item)
                and _is_three_mf_limit_guard_active_for_url(model_url, guard_state)
            ):
                paused_items.append(item)
            else:
                kept_items.append(item)

        if not paused_items:
            return 0

        queue["queued"] = kept_items
        self.task_store.save_archive_queue(queue)

        for item in paused_items:
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            model_url = normalize_source_url(
                str(meta.get("model_url") or item.get("url") or "")
            )
            model_id = str(meta.get("model_id") or extract_model_id(model_url) or "").strip()
            self.task_store.update_missing_3mf_status(
                model_id=model_id,
                title=str(meta.get("title") or ""),
                instance_id=str(meta.get("instance_id") or ""),
                model_url=model_url,
                status="missing",
                message=message,
            )

        append_business_log(
            "missing_3mf",
            "retry_queue_paused_daily_limit",
            message,
            paused_count=len(paused_items),
            limited_until=guard_state.get("limited_until") or "",
        )
        return len(paused_items)

    def _pause_three_mf_retry_tasks_for_gate(
        self,
        *,
        platform: str,
        state: str,
        message: str,
    ) -> int:
        normalized_platform = normalize_makerworld_source(platform) or str(platform or "").strip().lower()
        if normalized_platform not in {"cn", "global"}:
            return 0
        if not hasattr(self.task_store, "load_archive_queue") or not hasattr(self.task_store, "save_archive_queue"):
            return 0

        queue = self.task_store.load_archive_queue()
        queued_items = list(queue.get("queued") or [])
        paused_count = 0
        pause_message = str(message or "").strip() or describe_three_mf_failure(
            state,
            source=normalized_platform,
        )
        now = china_now().isoformat()

        for item in queued_items:
            status = str(item.get("status") or "queued").strip().lower()
            if status not in {"", "queued", "pending"}:
                continue
            if not _is_three_mf_only_task(item):
                continue

            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            item_url = normalize_source_url(str(meta.get("model_url") or item.get("url") or ""))
            item_platform = normalize_makerworld_source(meta.get("source"), item_url)
            if item_platform != normalized_platform:
                continue

            item["status"] = "paused"
            item["blocked_reason"] = "needs_verification"
            item["message"] = pause_message
            item["updated_at"] = now
            paused_count += 1

        if not paused_count:
            return 0

        queue["queued"] = queued_items
        self.task_store.save_archive_queue(queue)
        if hasattr(self.task_store, "mark_missing_3mf_platform_status"):
            self.task_store.mark_missing_3mf_platform_status(
                normalized_platform,
                status=str(state or "verification_required").strip() or "verification_required",
                message=pause_message,
            )
        append_business_log(
            "missing_3mf",
            "retry_queue_paused_account_gate",
            pause_message,
            paused_count=paused_count,
            platform=normalized_platform,
            state=str(state or "").strip(),
        )
        return paused_count

    def retry_missing_3mf(
        self,
        model_url: str,
        model_id: str = "",
        source: str = "",
        title: str = "",
        instance_id: str = "",
        browser_session_recovery: bool = False,
    ) -> dict:
        clean_url = normalize_source_url(model_url)
        clean_model_id = str(model_id or "").strip()
        clean_source = normalize_makerworld_source(source, clean_url)
        if not clean_url and clean_model_id:
            clean_url = normalize_model_url(
                f"/zh/models/{clean_model_id}",
                fallback_base=_missing_3mf_fallback_base(clean_source),
            )
            clean_source = normalize_makerworld_source(clean_source, clean_url)
        if not clean_url:
            _log_archive("missing_retry_rejected", "缺少可用模型链接，无法重新下载 3MF。", level="warning")
            return {
                "accepted": False,
                "message": "缺少可用模型链接，无法重新下载 3MF。",
            }

        config = self.store.load()
        if not _select_cookie(clean_url, config):
            message = "未找到可用 Cookie，请先到设置页配置对应站点 Cookie。"
            _log_archive(
                "missing_retry_rejected",
                message,
                level="warning",
                model_id=clean_model_id,
                model_url=clean_url,
                instance_id=instance_id,
            )
            self.task_store.update_missing_3mf_status(
                model_id=clean_model_id,
                title="" if clean_model_id else title,
                instance_id="" if clean_model_id else instance_id,
                model_url="" if clean_model_id else clean_url,
                status="missing",
                message=message,
            )
            return {
                "accepted": False,
                "message": message,
            }

        _clear_three_mf_limit_guard_for_manual_retry(clean_url)
        _reset_three_mf_daily_quota_for_manual_retry(clean_url)

        self.task_store.update_missing_3mf_status(
            model_id=clean_model_id,
            title="" if clean_model_id else title,
            instance_id="" if clean_model_id else instance_id,
            model_url="" if clean_model_id else clean_url,
            status="queued",
            message="等待重新下载 3MF",
        )

        result = self.submit(
            clean_url,
            force=True,
            meta={
                "missing_3mf_retry": True,
                "download_assets": False,
                "download_comment_assets": False,
                "collect_comments_data": False,
                "model_id": clean_model_id or extract_model_id(clean_url),
                "model_url": clean_url,
                "source": clean_source,
                "title": title,
                "instance_id": instance_id,
                "browser_session_recovery": bool(browser_session_recovery),
            },
        )
        if result.get("accepted"):
            self.task_store.update_missing_3mf_status(
                model_id=clean_model_id,
                title="" if clean_model_id else title,
                instance_id="" if clean_model_id else instance_id,
                model_url="" if clean_model_id else clean_url,
                status="queued",
                message="已加入重新下载队列",
            )
            return result

        if result.get("merged"):
            self.task_store.update_missing_3mf_status(
                model_id=clean_model_id,
                title="" if clean_model_id else title,
                instance_id="" if clean_model_id else instance_id,
                model_url="" if clean_model_id else clean_url,
                status="queued",
                message="已合并到重新下载队列",
            )
            return result

        if result.get("queued"):
            self.task_store.update_missing_3mf_status(
                model_id=clean_model_id,
                title="" if clean_model_id else title,
                instance_id="" if clean_model_id else instance_id,
                model_url="" if clean_model_id else clean_url,
                status="queued",
                message="已存在于重新下载队列",
            )
            return result

        message = str(result.get("message") or "")
        if "已经在归档队列中" in message:
            self.task_store.update_missing_3mf_status(
                model_id=clean_model_id,
                title="" if clean_model_id else title,
                instance_id="" if clean_model_id else instance_id,
                model_url="" if clean_model_id else clean_url,
                status="queued",
                message="已存在于归档队列",
            )
        elif message:
            self.task_store.update_missing_3mf_status(
                model_id=clean_model_id,
                title="" if clean_model_id else title,
                instance_id="" if clean_model_id else instance_id,
                model_url="" if clean_model_id else clean_url,
                status="missing",
                message=message,
            )
        return result

    def retry_verification_missing_3mf(
        self,
        *,
        platform: str,
        primary: Optional[dict] = None,
        retry_all: bool = True,
    ) -> dict:
        normalized_platform = normalize_makerworld_source(platform) or str(platform or "").strip().lower()
        self._blocked_queue_retry_at = 0.0
        primary_item = primary if isinstance(primary, dict) else {}
        verification_states = {"verification_required", "cloudflare", "auth_required", "cookie_invalid"}
        missing_payload = self.task_store.load_missing_3mf()
        candidates: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()

        def _candidate_key(item: dict[str, Any]) -> tuple[str, str, str]:
            return (
                str(item.get("model_id") or "").strip(),
                str(item.get("model_url") or "").strip(),
                normalize_makerworld_source(item.get("source"), item.get("model_url")),
            )

        def _append_candidate(raw_item: Any) -> bool:
            if not isinstance(raw_item, dict):
                return False
            item_url = normalize_source_url(str(raw_item.get("model_url") or ""))
            item_platform = normalize_makerworld_source(raw_item.get("source"), item_url)
            if normalized_platform and item_platform and item_platform != normalized_platform:
                return False
            state = normalize_three_mf_failure_state(
                raw_item.get("status") or raw_item.get("downloadState") or "",
                raw_item.get("message") or raw_item.get("downloadMessage") or "",
                url=item_url,
            )
            if state not in verification_states:
                return False
            candidate = {
                "model_url": item_url,
                "model_id": str(raw_item.get("model_id") or raw_item.get("id") or extract_model_id(item_url) or "").strip(),
                "title": str(raw_item.get("title") or raw_item.get("name") or "").strip(),
                "instance_id": str(raw_item.get("instance_id") or raw_item.get("profileId") or raw_item.get("instanceId") or "").strip(),
                "source": item_platform,
            }
            key = _candidate_key(candidate)
            if key in seen:
                return False
            seen.add(key)
            candidates.append(candidate)
            return True

        if primary_item:
            _append_candidate(primary_item)
        if retry_all:
            for raw_item in missing_payload.get("items") or []:
                _append_candidate(raw_item)
        elif not candidates:
            fallback_priority = {
                "verification_required": 0,
                "cloudflare": 1,
                "auth_required": 2,
                "cookie_invalid": 3,
            }

            def _fallback_priority(raw_item: Any) -> int:
                if not isinstance(raw_item, dict):
                    return len(fallback_priority)
                item_url = normalize_source_url(str(raw_item.get("model_url") or ""))
                state = normalize_three_mf_failure_state(
                    raw_item.get("status") or raw_item.get("downloadState") or "",
                    raw_item.get("message") or raw_item.get("downloadMessage") or "",
                    url=item_url,
                )
                return fallback_priority.get(state, len(fallback_priority))

            for raw_item in sorted(missing_payload.get("items") or [], key=_fallback_priority):
                if _append_candidate(raw_item):
                    break

        if hasattr(self.task_store, "mark_missing_3mf_retrying"):
            self.task_store.mark_missing_3mf_retrying(
                candidates,
                status="queued",
                message="验证已完成，等待重新下载 3MF",
            )

        accepted = 0
        queued = 0
        failed = 0
        targeted_resumed = 0
        last_message = ""
        for item in candidates:
            result = self.retry_missing_3mf(
                model_url=item["model_url"],
                model_id=item["model_id"],
                source=normalize_makerworld_source(item.get("source"), item.get("model_url")),
                title=item["title"],
                instance_id=item["instance_id"],
                browser_session_recovery=True,
            )
            last_message = str(result.get("message") or "")
            if result.get("accepted"):
                accepted += 1
            elif result.get("queued") or result.get("merged") or "已经在归档队列中" in last_message:
                queued += 1
                if not retry_all and self._resume_browser_session_recovery_task(
                    model_url=item["model_url"],
                    model_id=item["model_id"],
                    source=normalize_makerworld_source(item.get("source"), item.get("model_url")),
                    title=item["title"],
                    instance_id=item["instance_id"],
                ):
                    targeted_resumed += 1
            else:
                failed += 1

        resumed = (
            self._resume_paused_missing_3mf_retry_tasks_for_platform(normalized_platform)
            if retry_all
            else targeted_resumed
        )
        append_business_log(
            "missing_3mf",
            "verification_retry_completed",
            "验证完成后已重试同平台验证类 3MF 任务。",
            platform=normalized_platform,
            total=len(candidates),
            accepted=accepted,
            queued=queued,
            failed=failed,
            resumed=resumed,
        )
        if resumed:
            self._ensure_worker()
        return {
            "accepted": accepted > 0 or queued > 0 or resumed > 0,
            "accepted_count": accepted,
            "queued_count": queued,
            "failed_count": failed,
            "resumed_count": resumed,
            "total_count": len(candidates),
            "message": (
                f"验证后重试完成：新增入队 {accepted} 个，已在队列 {queued} 个，恢复暂停 {resumed} 个，失败 {failed} 个。"
                if candidates or resumed
                else "当前没有同平台验证类 3MF 任务。"
            ),
            "last_message": last_message,
        }

    def _resume_paused_missing_3mf_retry_tasks_for_platform(self, platform: str) -> int:
        normalized_platform = normalize_makerworld_source(platform) or str(platform or "").strip().lower()
        if normalized_platform not in {"cn", "global"}:
            return 0
        if not hasattr(self.task_store, "load_archive_queue") or not hasattr(self.task_store, "save_archive_queue"):
            return 0

        queue = self.task_store.load_archive_queue()
        queued_items = list(queue.get("queued") or [])
        resumed = 0
        now = china_now().isoformat()
        for item in queued_items:
            if str(item.get("status") or "").strip().lower() != "paused":
                continue
            if not _is_three_mf_only_task(item):
                continue
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            item_url = normalize_source_url(str(meta.get("model_url") or item.get("url") or ""))
            item_platform = normalize_makerworld_source(meta.get("source"), item_url)
            if item_platform != normalized_platform:
                continue

            item["status"] = "queued"
            item["message"] = "验证已完成，等待重新下载 3MF"
            item["updated_at"] = now
            item["meta"] = {**meta, "browser_session_recovery": True}
            item.pop("blocked_reason", None)
            resumed += 1

        if not resumed:
            return 0

        queue["queued"] = queued_items
        self.task_store.save_archive_queue(queue)
        if hasattr(self.task_store, "mark_missing_3mf_platform_status"):
            self.task_store.mark_missing_3mf_platform_status(
                normalized_platform,
                status="queued",
                message="验证已完成，等待重新下载 3MF",
            )
        append_business_log(
            "missing_3mf",
            "paused_retry_queue_resumed",
            "验证完成后已恢复暂停的缺失 3MF 队列任务。",
            platform=normalized_platform,
            resumed_count=resumed,
        )
        return resumed

    def cancel_missing_3mf(self, model_id: str = "", model_url: str = "", title: str = "", instance_id: str = "") -> dict:
        clean_model_id = str(model_id or "").strip()
        clean_title = str(title or "").strip()
        clean_instance_id = str(instance_id or "").strip()
        clean_url = normalize_source_url(model_url)

        if not any((clean_model_id, clean_title, clean_instance_id, clean_url)):
            return {
                "success": False,
                "message": "缺少可识别的缺失 3MF 条目标识。",
            }

        before = self.task_store.load_missing_3mf()
        after = self.task_store.remove_missing_3mf_item(
            model_id=clean_model_id,
            title=clean_title,
            instance_id=clean_instance_id,
            model_url=clean_url,
        )
        removed = max(int(before.get("count") or 0) - int(after.get("count") or 0), 0)
        append_business_log(
            "missing_3mf",
            "cancelled",
            "缺失 3MF 任务已取消。" if removed else "没有找到可取消的缺失 3MF 任务。",
            model_id=clean_model_id,
            model_url=clean_url,
            instance_id=clean_instance_id,
            removed_count=removed,
        )
        return {
            "success": removed > 0,
            "removed_count": removed,
            "message": "已取消该缺失 3MF 任务。" if removed else "没有找到可取消的缺失 3MF 任务。",
        }

    def retry_all_missing_3mf(self) -> dict:
        missing_payload = self.task_store.load_missing_3mf()
        items = missing_payload.get("items") or []
        limit_guard = _read_three_mf_limit_guard()
        if _is_three_mf_limit_guard_active(limit_guard):
            append_business_log(
                "missing_3mf",
                "retry_all_limit_guard_cleared_for_manual_retry",
                "手动批量重试缺失 3MF，已清除旧的每日上限暂停标记并重新检测。",
                total=len(items),
                previous_limited_until=limit_guard.get("limited_until") or "",
            )
            _clear_three_mf_limit_guard_for_manual_retry()
        sources_reset: set[str] = set()
        for item in items:
            item_url = normalize_source_url(str(item.get("model_url") or ""))
            source = normalize_makerworld_source(url=item_url)
            if source in {"cn", "global"} and source not in sources_reset:
                _reset_three_mf_daily_quota_for_manual_retry(item_url)
                sources_reset.add(source)
        accepted = 0
        queued = 0
        failed = 0
        last_message = ""
        self.task_store.mark_missing_3mf_retrying(
            items,
            status="queued",
            message="等待重新下载 3MF",
        )

        grouped_items: list[dict[str, Any]] = []
        seen_group_keys: set[tuple[str, str, str]] = set()
        for item in items:
            item_url = normalize_source_url(str(item.get("model_url") or ""))
            item_model_id = str(item.get("model_id") or extract_model_id(item_url) or "").strip()
            item_source = normalize_makerworld_source(item.get("source"), item_url)
            group_key = (item_model_id, item_url, item_source)
            if group_key in seen_group_keys:
                continue
            seen_group_keys.add(group_key)
            grouped_items.append(
                {
                    "model_url": item_url,
                    "model_id": item_model_id,
                    "source": item_source,
                    "title": str(item.get("title") or ""),
                    "instance_id": "",
                }
            )

        for item in grouped_items:
            result = self.retry_missing_3mf(
                model_url=str(item.get("model_url") or ""),
                model_id=str(item.get("model_id") or ""),
                source=str(item.get("source") or ""),
                title=str(item.get("title") or ""),
                instance_id=str(item.get("instance_id") or ""),
            )
            last_message = str(result.get("message") or "")
            if result.get("accepted"):
                accepted += 1
                continue
            if result.get("queued") or result.get("merged") or "已经在归档队列中" in last_message:
                queued += 1
            else:
                failed += 1

        append_business_log(
            "missing_3mf",
            "retry_all_completed",
            "缺失 3MF 全部重试处理完成。",
            total=len(items),
            accepted=accepted,
            queued=queued,
            failed=failed,
        )
        return {
            "accepted": accepted > 0 or queued > 0,
            "accepted_count": accepted,
            "queued_count": queued,
            "failed_count": failed,
            "message": (
                f"缺失 3MF 重试完成：新增入队 {accepted} 个，已在队列 {queued} 个，失败 {failed} 个。"
                if items
                else "当前没有缺失 3MF 任务。"
            ),
        }

    def _preview_batch(self, clean_url: str, mode: str) -> dict:
        config = self.store.load()
        cookie = _select_cookie(clean_url, config)
        if not cookie:
            _log_archive("batch_preview_rejected", "批量预扫描缺少 Cookie。", level="warning", url=clean_url, mode=mode)
            return {
                "accepted": False,
                "message": "未找到可用 Cookie，请先到设置页配置对应站点 Cookie。",
                "mode": mode,
                "url": clean_url,
            }

        batch_task_key = _task_key(clean_url)
        if batch_task_key in self._queued_task_keys():
            _log_archive("batch_preview_skipped", "批量归档任务已在队列中。", url=clean_url, mode=mode)
            return {
                "accepted": False,
                "mode": mode,
                "url": clean_url,
                "message": "该批量归档任务已经在队列中。",
            }

        with _temporary_proxy_env(config, clean_url):
            _log_archive("batch_preview_started", "开始批量预扫描。", url=clean_url, mode=mode)
            discovered = run_discover_batch_urls_job(clean_url, cookie, proxy_config=config.proxy)
            discovered["source_name"] = resolve_batch_source_name(clean_url, cookie)

        discovered_items = [_source_item_url(item) for item in discovered.get("items") or [] if _source_item_url(item)]
        discovered_count = len(discovered_items)
        if discovered_count <= 0:
            _log_archive("batch_preview_empty", "批量预扫描没有发现可归档模型。", level="warning", url=clean_url, mode=mode)
            return {
                "accepted": False,
                "mode": mode,
                "url": clean_url,
                "message": "没有扫描到可归档模型，请检查链接或 Cookie 是否有效。",
            }

        pending_keys = self._queued_task_keys()
        archived_keys = self._archived_task_keys()
        queued_count = 0
        archived_count = 0
        new_count = 0
        for model_url in discovered_items:
            key = _task_key(model_url)
            if key in pending_keys:
                queued_count += 1
            elif key in archived_keys:
                archived_count += 1
            else:
                new_count += 1

        preview_token = self._store_batch_preview(clean_url, mode, discovered)
        expected_total = discovered.get("expected_total")
        total_hint = ""
        if isinstance(expected_total, int) and expected_total > discovered_count:
            total_hint = f"，站点标记总数 {expected_total}"

        _log_archive(
            "batch_preview_done",
            f"批量预扫描完成，发现 {discovered_count} 个模型。",
            url=clean_url,
            mode=mode,
            discovered_count=discovered_count,
            expected_total=expected_total,
            new_count=new_count,
            queued_count=queued_count,
            archived_count=archived_count,
            pages_scanned=discovered.get("pages_scanned"),
            scan_mode=discovered.get("mode") or "",
        )
        return {
            "accepted": True,
            "mode": mode,
            "url": clean_url,
            "requires_confirmation": True,
            "preview_token": preview_token,
            "discovered_count": discovered_count,
            "expected_total": expected_total,
            "queued_count": queued_count,
            "archived_count": archived_count,
            "new_count": new_count,
            "subscription_supported": mode in BATCH_TASK_MODES,
            "subscription_name": str(discovered.get("source_name") or "").strip(),
            "message": (
                f"本次扫描到 {discovered_count} 个模型{total_hint}。"
                f" 其中新增 {new_count} 个，已在队列 {queued_count} 个，已归档 {archived_count} 个。"
            ),
        }

    def _submit_batch(self, clean_url: str, mode: str, preview_token: str = "") -> dict:
        config = self.store.load()
        cookie = _select_cookie(clean_url, config)
        if not cookie:
            _log_archive("batch_submit_rejected", "批量提交缺少 Cookie。", level="warning", url=clean_url, mode=mode)
            return {
                "accepted": False,
                "message": "未找到可用 Cookie，请先到设置页配置对应站点 Cookie。",
                "mode": mode,
                "url": clean_url,
            }

        batch_task_key = _task_key(clean_url)
        if batch_task_key in self._queued_task_keys():
            _log_archive("batch_submit_skipped", "批量归档任务已在队列中。", url=clean_url, mode=mode)
            return {
                "accepted": False,
                "mode": mode,
                "url": clean_url,
                "message": "该批量归档任务已经在队列中。",
            }

        preview = self._consume_batch_preview(preview_token, clean_url, mode)
        if preview_token and preview is None:
            _log_archive("batch_submit_rejected", "批量预扫描结果已失效。", level="warning", url=clean_url, mode=mode)
            return {
                "accepted": False,
                "mode": mode,
                "url": clean_url,
                "message": "预扫描结果已失效，请重新扫描后再确认提交。",
            }

        preview_items = list((preview or {}).get("discovered_items") or [])
        preview_count = len(preview_items)
        task_message = "等待扫描模型链接"
        if preview_count > 0:
            task_message = f"已确认提交，等待批量入队（预扫描 {preview_count} 个模型）"

        task_id = self._enqueue_single_task(
            clean_url,
            message=task_message,
            mode=mode,
            meta={
                "discovered_items": preview_items,
                "expected_total": (preview or {}).get("expected_total"),
                "pages_scanned": (preview or {}).get("pages_scanned"),
                "scan_mode": (preview or {}).get("scan_mode") or "",
            },
        )
        self._ensure_worker()
        _log_archive(
            "batch_submitted",
            "批量归档任务已入队。",
            url=clean_url,
            mode=mode,
            task_id=task_id,
            preview_count=preview_count,
            expected_total=(preview or {}).get("expected_total"),
        )
        return {
            "accepted": True,
            "task_id": task_id,
            "mode": mode,
            "url": clean_url,
            "preview_count": preview_count,
            "message": (
                f"批量归档任务已加入队列，确认提交 {preview_count} 个预扫描模型。"
                if preview_count > 0
                else "批量归档任务已加入队列，后台正在扫描模型链接。"
            ),
        }

    def submit_discovered_batch(
        self,
        *,
        source_url: str,
        mode: str,
        discovered_items: list[str],
        expected_total: Any = None,
        pages_scanned: Any = None,
        scan_mode: str = "",
        message_prefix: str = "",
        meta: Optional[dict] = None,
    ) -> dict:
        clean_url = normalize_source_url(source_url)
        clean_mode = mode if mode in BATCH_TASK_MODES else detect_archive_mode(clean_url)
        if clean_mode not in BATCH_TASK_MODES:
            _log_archive("discovered_batch_rejected", "订阅批量入队只支持作者页或收藏夹。", level="warning", url=clean_url, mode=clean_mode)
            return {
                "accepted": False,
                "message": "仅作者上传页或收藏夹页面支持批量归档。",
                "mode": clean_mode,
                "url": clean_url,
            }

        normalized_items = [_source_item_url(item) for item in discovered_items or [] if _source_item_url(item)]
        if not normalized_items:
            _log_archive("discovered_batch_empty", "订阅同步没有新增模型需要入队。", url=clean_url, mode=clean_mode)
            return {
                "accepted": False,
                "message": "本次同步没有新增模型需要入队。",
                "mode": clean_mode,
                "url": clean_url,
                "queued_count": 0,
            }

        task_id = self._enqueue_single_task(
            clean_url,
            message=f"订阅同步发现 {len(normalized_items)} 个新增模型，等待批量入队",
            mode=clean_mode,
            meta={
                "discovered_items": normalized_items,
                "expected_total": expected_total,
                "pages_scanned": pages_scanned,
                "scan_mode": scan_mode or "subscription",
                "child_queue_message_prefix": message_prefix or f"来自批量归档：{clean_url}",
                **(meta or {}),
            },
        )
        self._ensure_worker()
        _log_archive(
            "discovered_batch_submitted",
            f"订阅同步发现 {len(normalized_items)} 个新增模型，已创建批量任务。",
            url=clean_url,
            mode=clean_mode,
            task_id=task_id,
            queued_count=len(normalized_items),
            expected_total=expected_total,
            pages_scanned=pages_scanned,
            scan_mode=scan_mode,
        )
        return {
            "accepted": True,
            "task_id": task_id,
            "mode": clean_mode,
            "url": clean_url,
            "queued_count": len(normalized_items),
            "message": f"已把 {len(normalized_items)} 个新增模型加入归档队列。",
        }

    def _ensure_worker(self) -> None:
        if not self.background_enabled:
            return
        if time.monotonic() < self._blocked_queue_retry_at:
            return
        with self._lock:
            self._workers = [worker for worker in self._workers if worker.is_alive()]
            try:
                config = self.store.load()
            except Exception:
                config = None
            target_count = _archive_worker_concurrency(config)
            self._worker_target_count = target_count
            while len(self._workers) < target_count:
                self._worker_sequence += 1
                worker = threading.Thread(
                    target=self._run_loop,
                    daemon=True,
                    name=f"makerhub-archive-worker-{self._worker_sequence}",
                )
                worker.start()
                self._workers.append(worker)
            self._worker = self._workers[0] if self._workers else None

    def ensure_worker_for_pending(self) -> dict:
        if self._pending_maintenance_is_recent():
            queue = self._load_pending_queue_compact()
            if int(queue.get("queued_count") or 0) > 0:
                self._ensure_worker()
            return queue

        self._last_pending_maintenance_at = time.monotonic()
        queue = self._repair_queue_before_worker_start(repair_active=False)
        if hasattr(self.task_store, "resume_verification_paused_archive_tasks"):
            gate_by_platform = self._verification_resume_gate_snapshot(queue)
            self._schedule_browser_recovery_for_legacy_cookie_invalid_gates(queue, gate_by_platform)
        if int(queue.get("running_count") or 0) > 0:
            queue = self.task_store.refresh_recent_active_archive_leases()
        if int(queue.get("queued_count") or 0) > 0:
            self._ensure_worker()
        return queue

    def _pending_maintenance_is_recent(self) -> bool:
        return (
            self._last_pending_maintenance_at > 0
            and time.monotonic() - self._last_pending_maintenance_at < 10 * 60
        )

    def _load_pending_queue_compact(self) -> dict:
        loader = getattr(self.task_store, "load_archive_queue_compact", None)
        if callable(loader):
            try:
                return loader(item_limit=0)
            except Exception:
                pass
        return self.task_store.load_archive_queue()

    def _repair_queue_before_worker_start(self, *, repair_active: bool = False) -> dict:
        queue = self.task_store.load_archive_queue()
        if int(queue.get("queued_count") or 0) <= 0 and int(queue.get("running_count") or 0) <= 0:
            return queue
        if repair_active:
            result = self.task_store.repair_archive_queue()
            repaired_queue = result.get("queue") if isinstance(result, dict) else None
            queue = repaired_queue if isinstance(repaired_queue, dict) else self.task_store.load_archive_queue()
        else:
            result = self.task_store.repair_archive_queue(repair_active=False)
            repaired_queue = result.get("queue") if isinstance(result, dict) else None
            queue = repaired_queue if isinstance(repaired_queue, dict) else self.task_store.load_archive_queue()
        if int(queue.get("queued_count") or 0) > 0:
            summary = result.get("summary") if repair_active and isinstance(result, dict) else {}
            deduplicated = (
                int(summary.get("deduplicated") or 0)
                if isinstance(summary, dict)
                else int(result.get("deduplicated_count") or 0)
                if isinstance(result, dict)
                else 0
            )
            if deduplicated:
                _log_archive(
                    "archive_queue_repaired_before_worker_start",
                    "归档 worker 启动前已合并重复队列任务。",
                    deduplicated=deduplicated,
                    queued_count=int(queue.get("queued_count") or 0),
                )
        return queue

    def resume_pending_tasks(self) -> dict:
        queue = self.task_store.requeue_active_tasks()
        if int(queue.get("queued_count") or 0) > 0:
            queue = self._repair_queue_before_worker_start(repair_active=True)
        if (queue.get("queued_count") or 0) > 0:
            self._ensure_worker()
        self._last_pending_maintenance_at = time.monotonic()
        return queue

    def _next_executable_task(self, queue: dict) -> Optional[dict]:
        queued = list(queue.get("queued") or [])
        for item in queued:
            status = str(item.get("status") or "queued").strip().lower()
            if status not in {"", "queued", "pending"}:
                continue
            if _is_batch_parent_waiting_for_children(item):
                continue
            if self._is_three_mf_only_task_blocked_by_gate(item):
                continue
            return item
        for item in queued:
            status = str(item.get("status") or "queued").strip().lower()
            if status not in {"", "queued", "pending"}:
                continue
            if _is_batch_parent_waiting_for_children(item):
                continue
            if not _is_three_mf_only_task(item):
                return item
        return None

    def _is_three_mf_only_task_blocked_by_gate(self, item: dict) -> bool:
        if not _is_three_mf_only_task(item):
            return False
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        if bool(meta.get("browser_session_recovery")):
            return False
        url = normalize_source_url(str(meta.get("model_url") or item.get("url") or item.get("title") or ""))
        gate = three_mf_gate_for_url(url, meta)
        return not bool(gate.get("open"))

    @staticmethod
    def _task_platform_and_url(item: dict) -> tuple[str, str, dict[str, Any]]:
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        url = normalize_source_url(str(meta.get("model_url") or item.get("url") or item.get("title") or ""))
        platform = normalize_makerworld_source(meta.get("source"), url)
        return platform, url, meta

    def _verification_resume_gate_snapshot(self, queue: dict) -> dict[str, dict[str, Any]]:
        gate_by_platform: dict[str, dict[str, Any]] = {}
        for item in queue.get("queued") or []:
            if str(item.get("status") or "").strip().lower() != "paused":
                continue
            platform, url, meta = self._task_platform_and_url(item)
            if platform in gate_by_platform:
                continue
            gate_by_platform[platform] = dict(three_mf_gate_for_url(url, meta) or {})
        return gate_by_platform

    def _verification_resume_allowed(self, item: dict, gate_by_platform: dict[str, dict[str, Any]]) -> bool:
        platform, _url, _meta = self._task_platform_and_url(item)
        return bool((gate_by_platform.get(platform) or {}).get("open"))

    def _schedule_browser_recovery_for_legacy_cookie_invalid_gates(
        self,
        queue: dict,
        gate_by_platform: dict[str, dict[str, Any]],
    ) -> None:
        scheduled_platforms: set[str] = set()
        for item in queue.get("queued") or []:
            if str(item.get("status") or "").strip().lower() != "paused":
                continue
            if not _is_three_mf_only_task(item):
                continue
            platform, url, meta = self._task_platform_and_url(item)
            if platform in scheduled_platforms:
                continue
            gate = gate_by_platform.get(platform) or {}
            gate_state = str(gate.get("state") or "").strip().lower()
            if bool(gate.get("open")) or gate_state != "cookie_invalid":
                continue
            self._schedule_browser_session_recovery_for_three_mf_gate(
                platform,
                primary={
                    "model_url": url,
                    "model_id": str(meta.get("model_id") or extract_model_id(url) or ""),
                    "title": str(meta.get("title") or item.get("title") or ""),
                    "instance_id": str(meta.get("instance_id") or ""),
                    "source": platform,
                },
            )
            scheduled_platforms.add(platform)

    def _retire_current_worker_if_surplus(self) -> bool:
        current_worker = threading.current_thread()
        with self._lock:
            try:
                worker_index = self._workers.index(current_worker)
            except ValueError:
                return False
            target_count = max(int(self._worker_target_count or 0), 1)
            if worker_index < target_count:
                return False
            self._workers.pop(worker_index)
            self._worker = self._workers[0] if self._workers else None
            return True

    def _run_loop(self) -> None:
        while True:
            if self._retire_current_worker_if_surplus():
                return
            has_active_batch = self._refresh_batch_tasks()
            queue = self.task_store.load_archive_queue()
            queued = queue.get("queued") or []
            limit_guard = _read_three_mf_limit_guard()
            if _is_three_mf_limit_guard_active(limit_guard):
                paused_count = self._pause_missing_3mf_retry_tasks_for_limit(limit_guard)
                if paused_count:
                    queue = self.task_store.load_archive_queue()
                    queued = queue.get("queued") or []
            if not queued:
                if has_active_batch:
                    time.sleep(ACTIVE_BATCH_IDLE_POLL_SECONDS)
                    continue
                return

            candidate = self._next_executable_task(queue)
            if candidate is None:
                if has_active_batch:
                    time.sleep(ACTIVE_BATCH_IDLE_POLL_SECONDS)
                    continue
                self._blocked_queue_retry_at = time.monotonic() + 10 * 60
                return

            self._blocked_queue_retry_at = 0.0
            candidate_url = normalize_source_url(str(candidate.get("url") or ""))
            with resource_slot("makerworld_page_api", detail=candidate_url):
                if self._retire_current_worker_if_surplus():
                    return
                task = self.task_store.lease_next_archive_task(self._next_executable_task)
                if task is None:
                    continue
                self._run_leased_task(task)

    def _run_leased_task(self, task: dict[str, Any]) -> None:
        task_id = task["id"]
        task_url = str(task.get("url") or "")
        task_meta = task.get("meta") if isinstance(task.get("meta"), dict) else {}
        self.task_store.update_active_task(task_id, message="正在准备归档", progress=1)
        _log_archive(
            "task_started",
            "归档任务开始执行。",
            task_id=task_id,
            url=task_url,
            mode=str(task.get("mode") or "") or detect_archive_mode(task_url),
        )

        try:
            task_mode = str(task.get("mode") or "") or detect_archive_mode(task_url)
            if task_mode in {"author_upload", "collection_models"}:
                self._run_batch_task(task_id, task_url, task_mode, meta=task_meta)
            else:
                self._run_single_task(task_id, task_url, meta=task_meta)
        except Exception as exc:
            model_id = extract_model_id(task.get("url") or "")
            error_message = str(exc)
            if self._complete_terminal_not_found_task(
                task_id=task_id,
                url=task_url,
                meta=task_meta,
                model_id=model_id,
                message=error_message,
            ):
                return
            if model_id:
                self.task_store.update_missing_3mf_status(
                    model_id=model_id,
                    status="missing",
                    message=error_message,
                )
            _sync_account_health_for_archive_exception(
                task_meta=task_meta,
                model_url=normalize_source_url(task_url),
                model_id=model_id,
                detail=error_message,
            )
            self.task_store.fail_archive_task(task_id, error_message)
            _log_archive(
                "task_failed",
                error_message,
                level="error",
                task_id=task_id,
                url=task_url,
                mode=str(task.get("mode") or "") or detect_archive_mode(task_url),
            )
        finally:
            self._refresh_batch_tasks()

    def _complete_terminal_not_found_task(
        self,
        *,
        task_id: str,
        url: str,
        meta: dict[str, Any],
        model_id: str,
        message: str,
    ) -> bool:
        if not meta.get("missing_3mf_retry"):
            return False
        clean_url = normalize_source_url(str(meta.get("model_url") or url or ""))
        clean_model_id = str(meta.get("model_id") or model_id or extract_model_id(clean_url) or "").strip()
        if not clean_model_id:
            return False
        if not _is_not_found_archive_error(message, clean_url):
            return False

        title = str(meta.get("title") or "").strip()
        instance_id = str(meta.get("instance_id") or "").strip()
        self.task_store.remove_missing_3mf_item(
            model_id=clean_model_id,
            model_url=clean_url,
            title=title,
            instance_id=instance_id,
        )
        self.task_store.remove_recent_failures_for_model(clean_model_id, url=clean_url)
        completion_message = f"源端已不可用，已停止缺失 3MF 重试：{message}"
        self.task_store.complete_archive_task(
            task_id,
            progress=100,
            message=completion_message,
        )
        _log_archive(
            "missing_3mf_not_found_cleared",
            "源端已不可用，已停止缺失 3MF 重试。",
            task_id=task_id,
            url=clean_url,
            model_id=clean_model_id,
            instance_id=instance_id,
            message=message,
        )
        return True

    def _run_batch_task(self, task_id: str, url: str, mode: str, meta: Optional[dict] = None) -> None:
        config = self.store.load()
        cookie = _select_cookie(url, config)
        if not cookie:
            raise RuntimeError("未找到可用 Cookie，请先到设置页配置对应站点 Cookie。")

        meta = meta if isinstance(meta, dict) else {}
        existing_expected_items = self._normalize_batch_expected_items(meta.get("batch_expected_items"))
        if existing_expected_items:
            batch_progress = meta.get("batch_progress") if isinstance(meta.get("batch_progress"), dict) else {}
            total = max(int(batch_progress.get("total") or len(existing_expected_items)), 1)
            completed = max(int(batch_progress.get("completed") or 0), 0)
            failed = max(int(batch_progress.get("failed") or 0), 0)
            progress = 60 + int(((completed + failed) / total) * 40)
            self.task_store.update_active_task(
                task_id,
                progress=max(min(progress, 99), 1),
                message="批量任务已恢复，正在继续处理子任务",
                meta=meta,
            )
            _append_batch_queue_log(
                "batch_resumed",
                batch_task_id=task_id,
                batch_url=url,
                total=len(existing_expected_items),
            )
            _log_archive("batch_resumed", "批量任务已恢复，继续等待子任务完成。", task_id=task_id, url=url, total=len(existing_expected_items))
            return

        preview_items = list(meta.get("discovered_items") or [])
        if preview_items:
            print(f"[makerhub] batch_discovery reuse_preview mode={mode} url={url} items={len(preview_items)}", flush=True)
            _log_archive("batch_scan_reuse_preview", "使用预扫描结果执行批量归档。", task_id=task_id, url=url, mode=mode, items=len(preview_items))
            discovered = {
                "items": preview_items,
                "expected_total": meta.get("expected_total"),
                "pages_scanned": meta.get("pages_scanned"),
                "mode": meta.get("scan_mode") or "preview",
            }
            self.task_store.update_active_task(
                task_id,
                progress=55,
                message=f"已使用预扫描结果，发现 {len(preview_items)} 个模型，正在加入归档队列",
            )
        else:
            print(f"[makerhub] batch_discovery start mode={mode} url={url}", flush=True)
            _log_archive("batch_scan_started", "开始扫描批量归档链接。", task_id=task_id, url=url, mode=mode)
            self.task_store.update_active_task(
                task_id,
                progress=5,
                message="正在扫描模型链接",
            )

            with _temporary_proxy_env(config, url):
                discovered = run_discover_batch_urls_job(url, cookie, proxy_config=config.proxy)

        pending_keys = self._queued_task_keys()
        archived_keys = self._archived_task_keys()
        queued_count = 0
        skipped_pending = 0
        skipped_archived = 0
        expected_items: list[dict[str, Any]] = []
        discovered_items = [_source_item_url(item) for item in discovered.get("items") or [] if _source_item_url(item)]
        total_items = len(discovered_items)

        if total_items:
            self.task_store.update_active_task(
                task_id,
                progress=55,
                message=f"扫描完成，发现 {total_items} 个模型，正在加入归档队列",
            )

        child_queue_message_prefix = str(meta.get("child_queue_message_prefix") or "").strip() or f"来自批量归档：{url}"
        progress_step = max(total_items // 10, 1) if total_items else 1
        child_tasks: list[dict[str, Any]] = []
        child_contexts: list[dict[str, str]] = []

        for index, model_url in enumerate(discovered_items, start=1):
            key = _task_key(model_url)
            if key in pending_keys:
                skipped_pending += 1
            elif key in archived_keys:
                skipped_archived += 1
            else:
                child_task = self._new_archive_queue_item(
                    model_url,
                    message=child_queue_message_prefix,
                    mode="single_model",
                    meta={
                        "batch_parent_id": task_id,
                        "batch_source_url": url,
                    },
                )
                child_tasks.append(child_task)
                child_contexts.append({"url": model_url, "task_key": key})

            if total_items and (index == total_items or index % progress_step == 0):
                progress = 55 + int((index / total_items) * 40)
                self.task_store.update_active_task(
                    task_id,
                    progress=min(progress, 95),
                    message=f"正在加入归档队列：{index}/{total_items}",
                )

        bulk_result = self.task_store.enqueue_archive_tasks(child_tasks) if child_tasks else {"items": []}
        bulk_items = bulk_result.get("items") if isinstance(bulk_result, dict) else []
        bulk_items = bulk_items if isinstance(bulk_items, list) else []
        for context, result_item in zip(child_contexts, bulk_items):
            result_item = result_item if isinstance(result_item, dict) else {}
            if not result_item.get("enqueued"):
                skipped_pending += 1
                continue
            model_url = context["url"]
            expected_items.append(
                {
                    "url": model_url,
                    "task_key": context["task_key"],
                    "model_id": extract_model_id(model_url),
                    "attempts": 1,
                    "status": "queued",
                    "last_task_id": str(result_item.get("task_id") or ""),
                }
            )
        queued_count = len(expected_items)

        expected_total = discovered.get("expected_total")
        total_hint = ""
        if isinstance(expected_total, int) and expected_total > total_items:
            total_hint = f"（站点总数 {expected_total}）"

        print(
            "[makerhub] batch_discovery done "
            f"mode={mode} scan_mode={discovered.get('mode') or ''} pages={discovered.get('pages_scanned') or 0} "
            f"expected_total={expected_total} discovered={total_items} queued={queued_count} "
            f"skipped_pending={skipped_pending} skipped_archived={skipped_archived}",
            flush=True,
        )
        meta.pop("discovered_items", None)
        meta["batch_expected_items"] = expected_items
        meta["batch_progress"] = {
            "total": queued_count,
            "completed": 0,
            "failed": 0,
            "running": 0,
            "queued": queued_count,
            "remaining": queued_count,
        }
        meta["batch_summary"] = {
            "discovered": total_items,
            "expected_total": expected_total,
            "queued": queued_count,
            "skipped_pending": skipped_pending,
            "skipped_archived": skipped_archived,
            "scan_mode": discovered.get("mode") or "",
            "pages_scanned": discovered.get("pages_scanned") or 0,
        }
        summary_message = (
            f"批量扫描完成：发现 {total_items} 个模型{total_hint}，"
            f"新增入队 {queued_count} 个，已在队列 {skipped_pending} 个，已归档 {skipped_archived} 个。"
        )
        _append_batch_queue_log(
            "batch_enqueued",
            batch_task_id=task_id,
            batch_url=url,
            mode=mode,
            discovered=total_items,
            expected_total=expected_total,
            queued=queued_count,
            skipped_pending=skipped_pending,
            skipped_archived=skipped_archived,
        )
        _log_archive(
            "batch_enqueued",
            summary_message,
            task_id=task_id,
            url=url,
            mode=mode,
            discovered=total_items,
            expected_total=expected_total,
            queued=queued_count,
            skipped_pending=skipped_pending,
            skipped_archived=skipped_archived,
            pages_scanned=discovered.get("pages_scanned") or 0,
            scan_mode=discovered.get("mode") or "",
        )
        if queued_count <= 0:
            self.task_store.complete_archive_task(
                task_id,
                progress=100,
                message=summary_message,
                meta=meta,
            )
            _log_archive("batch_completed_no_new", summary_message, task_id=task_id, url=url, mode=mode)
            return

        self.task_store.update_active_task(
            task_id,
            progress=60,
            message=(
                f"{summary_message} 子任务已入队，正在等待批量完成确认。"
            ),
            meta=meta,
        )

    def _run_single_task(self, task_id: str, url: str, meta: Optional[dict] = None) -> None:
        config = self.store.load()
        cookie = _select_cookie(url, config)
        if not cookie:
            raise RuntimeError("未找到可用 Cookie，请先到设置页配置对应站点 Cookie。")

        meta = meta if isinstance(meta, dict) else {}
        missing_3mf_retry = bool(meta.get("missing_3mf_retry"))
        three_mf_download_task = bool(meta.get("three_mf_download"))
        browser_session_recovery = bool(meta.get("browser_session_recovery"))
        browser_platform = normalize_makerworld_source(meta.get("source"), url)
        browser_profile_id = next(
            (
                str(getattr(item, "browser_profile_id", "") or "").strip()
                for item in getattr(config, "cookies", []) or []
                if str(getattr(item, "platform", "") or "").strip() == browser_platform
            ),
            "",
        )
        browser_three_mf_authorization = bool(
            browser_profile_id and cloakbrowser_configured()
        )
        instance_ids = _clean_instance_ids(meta.get("instance_ids"))
        archive_instance_ids = instance_ids if three_mf_download_task else []
        asset_lightweight_task = missing_3mf_retry or three_mf_download_task
        default_asset_download = not asset_lightweight_task
        download_assets = _meta_bool(meta, "download_assets", default_asset_download)
        download_comment_assets = _meta_bool(
            meta,
            "download_comment_assets",
            False if asset_lightweight_task else download_assets,
        )
        collect_comments_data = _meta_bool(
            meta,
            "collect_comments_data",
            not asset_lightweight_task,
        )
        rebuild_archive = _meta_bool(meta, "rebuild_archive", True)
        record_missing_3mf_log = _meta_bool(meta, "record_missing_3mf_log", True)
        model_id = extract_model_id(url)
        limit_guard = _read_three_mf_limit_guard()
        daily_limit_active = _is_three_mf_limit_guard_active_for_url(url, limit_guard)
        platform_gate = three_mf_gate_for_url(url, meta)
        platform_gate_active = not bool(platform_gate.get("open")) and not browser_session_recovery
        platform_gate_skip_state = _three_mf_skip_state_from_gate(platform_gate.get("state"))
        defer_three_mf_download = not (
            missing_3mf_retry
            or three_mf_download_task
        )
        skip_three_mf_fetch = daily_limit_active or platform_gate_active or defer_three_mf_download
        skip_three_mf_message = (
            _three_mf_limit_message(limit_guard)
            if daily_limit_active
            else str(platform_gate.get("message") or "")
            if platform_gate_active
            else ""
        )
        if model_id and missing_3mf_retry:
            self.task_store.update_missing_3mf_status(
                model_id=model_id,
                status="running",
                message=(
                    "每日上限未恢复，正在刷新模型元数据并跳过 3MF 下载。"
                    if skip_three_mf_fetch
                    else "正在尝试重新下载 3MF"
                ),
            )

        progress_throttle = _ArchiveProgressThrottle()

        def progress_callback(payload: dict) -> None:
            archive_stage = _archive_stage_from_progress_payload(payload)
            percent = int(payload.get("percent") or 0)
            if not progress_throttle.should_persist(percent=percent, stage=archive_stage):
                return
            self.task_store.update_active_task(
                task_id,
                progress=percent,
                message=str(payload.get("message") or ""),
                archive_stage=archive_stage,
                archive_stage_progress=_archive_stage_progress_from_payload(payload, archive_stage),
            )

        cn_daily_limit, global_daily_limit = _three_mf_daily_limits(config)
        three_mf_captcha_result_header = ""
        def run_job() -> dict[str, Any]:
            with _temporary_proxy_env(config, url):
                return run_archive_model_job(
                    url=url,
                    cookie=cookie,
                    download_dir=str(ARCHIVE_DIR),
                    logs_dir=str(LOGS_DIR),
                    existing_root=str(ARCHIVE_DIR),
                    progress_callback=progress_callback,
                    skip_three_mf_fetch=skip_three_mf_fetch,
                    three_mf_skip_message=skip_three_mf_message,
                    download_assets=download_assets,
                    download_comment_assets=download_comment_assets,
                    collect_comments_data=collect_comments_data,
                    rebuild_archive=rebuild_archive,
                    record_missing_3mf_log=record_missing_3mf_log,
                    three_mf_skip_state=(
                        "download_limited"
                        if daily_limit_active
                        else platform_gate_skip_state
                        if platform_gate_active
                        else "pending_download"
                        if defer_three_mf_download
                        else ""
                    ),
                    three_mf_daily_limit_cn=cn_daily_limit,
                    three_mf_daily_limit_global=global_daily_limit,
                    proxy_config=config.proxy,
                    three_mf_captcha_result_header=three_mf_captcha_result_header,
                    browser_three_mf_authorization=browser_three_mf_authorization,
                    browser_profile_id=browser_profile_id,
                    instance_ids=archive_instance_ids,
                )

        result = run_job()

        missing_items = []
        cleared_not_found_items = []
        limit_guard_state: Optional[dict[str, Any]] = limit_guard if daily_limit_active else None
        resolved_model_id = str(result.get("model_id") or "")
        resolved_model_url = normalize_source_url(url)
        for item in result.get("missing_3mf") or []:
            if str(item.get("downloadState") or "").strip() == "download_limited":
                if not _is_three_mf_limit_guard_active_for_url(url, limit_guard_state):
                    limit_guard_state = _activate_three_mf_limit_guard(
                        message=str(item.get("downloadMessage") or ""),
                        model_id=resolved_model_id,
                        model_url=resolved_model_url,
                        instance_id=str(item.get("id") or item.get("profileId") or item.get("instanceId") or ""),
                    )
            missing_item = {
                "model_id": resolved_model_id,
                "model_url": resolved_model_url,
                "title": str(item.get("title") or item.get("name") or result.get("base_name") or ""),
                "instance_id": str(item.get("id") or item.get("profileId") or item.get("instanceId") or ""),
                "status": normalize_three_mf_failure_state(
                    item.get("downloadState") or "",
                    item.get("downloadMessage") or "",
                    url=url,
                ),
                "message": _missing_3mf_message_from_result(item, limit_guard_state, url=url),
                "updated_at": china_now().isoformat(),
            }
            if missing_3mf_retry and missing_item["status"] == "not_found":
                cleared_not_found_items.append(missing_item)
                continue
            missing_items.append(missing_item)
        self.task_store.replace_missing_3mf_for_model(resolved_model_id, missing_items)
        for item in cleared_not_found_items:
            self.task_store.remove_missing_3mf_item(
                model_id=item["model_id"],
                model_url=item["model_url"],
                title=item["title"],
                instance_id=item["instance_id"],
            )
            _log_archive(
                "missing_3mf_not_found_cleared",
                "源端已不可用，已停止缺失 3MF 重试。",
                task_id=task_id,
                url=item["model_url"],
                model_id=item["model_id"],
                instance_id=item["instance_id"],
                message=item["message"],
            )
        account_platform = normalize_makerworld_source(meta.get("source"), url)
        account_model_url = resolved_model_url
        account_instance_id = str(meta.get("instance_id") or "").strip()
        cookie_stale = _is_cookie_stale_for_platform(self.store, account_platform, cookie)
        account_gate_failure = None
        if cookie_stale and _account_health_failure_from_missing_items(missing_items) is not None:
            _log_archive(
                "stale_cookie_result_ignored",
                "任务运行期间 Cookie 已更新，跳过旧 Cookie 失败状态写回。",
                task_id=task_id,
                platform=account_platform,
                url=account_model_url,
                model_id=resolved_model_id,
            )
        else:
            account_gate_failure = _sync_account_health_for_archive_result(
                platform=account_platform,
                model_url=account_model_url,
                model_id=resolved_model_id,
                instance_id=account_instance_id,
                missing_items=missing_items,
                missing_3mf_retry=missing_3mf_retry,
                browser_session_recovery=browser_session_recovery,
            )
        if isinstance(account_gate_failure, dict):
            self._pause_three_mf_retry_tasks_for_gate(
                platform=account_platform,
                state=account_gate_failure.get("status") or "",
                message=account_gate_failure.get("detail") or "",
            )
            gate_state = str(account_gate_failure.get("status") or "").strip().lower()
            if gate_state in {"auth_required", "cookie_invalid", "verification_required", "cloudflare"}:
                blocked_instance_id = str(account_gate_failure.get("instance_id") or account_instance_id).strip()
                blocked_item = next(
                    (
                        item
                        for item in missing_items
                        if str(item.get("instance_id") or "").strip() == blocked_instance_id
                    ),
                    missing_items[0] if missing_items else {},
                )
                self._schedule_browser_session_recovery_for_three_mf_gate(
                    account_platform,
                    primary={
                        "model_url": account_model_url,
                        "model_id": resolved_model_id,
                        "title": str(blocked_item.get("title") or ""),
                        "instance_id": str(blocked_item.get("instance_id") or blocked_instance_id),
                        "source": account_platform,
                    },
                )
        elif browser_three_mf_authorization and missing_3mf_retry and not missing_items:
            resumed_count = self._resume_paused_missing_3mf_retry_tasks_for_platform(account_platform)
            if resumed_count:
                _log_archive(
                    "browser_3mf_authorization_verified",
                    "指纹浏览器已取得 3MF 授权，已恢复同平台暂停任务。",
                    platform=account_platform,
                    resumed_count=resumed_count,
                )
        if limit_guard_state is not None:
            self._pause_missing_3mf_retry_tasks_for_limit(limit_guard_state)
        self.task_store.remove_recent_failures_for_model(
            resolved_model_id,
            url=normalize_source_url(url),
        )
        archived_model_dir = _resolve_archive_result_model_dir(result)
        if archived_model_dir:
            invalidate_model_detail_cache(archived_model_dir)
        if not archived_model_dir or not upsert_archive_snapshot_model(
            archived_model_dir,
            reason="archive_worker_single_task_completed",
        ):
            invalidate_archive_snapshot("archive_worker_single_task_completed")

        if defer_three_mf_download and isinstance(result.get("instances"), list) and result.get("instances"):
            self._enqueue_three_mf_stage_task_from_result(url, result, meta)

        result_name = result.get("base_name") or result.get("work_dir") or ""
        if three_mf_download_task:
            completion_event, completion_message = _three_mf_task_completion(result_name, missing_items)
        else:
            completion_event = "single_completed"
            completion_message = f"归档完成：{result_name}"

        self.task_store.complete_archive_task(
            task_id,
            progress=100,
            message=completion_message,
        )
        _log_archive(
            completion_event,
            completion_message,
            task_id=task_id,
            url=url,
            model_id=resolved_model_id,
            base_name=result.get("base_name"),
            work_dir=result.get("work_dir"),
            missing_3mf_count=len(missing_items),
        )
