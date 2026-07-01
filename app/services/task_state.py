import json
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urljoin, urlparse, urlunparse

from app.core.database_json_state import load_database_json_state, save_database_json_state
from app.core.settings import LOGS_DIR, STATE_DIR, ensure_app_dirs
from app.core.timezone import now as china_now, now_iso as china_now_iso, parse_datetime
from app.services import task_messages
from app.services.state_contracts import (
    ARCHIVE_QUEUE_STATE_KEY,
    MISSING_3MF_STATE_KEY,
    MODEL_FLAGS_STATE_KEY,
    ORGANIZE_TASKS_STATE_KEY,
    REMOTE_REFRESH_STATE_KEY,
    SOURCE_REFRESH_QUEUE_STATE_KEY,
    SOURCE_REFRESH_RUNS_STATE_KEY,
    SUBSCRIPTIONS_STATE_KEY,
    THREE_MF_LIMIT_GUARD_STATE_KEY,
)
from app.services.state_events import publish_state_event, task_counts_payload
from app.services.task_runtime import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    is_lease_expired,
    lease_expiry_from_now,
    normalize_runtime_status,
    task_attempt_count,
    task_attempts_remaining,
)
from app.services.three_mf import describe_three_mf_failure, normalize_makerworld_source, normalize_three_mf_failure_state

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local dev only.
    fcntl = None


ARCHIVE_QUEUE_PATH = STATE_DIR / "archive_queue.json"
MISSING_3MF_PATH = STATE_DIR / "missing_3mf.json"
THREE_MF_LIMIT_GUARD_PATH = STATE_DIR / "three_mf_limit_guard.json"
ORGANIZE_TASKS_PATH = STATE_DIR / "organize_tasks.json"
MODEL_FLAGS_PATH = STATE_DIR / "model_flags.json"
SUBSCRIPTIONS_STATE_PATH = STATE_DIR / "subscriptions_state.json"
REMOTE_REFRESH_STATE_PATH = STATE_DIR / "remote_refresh_state.json"
SOURCE_REFRESH_QUEUE_PATH = STATE_DIR / "source_refresh_queue.json"
SOURCE_REFRESH_RUNS_PATH = STATE_DIR / "source_refresh_runs.json"
ORGANIZER_LOG_PATH = LOGS_DIR / "organizer.log"
ORGANIZE_TASK_VISIBLE_LIMIT = 50
ORGANIZER_TERMINAL_EVENTS = {
    "organized",
    "duplicate_skipped",
    "deleted_model_skipped",
    "duplicate_skip_failed",
    "organize_failed",
    "worker_timeout",
}
ARCHIVE_BATCH_TASK_MODES = {"author_upload", "collection_models"}
ARCHIVE_MODEL_PATH_RE = re.compile(r"/(?:[a-z]{2}/)?models/(\d+)(?:[^\"'\\s<>]*)?", re.I)
ARCHIVE_AUTHOR_UPLOAD_RE = re.compile(r"/(?:[a-z]{2}/)?@([^/?#]+)/upload(?:[/?#]|$)", re.I)
ARCHIVE_AUTHOR_ROOT_RE = re.compile(r"^/(?:[a-z]{2}/)?@[^/?#]+/?$", re.I)
ARCHIVE_COLLECTION_DETAIL_RE = re.compile(r"/(?:[a-z]{2}/)?collections/\d+(?:-[^/?#]+)?(?:[/?#]|$)", re.I)
ARCHIVE_COMPLETION_MESSAGE_MARKERS = (
    "归档完成",
    "批量归档完成",
    "新增 3MF 下载完成",
)
_STATE_LOCK = threading.RLock()
_ORGANIZER_HISTORY_COUNT_CACHE = {
    "mtime_ns": 0,
    "size": 0,
    "count": 0,
}
_ORGANIZER_TERMINAL_LOG_CACHE = {
    "mtime_ns": 0,
    "size": 0,
    "events": [],
}
ORGANIZER_STATUS_LOG_LOOKBACK_BYTES = 2 * 1024 * 1024
_JSON_STATE_KEYS = {
    ARCHIVE_QUEUE_PATH.resolve(): ARCHIVE_QUEUE_STATE_KEY,
    MISSING_3MF_PATH.resolve(): MISSING_3MF_STATE_KEY,
    ORGANIZE_TASKS_PATH.resolve(): ORGANIZE_TASKS_STATE_KEY,
    MODEL_FLAGS_PATH.resolve(): MODEL_FLAGS_STATE_KEY,
    SUBSCRIPTIONS_STATE_PATH.resolve(): SUBSCRIPTIONS_STATE_KEY,
    REMOTE_REFRESH_STATE_PATH.resolve(): REMOTE_REFRESH_STATE_KEY,
    SOURCE_REFRESH_QUEUE_PATH.resolve(): SOURCE_REFRESH_QUEUE_STATE_KEY,
    SOURCE_REFRESH_RUNS_PATH.resolve(): SOURCE_REFRESH_RUNS_STATE_KEY,
    THREE_MF_LIMIT_GUARD_PATH.resolve(): THREE_MF_LIMIT_GUARD_STATE_KEY,
}


def _json_state_key_for_path(path: Path) -> str:
    try:
        return _JSON_STATE_KEYS.get(path.resolve(), "")
    except OSError:
        return ""


def _looks_like_html_message(text: str) -> bool:
    return task_messages.looks_like_html_message(text)


def _sanitize_message_text(value: Any, fallback: str = "") -> str:
    return task_messages.sanitize_message_text(value, fallback)


def _normalize_source_refresh_text(value: Any) -> str:
    return task_messages.normalize_source_refresh_text(value)


def _normalize_source_refresh_item(item: dict) -> dict:
    return task_messages.normalize_source_refresh_item(item)


def _normalize_task_item(item: Any, default_status: str) -> dict:
    return task_messages.normalize_task_item(item, default_status)


def _normalize_archive_identity_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("/"):
        absolute = urljoin("https://makerworld.com.cn", raw)
    elif not raw.startswith(("http://", "https://")):
        absolute = f"https://{raw.lstrip('/')}"
    else:
        absolute = raw

    parsed = urlparse(absolute)
    path = parsed.path or ""
    author_match = ARCHIVE_AUTHOR_UPLOAD_RE.search(path)
    if author_match:
        handle = author_match.group(1)
        normalized_path = f"/zh/@{quote(handle, safe='@._-')}/upload"
        return urlunparse(parsed._replace(path=normalized_path, query="", fragment=""))
    if ARCHIVE_AUTHOR_ROOT_RE.fullmatch(path):
        handle = path.rstrip("/").split("@", 1)[-1]
        normalized_path = f"/zh/@{quote(handle, safe='@._-')}/upload"
        return urlunparse(parsed._replace(path=normalized_path, query="", fragment=""))
    if ARCHIVE_COLLECTION_DETAIL_RE.search(path):
        return urlunparse(parsed._replace(query="", fragment=""))
    return urlunparse(parsed._replace(fragment=""))


def _archive_model_id_from_url(value: Any) -> str:
    normalized = _normalize_archive_identity_url(value)
    match = ARCHIVE_MODEL_PATH_RE.search(urlparse(normalized).path or "")
    return match.group(1) if match else ""


def _archive_task_mode_from_url(value: Any) -> str:
    path = urlparse(_normalize_archive_identity_url(value)).path or ""
    if ARCHIVE_AUTHOR_UPLOAD_RE.search(path) or ARCHIVE_AUTHOR_ROOT_RE.fullmatch(path):
        return "author_upload"
    if "/collections/models" in path.lower() or ARCHIVE_COLLECTION_DETAIL_RE.search(path):
        return "collection_models"
    if ARCHIVE_MODEL_PATH_RE.search(path):
        return "single_model"
    return ""


def _archive_task_instance_ids(meta: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for value in meta.get("instance_ids") or []:
        clean = str(value or "").strip()
        if clean and clean not in values:
            values.append(clean)
    instance_id = str(meta.get("instance_id") or "").strip()
    if instance_id and instance_id not in values:
        values.append(instance_id)
    return values


def _archive_model_instance_identity(model_id: str, instance_ids: list[str]) -> str:
    suffix = f":instances:{'|'.join(sorted(instance_ids))}" if instance_ids else ""
    return f"model:{model_id}{suffix}"


def _merge_archive_task_instance_scope(target: dict[str, Any], source: dict[str, Any]) -> bool:
    target_meta = target.get("meta") if isinstance(target.get("meta"), dict) else {}
    source_meta = source.get("meta") if isinstance(source.get("meta"), dict) else {}
    merged_instance_ids: list[str] = []
    for value in _archive_task_instance_ids(target_meta) + _archive_task_instance_ids(source_meta):
        clean = str(value or "").strip()
        if clean and clean not in merged_instance_ids:
            merged_instance_ids.append(clean)
    if not merged_instance_ids:
        return False

    previous = _archive_task_instance_ids(target_meta)
    updated_meta = dict(target_meta)
    updated_meta["instance_ids"] = merged_instance_ids
    if not str(updated_meta.get("instance_id") or "").strip():
        updated_meta["instance_id"] = merged_instance_ids[0]
    target["meta"] = updated_meta
    return previous != merged_instance_ids


def _archive_task_identity_key(item: Any, *, failure: bool = False) -> str:
    if not isinstance(item, dict):
        return ""
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    title = str(item.get("title") or "").strip()
    raw_url = item.get("url") or meta.get("model_url") or ""
    if not raw_url and (
        title.startswith(("/", "http://", "https://"))
        or "makerworld." in title.lower()
    ):
        raw_url = title
    url = _normalize_archive_identity_url(raw_url)
    mode = str(item.get("mode") or "").strip() or _archive_task_mode_from_url(url)
    if mode in ARCHIVE_BATCH_TASK_MODES:
        return f"batch:{mode}:{url}" if url else ""

    model_id = str(meta.get("model_id") or "").strip() or _archive_model_id_from_url(url)
    if model_id:
        instance_ids = _archive_task_instance_ids(meta)
        if failure and instance_ids:
            return f"model:{model_id}:instance:{'|'.join(sorted(instance_ids))}"
        if meta.get("three_mf_download"):
            return f"three_mf_download:{_archive_model_instance_identity(model_id, instance_ids)}"
        if meta.get("missing_3mf_retry"):
            return f"missing_3mf_retry:model:{model_id}"
        return f"model:{model_id}"

    if url:
        return f"url:{url}"
    return f"title:{title}" if title else ""


def _archive_existing_identity_map(items: list[dict], default_status: str) -> dict[str, dict]:
    by_key: dict[str, dict] = {}
    for item in items:
        normalized = _normalize_archive_runtime_item(item, default_status)
        key = _archive_task_identity_key(normalized)
        if key and key not in by_key:
            by_key[key] = normalized
    return by_key


def _dedupe_archive_items(items: list[dict], default_status: str, *, failure: bool = False) -> tuple[list[dict], int]:
    kept: list[dict] = []
    seen: dict[str, dict] = {}
    deduped = 0
    for item in items:
        normalized = _normalize_archive_runtime_item(item, default_status)
        key = _archive_task_identity_key(normalized, failure=failure)
        if key and key in seen:
            _merge_archive_task_instance_scope(seen[key], normalized)
            deduped += 1
            continue
        if key:
            seen[key] = normalized
        kept.append(normalized)
    return kept, deduped


def _normalize_archive_runtime_item(item: Any, default_status: str) -> dict:
    normalized = _normalize_task_item(item, default_status)
    source = item if isinstance(item, dict) else {}
    meta = normalized.get("meta") if isinstance(normalized.get("meta"), dict) else {}
    status_default = "waiting_children" if meta.get("batch_expected_items") else default_status
    normalized["status"] = normalize_runtime_status(normalized.get("status"), status_default)
    if meta.get("batch_expected_items") and normalized["status"] == "running":
        normalized["status"] = "waiting_children"

    for field in (
        "lease_owner",
        "lease_expires_at",
        "heartbeat_at",
        "started_at",
        "last_progress_at",
        "parent_task_id",
        "blocked_reason",
        "archive_stage",
    ):
        value = source.get(field)
        if value is not None:
            normalized[field] = str(value)

    if "archive_stage_progress" in source:
        normalized["archive_stage_progress"] = _archive_subtask_int(source.get("archive_stage_progress"), 0)

    if "attempt_count" in source or "attempts" in source or normalized["status"] == "running":
        normalized["attempt_count"] = max(task_attempt_count(source), 1 if normalized["status"] == "running" else 0)
    subtasks = _derive_archive_subtasks(normalized, source.get("subtasks"))
    if subtasks:
        normalized["subtasks"] = subtasks
    return normalized


def _is_completed_archive_running_snapshot(item: dict[str, Any]) -> bool:
    status = normalize_runtime_status(item.get("status"), "running")
    if status != "running":
        return False
    if _archive_subtask_int(item.get("progress"), 0) < 100:
        return False
    message = str(item.get("message") or "").strip()
    return any(marker in message for marker in ARCHIVE_COMPLETION_MESSAGE_MARKERS)


def _has_recent_archive_progress(item: dict[str, Any]) -> bool:
    latest = None
    for field in ("heartbeat_at", "last_progress_at", "updated_at"):
        parsed = parse_datetime(str(item.get(field) or "").strip())
        if parsed is not None and (latest is None or parsed > latest):
            latest = parsed
    if latest is None:
        return False
    return (china_now() - latest).total_seconds() < DEFAULT_LEASE_SECONDS


def _normalize_archive_queue(payload: Any) -> dict:
    if isinstance(payload, list):
        queued = [_normalize_archive_runtime_item(item, "queued") for item in payload]
        return {"active": [], "queued": queued, "recent_failures": []}

    if not isinstance(payload, dict):
        return {"active": [], "queued": [], "recent_failures": []}

    active_items = payload.get("active") or payload.get("running") or []
    queued_items = payload.get("queued") or payload.get("items") or payload.get("pending") or []
    failed_items = payload.get("recent_failures") or payload.get("failed") or payload.get("failures") or []

    active = [
        item
        for item in (_normalize_archive_runtime_item(item, "running") for item in active_items)
        if normalize_runtime_status(item.get("status"), "running") != "completed"
    ]

    return {
        "active": active,
        "queued": [_normalize_archive_runtime_item(item, "queued") for item in queued_items],
        "recent_failures": [_normalize_archive_runtime_item(item, "failed") for item in failed_items],
    }


def _state_payload_signature(payload: dict) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


ARCHIVE_SUBTASK_DEFINITIONS = (
    {"type": "metadata", "label": "元数据", "start": 0, "end": 40},
    {"type": "media", "label": "图片资源", "start": 40, "end": 50},
    {"type": "attachments", "label": "附件", "start": 50, "end": 52},
    {"type": "comments", "label": "评论", "start": 52, "end": 55},
    {"type": "three_mf", "label": "3MF", "start": 55, "end": 78},
    {"type": "finalize", "label": "落盘与索引", "start": 78, "end": 100},
)
ARCHIVE_SUBTASK_TYPES = tuple(item["type"] for item in ARCHIVE_SUBTASK_DEFINITIONS)
ARCHIVE_SUBTASK_ALIASES = {
    "metadata_fetch": "metadata",
    "metadata": "metadata",
    "summary": "media",
    "media_assets": "media",
    "media": "media",
    "images": "media",
    "download_attachments": "attachments",
    "attachments": "attachments",
    "comments": "comments",
    "comment_assets": "comments",
    "three_mf_download": "three_mf",
    "missing_3mf": "three_mf",
    "three_mf": "three_mf",
    "3mf": "three_mf",
    "finalize_index": "finalize",
    "rebuild": "finalize",
    "finalize": "finalize",
}


def _archive_subtask_int(value: Any, default: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(0, min(number, 100))


def _normalize_archive_stage(value: Any) -> str:
    stage = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return ARCHIVE_SUBTASK_ALIASES.get(stage, "")


def _should_attach_archive_subtasks(item: dict[str, Any]) -> bool:
    mode = str(item.get("mode") or "").strip()
    if mode == "single_model":
        return True
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    if any(meta.get(key) for key in ("three_mf_download", "missing_3mf_retry")):
        return True
    url = str(item.get("url") or item.get("title") or "").lower()
    return "/models/" in url or "/model/" in url


def _infer_archive_stage(progress: int, message: str) -> str:
    text = str(message or "").strip().lower()
    if "附件" in text:
        return "attachments"
    if "评论" in text and "摘要、图片与评论整理完成" not in text:
        return "comments"
    if "3mf" in text or "实例" in text or "打印配置" in text:
        return "three_mf"
    if "归档目录" in text or "落盘" in text or "索引" in text or "元数据已生成" in text or "归档完成" in text:
        return "finalize"
    if "图片" in text or "摘要" in text or "头像" in text:
        return "media"
    if progress >= 78:
        return "finalize"
    if progress >= 55:
        return "three_mf"
    if progress >= 52:
        return "comments"
    if progress >= 50:
        return "attachments"
    if progress >= 40:
        return "media"
    return "metadata"


def _subtask_index(stage: str) -> int:
    try:
        return ARCHIVE_SUBTASK_TYPES.index(stage)
    except ValueError:
        return 0


def _progress_within_subtask(definition: dict[str, Any], overall_progress: int) -> int:
    start = int(definition.get("start") or 0)
    end = int(definition.get("end") or start)
    if overall_progress <= start:
        return 0
    if overall_progress >= end or end <= start:
        return 100
    return _archive_subtask_int(round((overall_progress - start) * 100 / (end - start)))


def _normalize_existing_archive_subtasks(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        subtask_type = _normalize_archive_stage(item.get("type"))
        if not subtask_type:
            continue
        status = str(item.get("status") or "").strip().lower()
        if status == "completed":
            status = "done"
        if status not in {"pending", "running", "done", "failed", "skipped", "blocked"}:
            status = "pending"
        normalized[subtask_type] = {
            "type": subtask_type,
            "label": str(item.get("label") or "").strip(),
            "status": status,
            "progress": _archive_subtask_int(item.get("progress"), 0),
            "message": _sanitize_message_text(item.get("message") or ""),
        }
    return normalized


def _derive_archive_subtasks(item: dict[str, Any], existing_subtasks: Any = None) -> list[dict[str, Any]]:
    if not _should_attach_archive_subtasks(item):
        return []

    progress = _archive_subtask_int(item.get("progress"), 0)
    message = str(item.get("message") or "").strip()
    task_status = normalize_runtime_status(item.get("status"), "queued")
    current_stage = _normalize_archive_stage(item.get("archive_stage")) or _infer_archive_stage(progress, message)
    current_index = _subtask_index(current_stage)
    stage_progress = item.get("archive_stage_progress")
    explicit_stage_progress = stage_progress is not None and str(stage_progress).strip() != ""
    existing = _normalize_existing_archive_subtasks(existing_subtasks)

    is_queued = task_status in {"queued", "pending"}
    is_done = task_status in {"completed", "success", "done"} or progress >= 100
    is_failed = task_status in {"failed", "error", "timed_out", "timeout"}

    subtasks: list[dict[str, Any]] = []
    for index, definition in enumerate(ARCHIVE_SUBTASK_DEFINITIONS):
        subtask_type = str(definition["type"])
        previous = existing.get(subtask_type, {})
        subtask = {
            "type": subtask_type,
            "label": str(previous.get("label") or definition["label"]),
            "status": "pending",
            "progress": 0,
            "message": str(previous.get("message") or ""),
        }

        if is_done:
            subtask["status"] = "done"
            subtask["progress"] = 100
        elif is_queued:
            subtask["status"] = "pending"
            subtask["progress"] = 0
        elif index < current_index:
            subtask["status"] = "done"
            subtask["progress"] = 100
        elif index == current_index:
            subtask["status"] = "failed" if is_failed else "running"
            subtask["progress"] = (
                _archive_subtask_int(stage_progress, 0)
                if explicit_stage_progress
                else _progress_within_subtask(definition, progress)
            )
            subtask["message"] = message or subtask["message"]
        else:
            subtask["status"] = "pending"
            subtask["progress"] = 0

        subtasks.append(subtask)

    return subtasks


def _organize_count_needs_backfill(*, item_count: int, total_count: int) -> bool:
    return item_count >= ORGANIZE_TASK_VISIBLE_LIMIT and total_count <= item_count


def _read_active_three_mf_limit_guard() -> dict[str, Any]:
    payload = load_database_json_state("three_mf_limit_guard", {})
    if not isinstance(payload, dict) or not bool(payload.get("active")):
        return {}

    limited_until = str(payload.get("limited_until") or "").strip()
    parsed_until = parse_datetime(limited_until)
    if parsed_until is None or parsed_until <= china_now():
        return {}
    return payload


def _limit_guard_for_missing_item(item_url: str, guard: dict[str, Any]) -> dict[str, Any]:
    if not guard:
        return {}
    guard_source = normalize_makerworld_source(url=guard.get("model_url"))
    item_source = normalize_makerworld_source(url=item_url)
    if guard_source and item_source and guard_source != item_source:
        return {}
    return guard


def _format_three_mf_limit_guard_message(guard: dict[str, Any]) -> str:
    if not guard:
        return ""
    source = normalize_makerworld_source(url=guard.get("model_url"))
    base_message = str(guard.get("message") or "").strip() or describe_three_mf_failure(
        "download_limited",
        source=source,
    )
    if "自动重试暂停至" in base_message:
        base_message = base_message.split("自动重试暂停至", 1)[0].rstrip("，,。 ")

    limited_until = str(guard.get("limited_until") or "").strip()
    parsed_until = parse_datetime(limited_until)
    if parsed_until is None:
        return base_message
    until_text = parsed_until.strftime("%Y-%m-%d %H:%M")
    return f"{base_message.rstrip('。')}，自动重试暂停至 {until_text}。"


def _normalize_missing_3mf(payload: Any, fallback_items: Optional[list[dict]] = None) -> dict:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("items") or payload.get("models") or []
    else:
        items = []

    if not items and fallback_items:
        items = fallback_items

    normalized = []
    limit_guard = _read_active_three_mf_limit_guard()
    for item in items:
        if isinstance(item, str):
            normalized.append({"model_id": "", "title": item, "status": "missing"})
            continue
        if not isinstance(item, dict):
            continue
        message = _sanitize_message_text(item.get("message") or item.get("downloadMessage") or "")
        item_url = str(item.get("model_url") or item.get("url") or "")
        status = normalize_three_mf_failure_state(
            item.get("status") or item.get("downloadState") or "",
            message,
            url=item_url,
        )
        if status == "download_limited":
            limit_message = _format_three_mf_limit_guard_message(_limit_guard_for_missing_item(item_url, limit_guard))
            message = describe_three_mf_failure(
                status,
                "",
                url=item_url,
                limit_message=limit_message,
            )
        if status in {"verification_required", "cloudflare"}:
            message = describe_three_mf_failure(status, message, url=item_url)
        normalized.append(
            {
                "model_id": str(item.get("model_id") or item.get("id") or ""),
                "title": str(item.get("title") or item.get("name") or ""),
                "status": status,
                "model_url": item_url,
                "instance_id": str(item.get("instance_id") or item.get("profileId") or item.get("instanceId") or ""),
                "message": message,
                "updated_at": str(item.get("updated_at") or item.get("time") or ""),
            }
        )
        normalized_item = normalized[-1]
        api_url = str(item.get("api_url") or item.get("apiUrl") or "").strip()
        if api_url:
            normalized_item["api_url"] = api_url
        source = normalize_makerworld_source(item.get("source"), item_url)
        if source:
            normalized_item["source"] = source
        verification = item.get("verification") if isinstance(item.get("verification"), dict) else {}
        captcha_id = str(
            item.get("captcha_id")
            or item.get("captchaId")
            or verification.get("captcha_id")
            or verification.get("captchaId")
            or ""
        ).strip()
        if captcha_id:
            normalized_item["captcha_id"] = captcha_id
            normalized_item["verification"] = {
                "captcha_id": captcha_id,
                "provider": str(verification.get("provider") or "geetest"),
            }

    return {"items": normalized}


def _missing_3mf_key(item: dict) -> tuple[str, str, str]:
    normalized = _normalize_missing_3mf([item]).get("items", [])
    if not normalized:
        return ("", "", "")
    payload = normalized[0]
    return (
        str(payload.get("model_id") or ""),
        str(payload.get("instance_id") or ""),
        str(payload.get("title") or ""),
    )


def _matches_missing_3mf_item(
    item: dict,
    *,
    model_id: str = "",
    title: str = "",
    instance_id: str = "",
    model_url: str = "",
) -> bool:
    item_model_id = str(item.get("model_id") or "").strip()
    item_title = str(item.get("title") or "").strip()
    item_instance_id = str(item.get("instance_id") or "").strip()
    item_model_url = str(item.get("model_url") or "").strip()

    target_model_id = str(model_id or "").strip()
    target_title = str(title or "").strip()
    target_instance_id = str(instance_id or "").strip()
    target_model_url = str(model_url or "").strip()

    if target_model_id and item_model_id != target_model_id:
        return False
    if target_instance_id and item_instance_id != target_instance_id:
        return False
    if target_title and item_title != target_title:
        return False
    if target_model_url and item_model_url != target_model_url:
        return False

    return any((target_model_id, target_title, target_instance_id, target_model_url))


def _normalize_organize_tasks(payload: Any) -> dict:
    if isinstance(payload, list):
        items = payload
        raw_detected_total = 0
        raw_count = len(items)
        raw_count_trusted = False
        raw_source_dir = ""
        raw_updated_at = ""
        raw_last_import = {}
    elif isinstance(payload, dict):
        items = payload.get("items") or payload.get("tasks") or []
        raw_detected_total = _safe_int(
            payload.get("detected_total", payload.get("pending_total", payload.get("total", 0))),
            0,
        )
        raw_count = _safe_int(
            payload.get("count", payload.get("total_count", len(items))),
            len(items),
        )
        raw_count_trusted = bool(payload.get("count_trusted"))
        raw_source_dir = str(payload.get("source_dir") or "")
        raw_updated_at = str(payload.get("updated_at") or "")
        raw_last_import = payload.get("last_import") if isinstance(payload.get("last_import"), dict) else {}
    else:
        items = []
        raw_detected_total = 0
        raw_count = 0
        raw_count_trusted = False
        raw_source_dir = ""
        raw_updated_at = ""
        raw_last_import = {}

    normalized = []
    for item in items:
        if isinstance(item, str):
            normalized.append(
                {
                    "source_dir": item,
                    "target_dir": "",
                    "status": "pending",
                    "updated_at": "",
                    "move_files": True,
                }
            )
            continue
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "pending")
        snapshot_ready = bool(item.get("snapshot_ready", False))
        if "snapshot_ready" not in item and status.strip().lower() in {"success", "organized"}:
            snapshot_ready = True
        normalized.append(
            {
                "id": str(item.get("id") or item.get("task_id") or ""),
                "title": str(item.get("title") or item.get("file_name") or Path(str(item.get("source_path") or "")).name or ""),
                "file_name": str(item.get("file_name") or Path(str(item.get("source_path") or "")).name or ""),
                "source_dir": str(item.get("source_dir") or item.get("source") or ""),
                "target_dir": str(item.get("target_dir") or item.get("target") or ""),
                "source_path": str(item.get("source_path") or ""),
                "target_path": str(item.get("target_path") or ""),
                "model_dir": str(item.get("model_dir") or ""),
                "status": status,
                "message": str(item.get("message") or item.get("detail") or ""),
                "progress": int(item.get("progress") or item.get("percent") or 0),
                "updated_at": str(item.get("updated_at") or item.get("time") or ""),
                "move_files": bool(item.get("move_files", item.get("move", True))),
                "fingerprint": str(item.get("fingerprint") or ""),
                "snapshot_ready": snapshot_ready,
                "kind": str(item.get("kind") or ""),
                "staging_dir": str(item.get("staging_dir") or ""),
                "package_source": str(item.get("package_source") or ""),
                "package_title": str(item.get("package_title") or ""),
                "original_source_path": str(item.get("original_source_path") or ""),
                "meta": item.get("meta") if isinstance(item.get("meta"), dict) else {},
            }
        )

    normalized.sort(key=_organize_task_sort_key, reverse=True)
    queued_count = 0
    running_count = 0
    for item in normalized:
        status = str(item.get("status") or "").strip().lower()
        if status in {"pending", "queued"}:
            queued_count += 1
        elif status == "running":
            running_count += 1

    detected_total = max(raw_detected_total, queued_count + running_count)
    total_count = max(raw_count, len(normalized))
    if _organize_count_needs_backfill(item_count=len(normalized), total_count=total_count):
        total_count = max(total_count, _organizer_history_count_from_log())
    return {
        "items": normalized,
        "count": total_count,
        "count_trusted": bool(raw_count_trusted or not _organize_count_needs_backfill(item_count=len(normalized), total_count=total_count)),
        "detected_total": detected_total,
        "queued_count": queued_count,
        "running_count": running_count,
        "source_dir": raw_source_dir,
        "updated_at": raw_updated_at,
        "last_import": _normalize_local_import_payload(raw_last_import),
    }


def _normalize_local_import_payload(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {}

    raw_files = payload.get("files")
    files: list[dict[str, Any]] = []
    if isinstance(raw_files, list):
        for item in raw_files:
            if isinstance(item, dict):
                file_name = str(item.get("file_name") or Path(str(item.get("source_path") or "")).name or "").strip()
                source_path = str(item.get("source_path") or "").strip()
                size = _safe_int(item.get("size"), 0)
                status = str(item.get("status") or "").strip()
                message = _sanitize_message_text(item.get("message") or "")
                updated_at = str(item.get("updated_at") or "").strip()
            else:
                file_name = str(item or "").strip()
                source_path = ""
                size = 0
                status = ""
                message = ""
                updated_at = ""
            if not file_name and not source_path:
                continue
            file_item = {
                "file_name": file_name,
                "source_path": source_path,
                "size": size,
            }
            if status:
                file_item["status"] = status
            if message:
                file_item["message"] = message
            if updated_at:
                file_item["updated_at"] = updated_at
            files.append(file_item)

    uploaded_at = str(payload.get("uploaded_at") or payload.get("time") or "")
    _apply_local_import_file_log_statuses(files, uploaded_at=uploaded_at)

    uploaded_count = _safe_int(payload.get("uploaded_count"), len(files))
    return {
        "uploaded_at": uploaded_at,
        "uploaded_count": max(uploaded_count, len(files)),
        "source_dir": str(payload.get("source_dir") or ""),
        "upload_dir": str(payload.get("upload_dir") or ""),
        "files": files,
    }


def _organizer_event_status(event: str) -> str:
    normalized = str(event or "").strip()
    if normalized == "organized":
        return "success"
    if normalized in {"duplicate_skipped", "deleted_model_skipped"}:
        return "skipped"
    if normalized in {"duplicate_skip_failed", "organize_failed", "worker_timeout"}:
        return "failed"
    return ""


def _organizer_event_message(payload: dict[str, Any], status: str) -> str:
    return task_messages.organizer_event_message(payload, status)


def _recent_organizer_terminal_events() -> list[dict[str, Any]]:
    try:
        stat = ORGANIZER_LOG_PATH.stat()
    except OSError:
        return []

    cache_mtime_ns = int(_ORGANIZER_TERMINAL_LOG_CACHE.get("mtime_ns") or 0)
    cache_size = int(_ORGANIZER_TERMINAL_LOG_CACHE.get("size") or 0)
    if cache_mtime_ns == int(stat.st_mtime_ns) and cache_size == int(stat.st_size):
        return list(_ORGANIZER_TERMINAL_LOG_CACHE.get("events") or [])

    events: list[dict[str, Any]] = []
    try:
        with ORGANIZER_LOG_PATH.open("rb") as handle:
            start = max(int(stat.st_size) - ORGANIZER_STATUS_LOG_LOOKBACK_BYTES, 0)
            handle.seek(start)
            if start > 0:
                handle.readline()
            for raw_line in handle:
                try:
                    line = raw_line.decode("utf-8", errors="ignore").strip()
                except AttributeError:
                    line = str(raw_line or "").strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event = str(payload.get("event") or "").strip()
                status = _organizer_event_status(event)
                if not status:
                    continue
                source = str(payload.get("source") or "").strip()
                if not source:
                    continue
                events.append(
                    {
                        "event": event,
                        "status": status,
                        "source": source,
                        "file_name": Path(source).name,
                        "updated_at": str(payload.get("time") or ""),
                        "message": _organizer_event_message(payload, status),
                    }
                )
    except OSError:
        return []

    _ORGANIZER_TERMINAL_LOG_CACHE.update(
        {
            "mtime_ns": int(stat.st_mtime_ns),
            "size": int(stat.st_size),
            "events": events,
        }
    )
    return list(events)


def _local_import_file_matches_event(file_item: dict[str, Any], event: dict[str, Any]) -> bool:
    source_path = str(file_item.get("source_path") or "").strip()
    file_name = str(file_item.get("file_name") or "").strip()
    event_source = str(event.get("source") or "").strip()
    event_file_name = str(event.get("file_name") or Path(event_source).name or "").strip()

    if source_path and event_source and source_path == event_source:
        return True
    return bool(file_name and event_file_name and file_name == event_file_name and not source_path)


def _apply_local_import_file_log_statuses(files: list[dict[str, Any]], *, uploaded_at: str) -> None:
    if not files:
        return

    uploaded_dt = parse_datetime(uploaded_at)
    for event in _recent_organizer_terminal_events():
        event_dt = parse_datetime(event.get("updated_at"))
        if uploaded_dt is not None and event_dt is not None and event_dt < uploaded_dt:
            continue
        for file_item in files:
            if not _local_import_file_matches_event(file_item, event):
                continue
            file_item["status"] = str(event.get("status") or "")
            file_item["message"] = str(event.get("message") or "")
            file_item["updated_at"] = str(event.get("updated_at") or "")


def _organizer_history_count_from_log() -> int:
    try:
        stat = ORGANIZER_LOG_PATH.stat()
    except OSError:
        return 0

    cache_mtime_ns = int(_ORGANIZER_HISTORY_COUNT_CACHE.get("mtime_ns") or 0)
    cache_size = int(_ORGANIZER_HISTORY_COUNT_CACHE.get("size") or 0)
    if cache_mtime_ns == int(stat.st_mtime_ns) and cache_size == int(stat.st_size):
        return int(_ORGANIZER_HISTORY_COUNT_CACHE.get("count") or 0)

    count = 0
    try:
        with ORGANIZER_LOG_PATH.open("r", encoding="utf-8", errors="ignore") as handle:
            for raw_line in handle:
                line = str(raw_line or "").strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(payload.get("event") or "").strip() in ORGANIZER_TERMINAL_EVENTS:
                    count += 1
    except OSError:
        return 0

    _ORGANIZER_HISTORY_COUNT_CACHE.update(
        {
            "mtime_ns": int(stat.st_mtime_ns),
            "size": int(stat.st_size),
            "count": count,
        }
    )
    return count


def _organize_task_sort_key(item: dict) -> tuple[int, str, str]:
    raw = str(item.get("updated_at") or "").strip()
    if raw:
        parsed = parse_datetime(raw)
        if parsed is not None:
            return (int(parsed.timestamp()), raw, str(item.get("id") or ""))
    return (0, raw, str(item.get("id") or ""))


def _normalize_model_flags(payload: Any) -> dict:
    if not isinstance(payload, dict):
        payload = {}

    def _normalize_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        seen: set[str] = set()
        result: list[str] = []
        for item in value:
            clean = str(item or "").strip().strip("/")
            if not clean or clean in seen:
                continue
            seen.add(clean)
            result.append(clean)
        return result

    return {
        "favorites": _normalize_list(payload.get("favorites")),
        "printed": _normalize_list(payload.get("printed")),
        "deleted": _normalize_list(payload.get("deleted")),
    }


def _normalize_subscription_source_item(item: Any) -> Optional[dict]:
    if isinstance(item, str):
        clean = str(item).strip()
        if not clean:
            return None
        return {
            "model_id": "",
            "url": clean,
            "task_key": clean,
        }

    if not isinstance(item, dict):
        return None

    model_id = str(item.get("model_id") or "").strip()
    url = str(item.get("url") or "").strip()
    task_key = str(item.get("task_key") or item.get("key") or "").strip()
    if not any((model_id, url, task_key)):
        return None
    if not task_key:
        task_key = f"model:{model_id}" if model_id else url
    normalized = {
        "model_id": model_id,
        "url": url,
        "task_key": task_key,
    }
    for key in ("source_order", "favorited_at", "source_position"):
        if key in item and item.get(key) not in (None, ""):
            normalized[key] = item.get(key)
    return normalized


def _normalize_subscription_state(payload: Any) -> dict:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("items") or payload.get("subscriptions") or []
    else:
        items = []

    normalized: list[dict] = []
    seen_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        subscription_id = str(item.get("id") or "").strip()
        if not subscription_id or subscription_id in seen_ids:
            continue
        seen_ids.add(subscription_id)

        current_items = []
        current_seen: set[str] = set()
        for raw_child in item.get("current_items") or []:
            child = _normalize_subscription_source_item(raw_child)
            if not child:
                continue
            key = child["task_key"]
            if key in current_seen:
                continue
            current_seen.add(key)
            current_items.append(child)

        tracked_items = []
        tracked_seen: set[str] = set()
        for raw_child in item.get("tracked_items") or []:
            child = _normalize_subscription_source_item(raw_child)
            if not child:
                continue
            key = child["task_key"]
            if key in tracked_seen:
                continue
            tracked_seen.add(key)
            tracked_items.append(child)

        normalized.append(
            {
                "id": subscription_id,
                "status": str(item.get("status") or "idle"),
                "running": bool(item.get("running", False)),
                "next_run_at": str(item.get("next_run_at") or ""),
                "manual_requested_at": str(item.get("manual_requested_at") or ""),
                "last_run_at": str(item.get("last_run_at") or ""),
                "last_success_at": str(item.get("last_success_at") or ""),
                "last_error_at": str(item.get("last_error_at") or ""),
                "last_message": str(item.get("last_message") or ""),
                "last_discovered_count": int(item.get("last_discovered_count") or 0),
                "last_new_count": int(item.get("last_new_count") or 0),
                "last_enqueued_count": int(item.get("last_enqueued_count") or 0),
                "last_deleted_count": int(item.get("last_deleted_count") or 0),
                "current_items": current_items,
                "tracked_items": tracked_items,
            }
        )

    return {"items": normalized}


def _normalize_subscription_state_summary(payload: Any) -> dict:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("items") or payload.get("subscriptions") or []
    else:
        items = []

    normalized: list[dict] = []
    seen_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        subscription_id = str(item.get("id") or "").strip()
        if not subscription_id or subscription_id in seen_ids:
            continue
        seen_ids.add(subscription_id)
        current_items = item.get("current_items") if isinstance(item.get("current_items"), list) else []
        tracked_items = item.get("tracked_items") if isinstance(item.get("tracked_items"), list) else []
        normalized.append(
            {
                "id": subscription_id,
                "status": str(item.get("status") or "idle"),
                "running": bool(item.get("running", False)),
                "next_run_at": str(item.get("next_run_at") or ""),
                "manual_requested_at": str(item.get("manual_requested_at") or ""),
                "last_run_at": str(item.get("last_run_at") or ""),
                "last_success_at": str(item.get("last_success_at") or ""),
                "last_error_at": str(item.get("last_error_at") or ""),
                "last_message": str(item.get("last_message") or ""),
                "last_discovered_count": int(item.get("last_discovered_count") or len(current_items) or 0),
                "last_new_count": int(item.get("last_new_count") or 0),
                "last_enqueued_count": int(item.get("last_enqueued_count") or 0),
                "last_deleted_count": int(item.get("last_deleted_count") or 0),
                "current_count": len(current_items),
                "tracked_count": len(tracked_items),
            }
        )

    return {"items": normalized}


def _normalize_remote_refresh_active_run(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {}
    batch_id = str(payload.get("batch_id") or "").strip()
    status = str(payload.get("status") or "").strip().lower()
    if not batch_id and not status:
        return {}
    if status not in {"running", "resuming", "interrupted", "completed", "abandoned"}:
        status = "running" if batch_id else ""
    return {
        "batch_id": batch_id,
        "status": status,
        "started_at": str(payload.get("started_at") or ""),
        "resumed_at": str(payload.get("resumed_at") or ""),
        "finished_at": str(payload.get("finished_at") or ""),
        "scheduled_cron": str(payload.get("scheduled_cron") or ""),
        "manual": bool(payload.get("manual", False)),
        "candidate_total": _safe_int(payload.get("candidate_total") or 0),
        "completed_total": _safe_int(payload.get("completed_total") or 0),
        "remaining_total": _safe_int(payload.get("remaining_total") or 0),
        "manifest_path": str(payload.get("manifest_path") or ""),
        "result_path": str(payload.get("result_path") or ""),
        "interrupted_reason": _normalize_source_refresh_text(_sanitize_message_text(payload.get("interrupted_reason") or "")),
    }


def _normalize_remote_refresh_state(payload: Any) -> dict:
    if not isinstance(payload, dict):
        payload = {}

    current_item = payload.get("current_item") if isinstance(payload.get("current_item"), dict) else {}
    current_items = payload.get("current_items") if isinstance(payload.get("current_items"), list) else []
    recent_items = payload.get("recent_items") if isinstance(payload.get("recent_items"), list) else []
    normalized_recent = [
        _normalize_source_refresh_item(_normalize_task_item(item, "idle"))
        for item in recent_items
        if isinstance(item, (dict, str))
    ]
    normalized_current_items = [
        _normalize_source_refresh_item(_normalize_task_item(item, "running"))
        for item in current_items
        if isinstance(item, (dict, str))
    ]
    if current_item and not normalized_current_items:
        normalized_current_items = [_normalize_source_refresh_item(_normalize_task_item(current_item, "running"))]
    normalized_metrics = payload.get("last_batch_metrics") if isinstance(payload.get("last_batch_metrics"), dict) else {}
    normalized_resource_waits = payload.get("last_resource_waits") if isinstance(payload.get("last_resource_waits"), dict) else {}
    normalized_slow_models = payload.get("last_slow_models") if isinstance(payload.get("last_slow_models"), list) else []
    active_run = _normalize_remote_refresh_active_run(payload.get("active_run"))

    return {
        "status": str(payload.get("status") or "idle"),
        "running": bool(payload.get("running", False)),
        "next_run_at": str(payload.get("next_run_at") or ""),
        "scheduled_cron": str(payload.get("scheduled_cron") or ""),
        "last_run_at": str(payload.get("last_run_at") or ""),
        "last_success_at": str(payload.get("last_success_at") or ""),
        "last_error_at": str(payload.get("last_error_at") or ""),
        "last_attempt_at": str(payload.get("last_attempt_at") or ""),
        "last_deferred_at": str(payload.get("last_deferred_at") or ""),
        "last_defer_reason": str(payload.get("last_defer_reason") or ""),
        "last_interrupted_at": str(payload.get("last_interrupted_at") or ""),
        "last_interrupted_reason": _normalize_source_refresh_text(_sanitize_message_text(payload.get("last_interrupted_reason") or "")),
        "last_completed_at": str(payload.get("last_completed_at") or ""),
        "manual_requested_at": str(payload.get("manual_requested_at") or ""),
        "last_message": _normalize_source_refresh_text(_sanitize_message_text(payload.get("last_message") or "")),
        "last_batch_total": _safe_int(payload.get("last_batch_total") or 0),
        "last_batch_succeeded": _safe_int(payload.get("last_batch_succeeded") or 0),
        "last_batch_failed": _safe_int(payload.get("last_batch_failed") or 0),
        "last_batch_skipped": _safe_int(payload.get("last_batch_skipped") or 0),
        "last_eligible_total": _safe_int(payload.get("last_eligible_total") or 0),
        "last_remaining_total": _safe_int(payload.get("last_remaining_total") or 0),
        "last_skipped_missing_cookie": _safe_int(payload.get("last_skipped_missing_cookie") or 0),
        "last_skipped_local_or_invalid": _safe_int(payload.get("last_skipped_local_or_invalid") or 0),
        "current_item": normalized_current_items[0] if normalized_current_items else {},
        "current_items": normalized_current_items[:8],
        "last_batch_metrics": normalized_metrics,
        "last_resource_waits": normalized_resource_waits,
        "last_slow_models": normalized_slow_models[:10],
        "recent_items": normalized_recent[:50],
        "stale_archive_queue_detected": bool(payload.get("stale_archive_queue_detected", False)),
        "active_run": active_run,
    }


def _normalize_source_refresh_task(item: Any, default_status: str) -> dict:
    source = item if isinstance(item, dict) else {}
    normalized = _normalize_task_item(item, default_status)
    status = str(normalized.get("status") or default_status).strip().lower()
    if status == "success":
        status = "succeeded"
    if status not in {"queued", "running", "succeeded", "failed", "skipped", "timed_out", "cancelled"}:
        status = default_status
    normalized["status"] = status
    normalized["run_id"] = str(source.get("run_id") or normalized.get("run_id") or "")
    normalized["model_dir"] = str(source.get("model_dir") or normalized.get("model_dir") or normalized.get("id") or "").strip().strip("/")
    normalized["title"] = str(source.get("title") or normalized.get("title") or normalized.get("model_dir") or "")
    normalized["url"] = str(source.get("url") or source.get("origin_url") or normalized.get("url") or "")
    normalized["site"] = normalize_makerworld_source(source.get("site"), normalized.get("url")) or str(source.get("site") or "")
    normalized["attempts"] = _safe_int(source.get("attempts", source.get("attempt_count", 0)), 0)
    normalized["message"] = _normalize_source_refresh_text(_sanitize_message_text(source.get("message") or normalized.get("message") or ""))
    normalized["created_at"] = str(source.get("created_at") or "")
    normalized["started_at"] = str(source.get("started_at") or "")
    normalized["updated_at"] = str(source.get("updated_at") or normalized.get("updated_at") or "")
    normalized["finished_at"] = str(source.get("finished_at") or "")
    normalized["lease_expires_at"] = str(source.get("lease_expires_at") or "")
    normalized["last_heartbeat_at"] = str(source.get("last_heartbeat_at") or source.get("heartbeat_at") or "")
    normalized["metrics"] = source.get("metrics") if isinstance(source.get("metrics"), dict) else {}
    return normalized


def _normalize_source_refresh_queue(payload: Any) -> dict:
    if isinstance(payload, list):
        payload = {"queued": payload}
    if not isinstance(payload, dict):
        payload = {}

    active = [
        item
        for item in (_normalize_source_refresh_task(item, "running") for item in (payload.get("active") or payload.get("running") or []))
        if str(item.get("status") or "") not in {"succeeded", "completed"}
    ]
    queued = [
        _normalize_source_refresh_task(item, "queued")
        for item in (payload.get("queued") or payload.get("items") or payload.get("pending") or [])
    ]
    recent_failures = [
        _normalize_source_refresh_task(item, "failed")
        for item in (payload.get("recent_failures") or payload.get("failed") or payload.get("failures") or [])
    ][:20]
    normalized = {
        "version": _safe_int(payload.get("version"), 1) or 1,
        "active": active,
        "queued": queued,
        "recent_failures": recent_failures,
        "updated_at": str(payload.get("updated_at") or ""),
    }
    normalized["running_count"] = len(normalized["active"])
    normalized["queued_count"] = len(normalized["queued"])
    normalized["failed_count"] = len(normalized["recent_failures"])
    return normalized


def _normalize_source_refresh_run(payload: Any, default_status: str = "") -> dict:
    if not isinstance(payload, dict):
        return {}
    run_id = str(payload.get("run_id") or payload.get("batch_id") or "").strip()
    raw_status = str(payload.get("status") or "").strip().lower()
    if not run_id and not raw_status:
        return {}
    status = raw_status or str(default_status or "").strip().lower()
    if status not in {"queued", "running", "paused", "resuming", "completed", "failed", "interrupted", "cancelled"}:
        status = default_status or ("running" if run_id else "")
    candidate_total = _safe_int(payload.get("candidate_total"), 0)
    completed_total = _safe_int(payload.get("completed_total"), 0)
    remaining_total = _safe_int(payload.get("remaining_total"), max(candidate_total - completed_total, 0))
    current_items = payload.get("current_items") if isinstance(payload.get("current_items"), list) else []
    return {
        "run_id": run_id,
        "status": status,
        "manual": bool(payload.get("manual", False)),
        "created_at": str(payload.get("created_at") or ""),
        "started_at": str(payload.get("started_at") or ""),
        "updated_at": str(payload.get("updated_at") or ""),
        "finished_at": str(payload.get("finished_at") or ""),
        "candidate_total": candidate_total,
        "queued_total": _safe_int(payload.get("queued_total"), 0),
        "completed_total": completed_total,
        "succeeded_total": _safe_int(payload.get("succeeded_total"), 0),
        "failed_total": _safe_int(payload.get("failed_total"), 0),
        "skipped_total": _safe_int(payload.get("skipped_total"), 0),
        "timed_out_total": _safe_int(payload.get("timed_out_total"), 0),
        "remaining_total": max(remaining_total, 0),
        "current_items": [
            _normalize_source_refresh_item(_normalize_task_item(item, "running"))
            for item in current_items
            if isinstance(item, (dict, str))
        ][:8],
        "manifest_path": str(payload.get("manifest_path") or ""),
        "result_path": str(payload.get("result_path") or ""),
        "message": _normalize_source_refresh_text(_sanitize_message_text(payload.get("message") or "")),
    }


def _normalize_source_refresh_runs(payload: Any) -> dict:
    if not isinstance(payload, dict):
        payload = {}
    active_run = _normalize_source_refresh_run(payload.get("active_run"), "running")
    last_completed_run = _normalize_source_refresh_run(payload.get("last_completed_run"), "completed")
    return {
        "version": _safe_int(payload.get("version"), 1) or 1,
        "active_run": active_run,
        "last_completed_run": last_completed_run,
        "last_attempt_at": str(payload.get("last_attempt_at") or ""),
        "last_deferred_at": str(payload.get("last_deferred_at") or ""),
        "last_defer_reason": str(payload.get("last_defer_reason") or ""),
        "last_interrupted_at": str(payload.get("last_interrupted_at") or ""),
        "last_interrupted_reason": _normalize_source_refresh_text(_sanitize_message_text(payload.get("last_interrupted_reason") or "")),
        "next_run_at": str(payload.get("next_run_at") or ""),
        "updated_at": str(payload.get("updated_at") or ""),
    }


def compact_remote_refresh_state(payload: Any, *, include_current: bool = True) -> dict:
    state = _normalize_remote_refresh_state(payload)
    compact = {
        "status": state["status"],
        "running": state["running"],
        "next_run_at": state["next_run_at"],
        "scheduled_cron": state["scheduled_cron"],
        "last_run_at": state["last_run_at"],
        "last_success_at": state["last_success_at"],
        "last_error_at": state["last_error_at"],
        "last_attempt_at": state["last_attempt_at"],
        "last_deferred_at": state["last_deferred_at"],
        "last_defer_reason": state["last_defer_reason"],
        "last_interrupted_at": state["last_interrupted_at"],
        "last_interrupted_reason": state["last_interrupted_reason"],
        "last_completed_at": state["last_completed_at"],
        "manual_requested_at": state["manual_requested_at"],
        "last_message": state["last_message"],
        "last_batch_total": state["last_batch_total"],
        "last_batch_succeeded": state["last_batch_succeeded"],
        "last_batch_failed": state["last_batch_failed"],
        "last_batch_skipped": state["last_batch_skipped"],
        "last_eligible_total": state["last_eligible_total"],
        "last_remaining_total": state["last_remaining_total"],
        "last_skipped_missing_cookie": state["last_skipped_missing_cookie"],
        "last_skipped_local_or_invalid": state["last_skipped_local_or_invalid"],
        "active_run": state["active_run"],
        "stale_archive_queue_detected": state["stale_archive_queue_detected"],
    }
    if include_current:
        compact["current_item"] = state["current_item"]
        compact["current_items"] = state["current_items"][:2]
    return compact


class TaskStateStore:
    def __init__(self) -> None:
        ensure_app_dirs()

    def _publish_state_event(self, scope: str, state: dict, event_type: str = "state.changed", payload: Optional[dict] = None) -> None:
        event_payload = task_counts_payload(state)
        for field in ("status", "running", "last_message", "last_error_at", "last_success_at"):
            if field in state:
                event_payload[field] = state.get(field)
        if isinstance(payload, dict):
            event_payload.update(payload)
        publish_state_event(scope, event_type, event_payload)

    def _read_json(self, path: Path, default: dict) -> dict:
        state_key = _json_state_key_for_path(path)
        if state_key:
            return load_database_json_state(state_key, default)
        raise RuntimeError(f"未登记的运行状态 key：{path}")

    def _write_json(self, path: Path, payload: dict) -> None:
        state_key = _json_state_key_for_path(path)
        if state_key:
            save_database_json_state(state_key, payload)
            return
        raise RuntimeError(f"未登记的运行状态 key：{path}")

    @contextmanager
    def _state_file_lock(self, path: Path):
        lock_path = path.with_name(f"{path.name}.lock")
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

    def _load_archive_queue_unlocked(self) -> dict:
        payload = self._read_json(
            ARCHIVE_QUEUE_PATH,
            {"active": [], "queued": [], "recent_failures": []},
        )
        queue = _normalize_archive_queue(payload)
        queue["running_count"] = len(queue["active"])
        queue["queued_count"] = len(queue["queued"])
        queue["failed_count"] = len(queue["recent_failures"])
        return queue

    def _save_archive_queue_unlocked(self, payload: dict) -> dict:
        normalized = _normalize_archive_queue(payload)
        self._write_json(
            ARCHIVE_QUEUE_PATH,
            {
                "active": normalized["active"],
                "queued": normalized["queued"],
                "recent_failures": normalized["recent_failures"],
            },
        )
        return self._load_archive_queue_unlocked()

    def _update_archive_queue(self, updater) -> dict:
        publish_event = True
        with _STATE_LOCK, self._state_file_lock(ARCHIVE_QUEUE_PATH):
            payload = self._load_archive_queue_unlocked()
            before_payload = {
                "active": payload.get("active") or [],
                "queued": payload.get("queued") or [],
                "recent_failures": payload.get("recent_failures") or [],
            }
            before_signature = _state_payload_signature(_normalize_archive_queue(before_payload))
            updated = updater(payload)
            if updated is None:
                updated = payload
            normalized_updated = _normalize_archive_queue(updated)
            after_signature = _state_payload_signature(normalized_updated)
            if before_signature == after_signature:
                result = self._load_archive_queue_unlocked()
                publish_event = False
            else:
                result = self._save_archive_queue_unlocked(normalized_updated)
        if publish_event:
            self._publish_state_event(ARCHIVE_QUEUE_STATE_KEY, result)
        return result

    def _load_missing_3mf_unlocked(self, fallback_items: Optional[list[dict]] = None) -> dict:
        payload = self._read_json(MISSING_3MF_PATH, {"items": []})
        missing = _normalize_missing_3mf(payload, fallback_items=fallback_items)
        missing["count"] = len(missing["items"])
        return missing

    def _save_missing_3mf_unlocked(self, payload: dict) -> dict:
        normalized = _normalize_missing_3mf(payload)
        self._write_json(MISSING_3MF_PATH, normalized)
        return self._load_missing_3mf_unlocked()

    def _update_missing_3mf(self, updater) -> dict:
        with _STATE_LOCK, self._state_file_lock(MISSING_3MF_PATH):
            payload = self._load_missing_3mf_unlocked()
            updated = updater(payload)
            if updated is None:
                updated = payload
            result = self._save_missing_3mf_unlocked(updated)
        self._publish_state_event(MISSING_3MF_STATE_KEY, result)
        return result

    def _load_subscriptions_state_unlocked(self) -> dict:
        payload = self._read_json(SUBSCRIPTIONS_STATE_PATH, {"items": []})
        state = _normalize_subscription_state(payload)
        state["count"] = len(state["items"])
        return state

    def _load_remote_refresh_state_unlocked(self) -> dict:
        payload = self._read_json(
            REMOTE_REFRESH_STATE_PATH,
            {
                "status": "idle",
                "running": False,
                "next_run_at": "",
                "last_run_at": "",
                "last_success_at": "",
                "last_error_at": "",
                "manual_requested_at": "",
                "last_message": "",
                "last_batch_total": 0,
                "last_batch_succeeded": 0,
                "last_batch_failed": 0,
                "last_batch_skipped": 0,
                "last_eligible_total": 0,
                "last_remaining_total": 0,
                "last_skipped_missing_cookie": 0,
                "last_skipped_local_or_invalid": 0,
                "current_item": {},
                "current_items": [],
                "last_batch_metrics": {},
                "last_resource_waits": {},
                "last_slow_models": [],
                "recent_items": [],
            },
        )
        return _normalize_remote_refresh_state(payload)

    def _load_source_refresh_queue_unlocked(self) -> dict:
        payload = self._read_json(
            SOURCE_REFRESH_QUEUE_PATH,
            {"version": 1, "active": [], "queued": [], "recent_failures": [], "updated_at": ""},
        )
        return _normalize_source_refresh_queue(payload)

    def _save_source_refresh_queue_unlocked(self, payload: dict) -> dict:
        normalized = _normalize_source_refresh_queue(payload)
        self._write_json(
            SOURCE_REFRESH_QUEUE_PATH,
            {
                "version": normalized["version"],
                "active": normalized["active"],
                "queued": normalized["queued"],
                "recent_failures": normalized["recent_failures"],
                "updated_at": normalized["updated_at"],
            },
        )
        return self._load_source_refresh_queue_unlocked()

    def _load_source_refresh_runs_unlocked(self) -> dict:
        payload = self._read_json(
            SOURCE_REFRESH_RUNS_PATH,
            {
                "version": 1,
                "active_run": {},
                "last_completed_run": {},
                "last_attempt_at": "",
                "last_deferred_at": "",
                "last_defer_reason": "",
                "last_interrupted_at": "",
                "last_interrupted_reason": "",
                "next_run_at": "",
                "updated_at": "",
            },
        )
        return _normalize_source_refresh_runs(payload)

    def _save_source_refresh_runs_unlocked(self, payload: dict) -> dict:
        normalized = _normalize_source_refresh_runs(payload)
        self._write_json(SOURCE_REFRESH_RUNS_PATH, normalized)
        return self._load_source_refresh_runs_unlocked()

    def _load_organize_tasks_unlocked(self) -> dict:
        payload = self._read_json(ORGANIZE_TASKS_PATH, {"items": []})
        tasks = _normalize_organize_tasks(payload)
        raw_payload = payload if isinstance(payload, dict) else {}
        raw_count = _safe_int(raw_payload.get("count"), 0)
        raw_count_trusted = bool(raw_payload.get("count_trusted"))
        if int(tasks.get("count") or 0) != raw_count or bool(tasks.get("count_trusted")) != raw_count_trusted:
            persisted = dict(tasks)
            self._write_json(ORGANIZE_TASKS_PATH, persisted)
        return tasks

    def _save_organize_tasks_unlocked(self, payload: dict) -> dict:
        base_payload = dict(payload or {}) if isinstance(payload, dict) else {"items": payload if isinstance(payload, list) else []}
        normalized = _normalize_organize_tasks({**base_payload, "count_trusted": True})
        normalized["count_trusted"] = True
        self._write_json(ORGANIZE_TASKS_PATH, normalized)
        return self._load_organize_tasks_unlocked()

    def _update_organize_tasks(self, updater) -> dict:
        with _STATE_LOCK, self._state_file_lock(ORGANIZE_TASKS_PATH):
            payload = self._load_organize_tasks_unlocked()
            updated = updater(payload)
            if updated is None:
                updated = payload
            result = self._save_organize_tasks_unlocked(updated)
        self._publish_state_event(ORGANIZE_TASKS_STATE_KEY, result)
        return result

    def _save_subscriptions_state_unlocked(self, payload: dict) -> dict:
        normalized = _normalize_subscription_state(payload)
        self._write_json(SUBSCRIPTIONS_STATE_PATH, normalized)
        return self._load_subscriptions_state_unlocked()

    def _save_remote_refresh_state_unlocked(self, payload: dict) -> dict:
        normalized = _normalize_remote_refresh_state(payload)
        self._write_json(REMOTE_REFRESH_STATE_PATH, normalized)
        return self._load_remote_refresh_state_unlocked()

    def _update_subscriptions_state(self, updater) -> dict:
        with _STATE_LOCK, self._state_file_lock(SUBSCRIPTIONS_STATE_PATH):
            payload = self._load_subscriptions_state_unlocked()
            updated = updater(payload)
            if updated is None:
                updated = payload
            result = self._save_subscriptions_state_unlocked(updated)
        self._publish_state_event(SUBSCRIPTIONS_STATE_KEY, result)
        return result

    def _update_remote_refresh_state(self, updater, *, publish_event: bool = True) -> dict:
        with _STATE_LOCK, self._state_file_lock(REMOTE_REFRESH_STATE_PATH):
            payload = self._load_remote_refresh_state_unlocked()
            updated = updater(payload)
            if updated is None:
                updated = payload
            result = self._save_remote_refresh_state_unlocked(updated)
        if publish_event:
            self._publish_state_event(REMOTE_REFRESH_STATE_KEY, result)
        return result

    def _update_source_refresh_queue(self, updater, *, publish_event: bool = True) -> dict:
        with _STATE_LOCK, self._state_file_lock(SOURCE_REFRESH_QUEUE_PATH):
            payload = self._load_source_refresh_queue_unlocked()
            updated = updater(payload)
            if updated is None:
                updated = payload
            result = self._save_source_refresh_queue_unlocked(updated)
        if publish_event:
            self._publish_state_event(SOURCE_REFRESH_QUEUE_STATE_KEY, result)
        return result

    def _update_source_refresh_runs(self, updater, *, publish_event: bool = True) -> dict:
        with _STATE_LOCK, self._state_file_lock(SOURCE_REFRESH_RUNS_PATH):
            payload = self._load_source_refresh_runs_unlocked()
            updated = updater(payload)
            if updated is None:
                updated = payload
            result = self._save_source_refresh_runs_unlocked(updated)
        if publish_event:
            self._publish_state_event(SOURCE_REFRESH_RUNS_STATE_KEY, result)
        return result

    def save_archive_queue(self, payload: dict) -> dict:
        with _STATE_LOCK, self._state_file_lock(ARCHIVE_QUEUE_PATH):
            result = self._save_archive_queue_unlocked(payload)
        self._publish_state_event(ARCHIVE_QUEUE_STATE_KEY, result)
        return result

    def save_missing_3mf(self, payload: dict) -> dict:
        with _STATE_LOCK, self._state_file_lock(MISSING_3MF_PATH):
            result = self._save_missing_3mf_unlocked(payload)
        self._publish_state_event(MISSING_3MF_STATE_KEY, result)
        return result

    def load_archive_queue(self) -> dict:
        with _STATE_LOCK:
            return self._load_archive_queue_unlocked()

    def load_missing_3mf(self, fallback_items: Optional[list[dict]] = None) -> dict:
        with _STATE_LOCK:
            return self._load_missing_3mf_unlocked(fallback_items=fallback_items)

    def load_organize_tasks(self) -> dict:
        with _STATE_LOCK:
            return self._load_organize_tasks_unlocked()

    def save_organize_tasks(self, payload: dict) -> dict:
        with _STATE_LOCK, self._state_file_lock(ORGANIZE_TASKS_PATH):
            result = self._save_organize_tasks_unlocked(payload)
        self._publish_state_event(ORGANIZE_TASKS_STATE_KEY, result)
        return result

    def save_subscriptions_state(self, payload: dict) -> dict:
        with _STATE_LOCK, self._state_file_lock(SUBSCRIPTIONS_STATE_PATH):
            result = self._save_subscriptions_state_unlocked(payload)
        self._publish_state_event(SUBSCRIPTIONS_STATE_KEY, result)
        return result

    def load_subscriptions_state(self) -> dict:
        with _STATE_LOCK:
            return self._load_subscriptions_state_unlocked()

    def load_subscriptions_state_summary(self) -> dict:
        with _STATE_LOCK:
            payload = self._read_json(SUBSCRIPTIONS_STATE_PATH, {"items": []})
            state = _normalize_subscription_state_summary(payload)
            state["count"] = len(state["items"])
            return state

    def save_remote_refresh_state(self, payload: dict, *, publish_event: bool = True) -> dict:
        with _STATE_LOCK, self._state_file_lock(REMOTE_REFRESH_STATE_PATH):
            result = self._save_remote_refresh_state_unlocked(payload)
        if publish_event:
            self._publish_state_event(REMOTE_REFRESH_STATE_KEY, result)
        return result

    def load_remote_refresh_state(self) -> dict:
        with _STATE_LOCK:
            return self._load_remote_refresh_state_unlocked()

    def save_source_refresh_queue(self, payload: dict, *, publish_event: bool = True) -> dict:
        with _STATE_LOCK, self._state_file_lock(SOURCE_REFRESH_QUEUE_PATH):
            result = self._save_source_refresh_queue_unlocked(payload)
        if publish_event:
            self._publish_state_event(SOURCE_REFRESH_QUEUE_STATE_KEY, result)
        return result

    def load_source_refresh_queue(self) -> dict:
        with _STATE_LOCK:
            return self._load_source_refresh_queue_unlocked()

    def save_source_refresh_runs(self, payload: dict, *, publish_event: bool = True) -> dict:
        with _STATE_LOCK, self._state_file_lock(SOURCE_REFRESH_RUNS_PATH):
            result = self._save_source_refresh_runs_unlocked(payload)
        if publish_event:
            self._publish_state_event(SOURCE_REFRESH_RUNS_STATE_KEY, result)
        return result

    def load_source_refresh_runs(self) -> dict:
        with _STATE_LOCK:
            return self._load_source_refresh_runs_unlocked()

    def save_model_flags(self, payload: dict) -> dict:
        with _STATE_LOCK, self._state_file_lock(MODEL_FLAGS_PATH):
            normalized = _normalize_model_flags(payload)
            self._write_json(MODEL_FLAGS_PATH, normalized)
            return self.load_model_flags()

    def load_model_flags(self) -> dict:
        with _STATE_LOCK:
            payload = self._read_json(MODEL_FLAGS_PATH, {"favorites": [], "printed": [], "deleted": []})
            flags = _normalize_model_flags(payload)
            flags["favorite_count"] = len(flags["favorites"])
            flags["printed_count"] = len(flags["printed"])
            flags["deleted_count"] = len(flags["deleted"])
            return flags

    def update_model_flag(self, model_dir: str, flag_name: str, active: bool) -> dict:
        clean_model_dir = str(model_dir or "").strip().strip("/")
        if not clean_model_dir:
            return self.load_model_flags()

        if flag_name not in {"favorites", "printed", "deleted"}:
            return self.load_model_flags()

        with _STATE_LOCK:
            payload = self._read_json(MODEL_FLAGS_PATH, {"favorites": [], "printed": [], "deleted": []})
            flags = _normalize_model_flags(payload)
            items = list(flags.get(flag_name) or [])
            item_set = set(items)

            if active:
                if clean_model_dir not in item_set:
                    items.append(clean_model_dir)
            else:
                items = [item for item in items if item != clean_model_dir]

            flags[flag_name] = items
            return self.save_model_flags(flags)

    def remove_model_flags(self, model_dirs: list[str]) -> dict:
        clean_model_dirs = {
            str(item or "").strip().strip("/")
            for item in model_dirs or []
            if str(item or "").strip().strip("/")
        }
        if not clean_model_dirs:
            return self.load_model_flags()

        with _STATE_LOCK:
            payload = self._read_json(MODEL_FLAGS_PATH, {"favorites": [], "printed": [], "deleted": []})
            flags = _normalize_model_flags(payload)
            flags["favorites"] = [item for item in flags.get("favorites") or [] if item not in clean_model_dirs]
            flags["printed"] = [item for item in flags.get("printed") or [] if item not in clean_model_dirs]
            flags["deleted"] = [item for item in flags.get("deleted") or [] if item not in clean_model_dirs]
            return self.save_model_flags(flags)

    def enqueue_archive_task(self, item: dict) -> dict:
        enqueued = False
        merged = False
        existing_task_id = ""
        duplicate_key = ""

        def _mutate(payload: dict) -> dict:
            nonlocal enqueued, merged, existing_task_id, duplicate_key
            active = [_normalize_archive_runtime_item(item, "running") for item in (payload.get("active") or [])]
            queued = [_normalize_archive_runtime_item(item, "queued") for item in (payload.get("queued") or [])]
            target = _normalize_archive_runtime_item(item, "queued")
            target_key = _archive_task_identity_key(target)
            duplicate_key = target_key
            if target_key:
                existing = _archive_existing_identity_map(active + queued, "queued").get(target_key)
                if existing is not None:
                    existing_task_id = str(existing.get("id") or "")
                    merged = _merge_archive_task_instance_scope(existing, target)
                    if merged:
                        existing["updated_at"] = china_now_iso()
                        existing["message"] = str(existing.get("message") or target.get("message") or "")
                        for collection in (active, queued):
                            for index, candidate in enumerate(collection):
                                if str(candidate.get("id") or "") == existing_task_id:
                                    collection[index] = existing
                                    break
                    payload["active"] = active
                    payload["queued"] = queued
                    return payload

            queued.append(target)
            enqueued = True
            payload["active"] = active
            payload["queued"] = queued
            return payload

        queue = self._update_archive_queue(_mutate)
        queue["enqueued"] = enqueued
        queue["merged"] = merged
        if existing_task_id:
            queue["existing_task_id"] = existing_task_id
        if duplicate_key:
            queue["task_identity_key"] = duplicate_key
        return queue

    def start_archive_task(self, task_id: str) -> dict:
        def _mutate(payload: dict) -> dict:
            queued = list(payload.get("queued") or [])
            active = list(payload.get("active") or [])
            task = None
            remaining = []
            now = china_now_iso()
            for item in queued:
                normalized = _normalize_archive_runtime_item(item, "queued")
                if normalized["id"] == task_id and task is None:
                    normalized["status"] = "running"
                    normalized["started_at"] = normalized.get("started_at") or now
                    normalized["heartbeat_at"] = now
                    normalized["last_progress_at"] = now
                    normalized["lease_expires_at"] = lease_expiry_from_now()
                    normalized["attempt_count"] = max(task_attempt_count(normalized) + 1, 1)
                    normalized["updated_at"] = now
                    task = normalized
                    continue
                remaining.append(normalized)

            if task is None:
                current_active = []
                for item in active:
                    normalized = _normalize_archive_runtime_item(item, "running")
                    if normalized["id"] == task_id:
                        normalized["status"] = "running"
                        normalized["heartbeat_at"] = now
                        normalized["last_progress_at"] = now
                        normalized["lease_expires_at"] = lease_expiry_from_now()
                        normalized["updated_at"] = now
                        task = normalized
                    else:
                        current_active.append(normalized)
                active = current_active
            else:
                active = [_normalize_archive_runtime_item(item, "running") for item in active]

            if task is not None:
                active.append(task)

            payload["queued"] = remaining
            payload["active"] = active
            return payload

        return self._update_archive_queue(_mutate)

    def lease_next_archive_task(self, selector=None) -> Optional[dict[str, Any]]:
        leased_task: Optional[dict[str, Any]] = None

        def _mutate(payload: dict) -> dict:
            nonlocal leased_task
            queued = [
                _normalize_archive_runtime_item(item, "queued")
                for item in (payload.get("queued") or [])
            ]
            active = [
                _normalize_archive_runtime_item(item, "running")
                for item in (payload.get("active") or [])
            ]
            selected_index: Optional[int] = None
            selected_task: Optional[dict[str, Any]] = None

            if callable(selector):
                selected_task = selector({"active": active, "queued": queued, "recent_failures": payload.get("recent_failures") or []})
                selected_id = str((selected_task or {}).get("id") or "").strip()
                if selected_id:
                    for index, item in enumerate(queued):
                        if str(item.get("id") or "").strip() == selected_id:
                            selected_index = index
                            break
            elif queued:
                selected_index = 0

            if selected_index is None:
                return payload

            now = china_now_iso()
            task = queued.pop(selected_index)
            task["status"] = "running"
            task["started_at"] = task.get("started_at") or now
            task["heartbeat_at"] = now
            task["last_progress_at"] = now
            task["lease_expires_at"] = lease_expiry_from_now()
            task["attempt_count"] = max(task_attempt_count(task) + 1, 1)
            task["updated_at"] = now
            active.append(task)
            leased_task = task

            payload["queued"] = queued
            payload["active"] = active
            return payload

        self._update_archive_queue(_mutate)
        return leased_task

    def update_active_task(self, task_id: str, **changes: Any) -> dict:
        def _mutate(payload: dict) -> dict:
            active = []
            now = china_now_iso()
            for item in payload.get("active") or []:
                normalized = _normalize_archive_runtime_item(item, "running")
                if normalized["id"] == task_id:
                    normalized.update({key: value for key, value in changes.items() if value is not None})
                    normalized["heartbeat_at"] = now
                    normalized["last_progress_at"] = now
                    normalized["lease_expires_at"] = lease_expiry_from_now()
                    normalized["updated_at"] = now
                active.append(normalized)
            payload["active"] = active
            return payload

        return self._update_archive_queue(_mutate)

    def refresh_recent_active_archive_leases(self) -> dict:
        refreshed_count = 0
        finalized_items: list[dict[str, Any]] = []

        def _mutate(payload: dict) -> dict:
            nonlocal refreshed_count, finalized_items
            active = []
            finalized_items = []
            now = china_now_iso()
            for item in payload.get("active") or []:
                normalized = _normalize_archive_runtime_item(item, "running")
                if _is_completed_archive_running_snapshot(normalized):
                    finalized_items.append(normalized)
                    continue
                status = normalize_runtime_status(normalized.get("status"), "running")
                if (
                    status == "running"
                    and is_lease_expired(normalized.get("lease_expires_at"))
                    and _has_recent_archive_progress(normalized)
                ):
                    normalized["heartbeat_at"] = now
                    normalized["last_progress_at"] = now
                    normalized["lease_expires_at"] = lease_expiry_from_now()
                    normalized["updated_at"] = now
                    refreshed_count += 1
                active.append(normalized)
            payload["active"] = active
            return payload

        queue = self._update_archive_queue(_mutate)
        queue["refreshed_count"] = refreshed_count
        queue["finalized_count"] = len(finalized_items)
        for item in finalized_items:
            self._publish_state_event(
                ARCHIVE_QUEUE_STATE_KEY,
                queue,
                "archive.completed",
                {
                    "id": item.get("id") or "",
                    "mode": item.get("mode") or "",
                    "url": item.get("url") or "",
                    "title": item.get("title") or "",
                },
            )
        return queue

    def complete_archive_task(self, task_id: str, **changes: Any) -> dict:
        completed_item: Optional[dict[str, Any]] = None

        def _mutate(payload: dict) -> dict:
            nonlocal completed_item
            active_items = []
            for item in payload.get("active") or []:
                normalized = _normalize_archive_runtime_item(item, "running")
                if normalized["id"] == task_id:
                    if changes:
                        normalized.update({key: value for key, value in changes.items() if value is not None})
                        normalized["updated_at"] = china_now_iso()
                    completed_item = normalized
                    continue
                active_items.append(normalized)
            payload["active"] = [
                item
                for item in active_items
            ]
            return payload

        queue = self._update_archive_queue(_mutate)
        if completed_item is not None:
            self._publish_state_event(
                ARCHIVE_QUEUE_STATE_KEY,
                queue,
                "archive.completed",
                {
                    "id": completed_item.get("id") or "",
                    "mode": completed_item.get("mode") or "",
                    "url": completed_item.get("url") or "",
                    "title": completed_item.get("title") or "",
                },
            )
        return queue

    def fail_archive_task(self, task_id: str, message: str) -> dict:
        failed_item_payload: Optional[dict[str, Any]] = None

        def _mutate(payload: dict) -> dict:
            nonlocal failed_item_payload
            failed_item = None
            active = []
            for item in payload.get("active") or []:
                normalized = _normalize_task_item(item, "running")
                if normalized["id"] == task_id:
                    normalized["status"] = "failed"
                    normalized["message"] = message
                    normalized["updated_at"] = china_now_iso()
                    failed_item = normalized
                else:
                    active.append(normalized)

            queued = []
            for item in payload.get("queued") or []:
                normalized = _normalize_task_item(item, "queued")
                if normalized["id"] == task_id and failed_item is None:
                    normalized["status"] = "failed"
                    normalized["message"] = message
                    normalized["updated_at"] = china_now_iso()
                    failed_item = normalized
                else:
                    queued.append(normalized)

            recent_failures = [_normalize_task_item(item, "failed") for item in (payload.get("recent_failures") or [])]
            if failed_item is not None:
                failed_key = _archive_task_identity_key(failed_item, failure=True)
                if failed_key:
                    recent_failures = [
                        item
                        for item in recent_failures
                        if _archive_task_identity_key(item, failure=True) != failed_key
                    ]
                recent_failures.insert(0, failed_item)
                recent_failures = recent_failures[:20]
                failed_item_payload = failed_item

            payload["active"] = active
            payload["queued"] = queued
            payload["recent_failures"] = recent_failures
            return payload

        queue = self._update_archive_queue(_mutate)
        if failed_item_payload is not None:
            self._publish_state_event(
                ARCHIVE_QUEUE_STATE_KEY,
                queue,
                "archive.failed",
                {
                    "id": failed_item_payload.get("id") or "",
                    "mode": failed_item_payload.get("mode") or "",
                    "url": failed_item_payload.get("url") or "",
                    "title": failed_item_payload.get("title") or "",
                    "message": failed_item_payload.get("message") or "",
                },
            )
        return queue

    def requeue_active_tasks(self, message: str = "服务重启后自动恢复") -> dict:
        recovered_count = 0
        deduplicated_count = 0
        finalized_items: list[dict[str, Any]] = []

        def _mutate(payload: dict) -> dict:
            nonlocal recovered_count, deduplicated_count, finalized_items
            active_items = [_normalize_archive_runtime_item(item, "running") for item in (payload.get("active") or [])]
            queued_items, initial_deduped = _dedupe_archive_items(list(payload.get("queued") or []), "queued")
            deduplicated_count += initial_deduped
            if not active_items:
                recovered_count = 0
                finalized_items = []
                payload["queued"] = queued_items
                return payload

            recovered = []
            finalized_items = []
            queued_keys = {
                key
                for key in (_archive_task_identity_key(item) for item in queued_items)
                if key
            }
            queued_by_key = {
                key: item
                for item in queued_items
                for key in [_archive_task_identity_key(item)]
                if key
            }
            now = china_now_iso()
            for item in active_items:
                if _is_completed_archive_running_snapshot(item):
                    finalized_items.append(item)
                    continue
                key = _archive_task_identity_key(item)
                if key and key in queued_keys:
                    existing = queued_by_key.get(key)
                    if existing is not None:
                        _merge_archive_task_instance_scope(existing, item)
                    deduplicated_count += 1
                    continue
                item["status"] = "queued"
                item["progress"] = 0
                item["message"] = message
                item["updated_at"] = now
                recovered.append(item)
                if key:
                    queued_keys.add(key)
                    queued_by_key[key] = item

            recovered_count = len(recovered)
            payload["active"] = []
            payload["queued"] = recovered + queued_items
            return payload

        queue = self._update_archive_queue(_mutate)
        queue["recovered_count"] = recovered_count
        queue["deduplicated_count"] = deduplicated_count
        queue["finalized_count"] = len(finalized_items)
        for item in finalized_items:
            self._publish_state_event(
                ARCHIVE_QUEUE_STATE_KEY,
                queue,
                "archive.completed",
                {
                    "id": item.get("id") or "",
                    "mode": item.get("mode") or "",
                    "url": item.get("url") or "",
                    "title": item.get("title") or "",
                },
            )
        return queue

    def repair_archive_queue(self, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS, repair_active: bool = True) -> dict:
        summary = {
            "examined": 0,
            "requeued": 0,
            "failed": 0,
            "finalized": 0,
            "skipped": 0,
            "deduplicated": 0,
            "errors": [],
        }
        finalized_items: list[dict[str, Any]] = []

        def _mutate(payload: dict) -> dict:
            nonlocal finalized_items
            now = china_now_iso()
            active = []
            finalized_items = []
            queued, queued_deduped = _dedupe_archive_items(list(payload.get("queued") or []), "queued")
            recent_failures, failure_deduped = _dedupe_archive_items(list(payload.get("recent_failures") or []), "failed", failure=True)
            summary["deduplicated"] += queued_deduped + failure_deduped
            queued_keys = {
                key
                for key in (_archive_task_identity_key(item) for item in queued)
                if key
            }

            if not repair_active:
                payload["active"] = [
                    _normalize_archive_runtime_item(item, "running")
                    for item in (payload.get("active") or [])
                ]
                payload["queued"] = queued
                payload["recent_failures"] = recent_failures[:20]
                return payload

            for item in payload.get("active") or []:
                normalized = _normalize_archive_runtime_item(item, "running")
                status = normalize_runtime_status(normalized.get("status"), "running")
                summary["examined"] += 1

                if _is_completed_archive_running_snapshot(normalized):
                    summary["finalized"] += 1
                    finalized_items.append(normalized)
                    continue

                if status in {"paused", "waiting_children", "blocked"}:
                    summary["skipped"] += 1
                    active.append(normalized)
                    continue

                if status != "running" or not is_lease_expired(normalized.get("lease_expires_at")):
                    summary["skipped"] += 1
                    active.append(normalized)
                    continue

                if _has_recent_archive_progress(normalized):
                    normalized["heartbeat_at"] = now
                    normalized["last_progress_at"] = now
                    normalized["lease_expires_at"] = lease_expiry_from_now()
                    normalized["updated_at"] = now
                    summary["skipped"] += 1
                    active.append(normalized)
                    continue

                normalized["updated_at"] = now
                normalized["heartbeat_at"] = ""
                normalized["lease_expires_at"] = ""

                if task_attempts_remaining(normalized, max_attempts=max_attempts):
                    normalized["status"] = "queued"
                    normalized["message"] = "检测到任务心跳过期，已重新排队。"
                    key = _archive_task_identity_key(normalized)
                    if key and key in queued_keys:
                        summary["deduplicated"] += 1
                        continue
                    queued.insert(0, normalized)
                    if key:
                        queued_keys.add(key)
                    summary["requeued"] += 1
                else:
                    normalized["status"] = "failed"
                    normalized["message"] = "任务心跳过期且已达到最大重试次数。"
                    failed_key = _archive_task_identity_key(normalized, failure=True)
                    if failed_key:
                        recent_failures = [
                            item
                            for item in recent_failures
                            if _archive_task_identity_key(item, failure=True) != failed_key
                        ]
                    recent_failures.insert(0, normalized)
                    summary["failed"] += 1

            payload["active"] = active
            payload["queued"] = queued
            payload["recent_failures"] = recent_failures[:20]
            return payload

        queue = self._update_archive_queue(_mutate)
        for item in finalized_items:
            self._publish_state_event(
                ARCHIVE_QUEUE_STATE_KEY,
                queue,
                "archive.completed",
                {
                    "id": item.get("id") or "",
                    "mode": item.get("mode") or "",
                    "url": item.get("url") or "",
                    "title": item.get("title") or "",
                },
            )
        return {"summary": summary, "queue": queue}

    def remove_recent_failures_for_model(self, model_id: str, url: str = "") -> dict:
        model_key = str(model_id or "").strip()
        url_key = str(url or "").strip()
        if not model_key and not url_key:
            return self.load_archive_queue()

        def _mutate(payload: dict) -> dict:
            remaining = []
            for item in payload.get("recent_failures") or []:
                normalized = _normalize_task_item(item, "failed")
                haystack = " ".join(
                    [
                        str(normalized.get("url") or ""),
                        str(normalized.get("title") or ""),
                        str(normalized.get("id") or ""),
                    ]
                )
                if model_key and model_key in haystack:
                    continue
                if url_key and (normalized.get("url") == url_key or normalized.get("title") == url_key):
                    continue
                remaining.append(normalized)

            payload["recent_failures"] = remaining
            return payload

        return self._update_archive_queue(_mutate)

    def clear_archive_recent_failures(self) -> dict:
        cleared_count = 0

        def _mutate(payload: dict) -> dict:
            nonlocal cleared_count
            cleared_count = len(payload.get("recent_failures") or [])
            payload["recent_failures"] = []
            return payload

        queue = self._update_archive_queue(_mutate)
        queue["cleared_count"] = cleared_count
        return queue

    def merge_missing_3mf_items(self, items: list[dict]) -> dict:
        def _mutate(payload: dict) -> dict:
            existing = list(payload.get("items") or [])
            merged: list[dict] = []
            seen = set()

            for item in existing + (items or []):
                normalized_list = _normalize_missing_3mf([item]).get("items", [])
                if not normalized_list:
                    continue
                normalized = normalized_list[0]
                key = _missing_3mf_key(normalized)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(normalized)

            return {"items": merged}

        return self._update_missing_3mf(_mutate)

    def replace_missing_3mf_for_model(self, model_id: str, items: list[dict]) -> dict:
        model_key = str(model_id or "").strip()

        def _mutate(payload: dict) -> dict:
            existing = _normalize_missing_3mf(payload).get("items", [])
            remaining = [
                item for item in existing
                if str(item.get("model_id") or "").strip() != model_key or not model_key
            ]
            merged: list[dict] = []
            seen = set()
            for item in remaining + (items or []):
                normalized_list = _normalize_missing_3mf([item]).get("items", [])
                if not normalized_list:
                    continue
                normalized = normalized_list[0]
                key = _missing_3mf_key(normalized)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(normalized)
            return {"items": merged}

        return self._update_missing_3mf(_mutate)

    def replace_missing_3mf_for_models(self, items_by_model: dict[str, list[dict]]) -> dict:
        normalized_groups: dict[str, list[dict]] = {}
        for raw_model_id, raw_items in (items_by_model or {}).items():
            model_key = str(raw_model_id or "").strip()
            if not model_key:
                continue

            merged: list[dict] = []
            seen = set()
            for item in raw_items or []:
                normalized_list = _normalize_missing_3mf([item]).get("items", [])
                if not normalized_list:
                    continue
                normalized = normalized_list[0]
                if not str(normalized.get("model_id") or "").strip():
                    normalized["model_id"] = model_key
                key = _missing_3mf_key(normalized)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(normalized)

            normalized_groups[model_key] = merged

        if not normalized_groups:
            return self.load_missing_3mf()

        def _mutate(payload: dict) -> dict:
            existing = _normalize_missing_3mf(payload).get("items", [])
            remaining = [
                item
                for item in existing
                if str(item.get("model_id") or "").strip() not in normalized_groups
            ]

            merged: list[dict] = []
            seen = set()
            for item in remaining:
                normalized_list = _normalize_missing_3mf([item]).get("items", [])
                if not normalized_list:
                    continue
                normalized = normalized_list[0]
                key = _missing_3mf_key(normalized)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(normalized)

            for model_key in normalized_groups:
                for item in normalized_groups.get(model_key) or []:
                    normalized_list = _normalize_missing_3mf([item]).get("items", [])
                    if not normalized_list:
                        continue
                    normalized = normalized_list[0]
                    if not str(normalized.get("model_id") or "").strip():
                        normalized["model_id"] = model_key
                    key = _missing_3mf_key(normalized)
                    if key in seen:
                        continue
                    seen.add(key)
                    merged.append(normalized)

            return {"items": merged}

        return self._update_missing_3mf(_mutate)

    def remove_missing_3mf_for_model(self, model_id: str) -> dict:
        return self.replace_missing_3mf_for_model(model_id, [])

    def remove_missing_3mf_item(
        self,
        *,
        model_id: str = "",
        title: str = "",
        instance_id: str = "",
        model_url: str = "",
    ) -> dict:
        def _mutate(payload: dict) -> dict:
            items = _normalize_missing_3mf(payload).get("items", [])
            remaining = [
                item for item in items
                if not _matches_missing_3mf_item(
                    item,
                    model_id=model_id,
                    title=title,
                    instance_id=instance_id,
                    model_url=model_url,
                )
            ]
            return {"items": remaining}

        return self._update_missing_3mf(_mutate)

    def upsert_organize_task(self, item: dict, limit: int = 50) -> dict:
        normalized_items = _normalize_organize_tasks({"items": [item]}).get("items", [])
        if not normalized_items:
            return self.load_organize_tasks()

        target = normalized_items[0]
        target_id = target.get("id") or target.get("fingerprint") or target.get("source_path")
        if not target_id:
            target_id = f"{target.get('source_dir')}::{target.get('target_dir')}::{target.get('title')}"
            target["id"] = target_id

        def _mutate(payload: dict) -> dict:
            raw_items = payload.get("items") or []
            total_count = _safe_int(payload.get("count"), len(raw_items))
            items = []
            replaced = False
            for existing in payload.get("items") or []:
                normalized = _normalize_organize_tasks({"items": [existing]}).get("items", [])
                if not normalized:
                    continue
                current = normalized[0]
                current_id = current.get("id") or current.get("fingerprint") or current.get("source_path")
                if current_id == target_id:
                    items.append(target)
                    replaced = True
                else:
                    items.append(current)

            if not replaced:
                items.insert(0, target)
                total_count += 1

            return {
                **payload,
                "items": items[: max(int(limit or 0), 1)],
                "count": total_count,
            }

        tasks = self._update_organize_tasks(_mutate)
        if str(target.get("status") or "").strip().lower() == "success":
            self._publish_state_event(
                ORGANIZE_TASKS_STATE_KEY,
                tasks,
                "organize.completed",
                {
                    "id": target.get("id") or target_id,
                    "title": target.get("title") or "",
                    "model_dir": target.get("model_dir") or "",
                    "source_path": target.get("source_path") or "",
                    "snapshot_ready": bool(target.get("snapshot_ready")),
                },
            )
        return tasks

    def update_missing_3mf_status(
        self,
        model_id: str,
        title: str = "",
        instance_id: str = "",
        model_url: str = "",
        status: str = "",
        message: str = "",
    ) -> dict:
        def _mutate(payload: dict) -> dict:
            items = _normalize_missing_3mf(payload).get("items", [])
            now = china_now_iso()

            for item in items:
                if not _matches_missing_3mf_item(
                    item,
                    model_id=model_id,
                    title=title,
                    instance_id=instance_id,
                    model_url=model_url,
                ):
                    continue
                if status:
                    item["status"] = status
                if message:
                    item["message"] = message
                item["updated_at"] = now

            return {"items": items}

        return self._update_missing_3mf(_mutate)

    def mark_missing_3mf_retrying(
        self,
        items: list[dict],
        *,
        status: str = "queued",
        message: str = "等待重新下载 3MF",
    ) -> dict:
        targets = _normalize_missing_3mf({"items": items or []}).get("items", [])
        if not targets:
            return self.load_missing_3mf()

        target_keys = {_missing_3mf_key(item) for item in targets}
        target_urls = {
            str(item.get("model_url") or "").strip()
            for item in targets
            if str(item.get("model_url") or "").strip()
        }

        def _mutate(payload: dict) -> dict:
            existing = _normalize_missing_3mf(payload).get("items", [])
            now = china_now_iso()
            for item in existing:
                key = _missing_3mf_key(item)
                item_url = str(item.get("model_url") or "").strip()
                if key not in target_keys and item_url not in target_urls:
                    continue
                item["status"] = status
                item["message"] = message
                item["updated_at"] = now
            return {"items": existing}

        return self._update_missing_3mf(_mutate)

    def upsert_subscription_state(self, item: dict) -> dict:
        normalized_item = _normalize_subscription_state({"items": [item]}).get("items", [])
        if not normalized_item:
            return self.load_subscriptions_state()
        target = normalized_item[0]
        target_id = target["id"]

        def _mutate(payload: dict) -> dict:
            items = list(payload.get("items") or [])
            replaced = False
            merged: list[dict] = []
            for existing in items:
                if str(existing.get("id") or "") == target_id:
                    merged.append(target)
                    replaced = True
                else:
                    merged.append(existing)
            if not replaced:
                merged.append(target)
            return {"items": merged}

        return self._update_subscriptions_state(_mutate)

    def patch_subscription_state(self, subscription_id: str, **changes: Any) -> dict:
        target_id = str(subscription_id or "").strip()
        if not target_id:
            return self.load_subscriptions_state()

        def _mutate(payload: dict) -> dict:
            items = _normalize_subscription_state(payload).get("items", [])
            updated_items: list[dict] = []
            found = False
            for existing in items:
                if str(existing.get("id") or "") != target_id:
                    updated_items.append(existing)
                    continue
                merged = dict(existing)
                for key, value in changes.items():
                    if value is None:
                        continue
                    merged[key] = value
                updated_items.append(merged)
                found = True

            if not found:
                baseline = {
                    "id": target_id,
                    "status": "idle",
                    "running": False,
                    "next_run_at": "",
                    "manual_requested_at": "",
                    "last_run_at": "",
                    "last_success_at": "",
                    "last_error_at": "",
                    "last_message": "",
                    "last_discovered_count": 0,
                    "last_new_count": 0,
                    "last_enqueued_count": 0,
                    "last_deleted_count": 0,
                    "current_items": [],
                    "tracked_items": [],
                }
                for key, value in changes.items():
                    if value is None:
                        continue
                    baseline[key] = value
                updated_items.append(baseline)

            return {"items": updated_items}

        return self._update_subscriptions_state(_mutate)

    def remove_subscription_state(self, subscription_ids: list[str]) -> dict:
        clean_ids = {
            str(item or "").strip()
            for item in subscription_ids or []
            if str(item or "").strip()
        }
        if not clean_ids:
            return self.load_subscriptions_state()

        def _mutate(payload: dict) -> dict:
            items = _normalize_subscription_state(payload).get("items", [])
            remaining = [item for item in items if str(item.get("id") or "") not in clean_ids]
            return {"items": remaining}

        return self._update_subscriptions_state(_mutate)

    def patch_remote_refresh_state(self, *, publish_event: bool = True, **changes: Any) -> dict:
        def _mutate(payload: dict) -> dict:
            merged = dict(_normalize_remote_refresh_state(payload))
            for key, value in changes.items():
                if value is None:
                    continue
                if key == "current_item":
                    if isinstance(value, dict) and value:
                        merged[key] = _normalize_task_item(value, "running")
                    elif not value:
                        merged[key] = {}
                        if "current_items" not in changes:
                            merged["current_items"] = []
                    continue
                if key == "current_items":
                    if isinstance(value, list):
                        merged[key] = [
                            _normalize_task_item(item, "running")
                            for item in value
                            if isinstance(item, (dict, str))
                        ][:8]
                    elif not value:
                        merged[key] = []
                    continue
                merged[key] = value
            return merged

        return self._update_remote_refresh_state(_mutate, publish_event=publish_event)

    def patch_source_refresh_runs(self, *, publish_event: bool = True, **changes: Any) -> dict:
        def _mutate(payload: dict) -> dict:
            merged = dict(_normalize_source_refresh_runs(payload))
            for key, value in changes.items():
                if value is None:
                    continue
                if key == "active_run":
                    merged[key] = _normalize_source_refresh_run(value, "running")
                    continue
                if key == "last_completed_run":
                    merged[key] = _normalize_source_refresh_run(value, "completed")
                    continue
                merged[key] = value
            merged["updated_at"] = china_now_iso()
            return merged

        return self._update_source_refresh_runs(_mutate, publish_event=publish_event)

    def append_remote_refresh_history(self, item: dict, limit: int = 50, *, publish_event: bool = True) -> dict:
        normalized_list = _normalize_remote_refresh_state({"recent_items": [item]}).get("recent_items", [])
        if not normalized_list:
            return self.load_remote_refresh_state()
        target = normalized_list[0]

        def _mutate(payload: dict) -> dict:
            recent_items = [target]
            for existing in payload.get("recent_items") or []:
                normalized = _normalize_task_item(existing, "idle")
                if normalized["id"] and normalized["id"] == target["id"]:
                    continue
                recent_items.append(normalized)
            normalized_payload = dict(payload)
            normalized_payload["recent_items"] = recent_items[: max(int(limit or 0), 1)]
            return normalized_payload

        return self._update_remote_refresh_state(_mutate, publish_event=publish_event)
