import copy
import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from croniter import CroniterBadCronError, croniter

from app.core.settings import ARCHIVE_DIR, LOGS_DIR
from app.core.store import JsonStore
from app.core.timezone import ensure_timezone, from_timestamp as china_from_timestamp, now as china_now, now_iso as china_now_iso, parse_datetime
from app.services.cookie_utils import sanitize_cookie_header
from app.services.archive_worker import (
    _activate_three_mf_limit_guard,
    _is_three_mf_limit_guard_active_for_url,
    _read_three_mf_limit_guard,
    _three_mf_limit_message,
    detect_archive_mode,
)
from app.services.batch_discovery import normalize_source_url
from app.services.business_logs import append_business_log
from app.services.catalog import (
    get_archive_snapshot,
    invalidate_archive_snapshot,
    invalidate_model_detail_cache,
    upsert_archive_snapshot_model,
)
from app.services.legacy_archiver import COMMENT_SCHEMA_VERSION, normalize_threaded_comments
from app.services.process_jobs import run_archive_model_job, run_source_deleted_check_job
from app.services.resource_limiter import resource_snapshot, resource_slot
from app.services.task_state import TaskStateStore, is_metadata_only_missing_3mf_placeholder
from app.services.three_mf import describe_three_mf_failure, normalize_makerworld_source, resolve_model_instance_files


REMOTE_REFRESH_LOG_PATH = LOGS_DIR / "remote_refresh.log"
REMOTE_REFRESH_POLL_SECONDS = 20
DEFAULT_REMOTE_REFRESH_CRON = "0 0 * * *"
DEFAULT_REMOTE_REFRESH_MODEL_WORKERS = 2
MAX_REMOTE_REFRESH_MODEL_WORKERS = 4


def _remote_refresh_model_workers(config: Any = None) -> int:
    advanced = getattr(config, "advanced", None)
    configured = getattr(advanced, "remote_refresh_model_workers", None)
    try:
        value = int(configured if configured is not None else os.environ.get("MAKERHUB_REMOTE_REFRESH_MODEL_WORKERS") or DEFAULT_REMOTE_REFRESH_MODEL_WORKERS)
    except (TypeError, ValueError):
        value = DEFAULT_REMOTE_REFRESH_MODEL_WORKERS
    return max(1, min(value, MAX_REMOTE_REFRESH_MODEL_WORKERS))


def _now() -> datetime:
    return china_now()


def _now_iso() -> str:
    return china_now_iso()


def _parse_iso(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    return parse_datetime(raw)


def _parse_ts(value: Any) -> int:
    parsed = _parse_iso(value)
    if parsed is not None:
        return int(parsed.timestamp())
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _looks_like_html_error(text: str) -> bool:
    head = str(text or "").strip().lower()[:1200]
    if not head:
        return False
    if head.startswith("<!doctype html") or "<html" in head:
        return True
    return bool(re.search(r"<(html|head|body|script|title|div|meta|style)\b", head))


def _sanitize_remote_refresh_message(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    if _looks_like_html_error(text):
        lowered = text.lower()
        if any(token in lowered for token in ("cloudflare", "cf-browser-verification", "cf-chl", "__cf_bm", "cf_clearance")):
            return "源端刷新返回了风控校验页，通常是 Cookie 失效、代理异常或站点触发了 Cloudflare 校验。"
        return "源端刷新返回了 HTML 页面，通常是 Cookie 失效、代理错误或站点风控页。"
    text = re.sub(r"\s+", " ", text).strip()
    return text[:400]


def _validate_cron(value: str) -> str:
    clean = str(value or "").strip() or DEFAULT_REMOTE_REFRESH_CRON
    try:
        croniter(clean, _now())
    except (CroniterBadCronError, ValueError) as exc:
        raise ValueError(f"Cron 表达式无效：{exc}") from exc
    return clean


def _next_run_at(cron_expr: str, base: Optional[datetime] = None) -> str:
    normalized = _validate_cron(cron_expr)
    return ensure_timezone(croniter(normalized, base or _now()).get_next(datetime)).isoformat()


def _append_remote_refresh_log(event: str, **payload: Any) -> None:
    try:
        REMOTE_REFRESH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with REMOTE_REFRESH_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"time": _now_iso(), "event": event, **payload}, ensure_ascii=False) + "\n")
    except Exception:
        return


def _select_cookie(url: str, config) -> str:
    cookie_map = {item.platform: item.cookie for item in config.cookies}
    normalized_url = normalize_source_url(url)
    lowered = normalized_url.lower()
    if "makerworld.com/" in lowered and "makerworld.com.cn" not in lowered:
        return sanitize_cookie_header(cookie_map.get("global") or "")
    return sanitize_cookie_header(cookie_map.get("cn") or "")


@contextmanager
def _temporary_proxy_env(config):
    if not getattr(config.proxy, "enabled", False):
        yield
        return

    previous = {
        "HTTP_PROXY": os.environ.get("HTTP_PROXY"),
        "HTTPS_PROXY": os.environ.get("HTTPS_PROXY"),
        "NO_PROXY": os.environ.get("NO_PROXY"),
        "http_proxy": os.environ.get("http_proxy"),
        "https_proxy": os.environ.get("https_proxy"),
        "no_proxy": os.environ.get("no_proxy"),
    }

    if config.proxy.http_proxy:
        os.environ["HTTP_PROXY"] = config.proxy.http_proxy
        os.environ["http_proxy"] = config.proxy.http_proxy
    if config.proxy.https_proxy:
        os.environ["HTTPS_PROXY"] = config.proxy.https_proxy
        os.environ["https_proxy"] = config.proxy.https_proxy
    if config.proxy.no_proxy:
        os.environ["NO_PROXY"] = config.proxy.no_proxy
        os.environ["no_proxy"] = config.proxy.no_proxy

    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _comment_key(comment: Any) -> str:
    if not isinstance(comment, dict):
        return ""
    value = str(comment.get("id") or "").strip()
    if value:
        return value
    author = comment.get("author") if isinstance(comment.get("author"), dict) else {}
    digest = hashlib.sha1(
        "|".join(
            [
                str(author.get("name") or comment.get("author") or "").strip(),
                str(comment.get("createdAt") or comment.get("time") or "").strip(),
                str(comment.get("content") or comment.get("comment") or comment.get("text") or "").strip(),
            ]
        ).encode("utf-8", errors="ignore")
    ).hexdigest()
    return digest[:16]


def _comment_reply_items(comment: Any) -> list[dict[str, Any]]:
    if not isinstance(comment, dict):
        return []
    replies = comment.get("replies")
    if not isinstance(replies, list):
        return []
    return [item for item in replies if isinstance(item, dict)]


def _count_comment_threads(comments: list[Any]) -> int:
    total = 0
    for item in comments or []:
        if not isinstance(item, dict):
            continue
        total += 1 + _count_comment_threads(_comment_reply_items(item))
    return total


def _merge_single_comment(existing: dict[str, Any], fresh: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(existing)
    for key, value in fresh.items():
        if key == "replies":
            continue
        if value in (None, "", [], {}):
            continue
        merged[key] = copy.deepcopy(value)

    merged_replies, _ = _merge_comments(_comment_reply_items(existing), _comment_reply_items(fresh))
    if merged_replies:
        merged["replies"] = merged_replies
        merged["replyCount"] = max(
            len(merged_replies),
            int(existing.get("replyCount") or existing.get("reply_count") or 0),
            int(fresh.get("replyCount") or fresh.get("reply_count") or 0),
        )
    return merged


def _merge_comments(existing_comments: list[Any], fresh_comments: list[Any]) -> tuple[list[dict[str, Any]], int]:
    merged: list[dict[str, Any]] = []
    merged_by_key: dict[str, dict[str, Any]] = {}
    fresh_count = 0

    for raw in fresh_comments or []:
        if not isinstance(raw, dict):
            continue
        item = copy.deepcopy(raw)
        key = _comment_key(item)
        if not key:
            continue
        item["id"] = key
        if key in merged_by_key:
            merged_comment = _merge_single_comment(merged_by_key[key], item)
            merged_by_key[key].clear()
            merged_by_key[key].update(merged_comment)
            continue
        merged.append(item)
        merged_by_key[key] = item
        fresh_count += 1

    added_count = 0
    for raw in existing_comments or []:
        if not isinstance(raw, dict):
            continue
        item = copy.deepcopy(raw)
        key = _comment_key(item)
        if not key:
            continue
        item["id"] = key
        if key in merged_by_key:
            merged_comment = _merge_single_comment(merged_by_key[key], item)
            merged_by_key[key].clear()
            merged_by_key[key].update(merged_comment)
            continue
        merged.append(item)
        merged_by_key[key] = item
        added_count += 1

    merged_total = _count_comment_threads(merged)
    fresh_total = _count_comment_threads(fresh_comments or [])
    return merged, max(merged_total - fresh_total, 0 if fresh_count else added_count)


def _instance_key(instance: Any) -> str:
    if not isinstance(instance, dict):
        return ""
    for field in ("id", "profileId", "instanceId", "fileName", "name", "title"):
        value = str(instance.get(field) or "").strip()
        if value:
            return value
    return ""


def _instance_match_tokens(instance: Any) -> set[tuple[str, str]]:
    if not isinstance(instance, dict):
        return set()
    tokens: set[tuple[str, str]] = set()
    for field in ("id", "profileId", "instanceId"):
        value = str(instance.get(field) or "").strip()
        if value:
            tokens.add(("id", value))
    for field in ("title", "name"):
        value = str(instance.get(field) or "").strip()
        if value:
            tokens.add(("title", value))
    return tokens


def _missing_3mf_match_tokens(item: Any) -> set[tuple[str, str]]:
    if not isinstance(item, dict):
        return set()
    tokens: set[tuple[str, str]] = set()
    instance_id = str(item.get("instance_id") or item.get("profileId") or item.get("instanceId") or "").strip()
    if instance_id:
        tokens.add(("id", instance_id))
    title = str(item.get("title") or item.get("name") or "").strip()
    if title:
        tokens.add(("title", title))
    return tokens


def _split_new_instance_missing_3mf_items(
    items: list[dict[str, Any]],
    added_instance_tokens: set[tuple[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not items or not added_instance_tokens:
        return [], list(items or [])

    pending_download: list[dict[str, Any]] = []
    remaining_missing: list[dict[str, Any]] = []
    for item in items:
        item_tokens = _missing_3mf_match_tokens(item)
        item_id_tokens = {token for token in item_tokens if token[0] == "id"}
        if (
            (item_id_tokens and item_id_tokens & added_instance_tokens)
            or (not item_id_tokens and item_tokens & added_instance_tokens)
        ):
            pending_download.append(item)
        else:
            remaining_missing.append(item)
    return pending_download, remaining_missing


def _merge_instance_record(existing_item: dict[str, Any], fresh_item: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(fresh_item)

    # 源端刷新有时能拿到实例卡片信息，但拿不到 3MF 下载地址。
    # 这种情况下不能把本地已经存在的 3MF 元信息冲掉。
    if not str(merged.get("downloadUrl") or "").strip():
        for field in ("downloadUrl", "fileName", "name", "apiUrl", "downloadState", "downloadMessage"):
            value = existing_item.get(field)
            if value not in ("", None):
                merged[field] = copy.deepcopy(value)

    for field in ("profileId", "instanceId", "title", "titleTranslated", "publishTime"):
        if merged.get(field) in ("", None) and existing_item.get(field) not in ("", None):
            merged[field] = copy.deepcopy(existing_item.get(field))

    return merged


def _missing_3mf_message_from_instance(instance: dict[str, Any], *, source: str = "", url: str = "") -> tuple[str, str]:
    download_state = str(instance.get("downloadState") or "").strip()
    download_message = str(instance.get("downloadMessage") or "").strip()

    if download_state or download_message:
        return "missing", describe_three_mf_failure(download_state, download_message, source=source, url=url)
    return "missing", "等待重新下载"


def _build_missing_3mf_items(
    meta_path: Path,
    meta: dict[str, Any],
    resolved_files: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    model_root = meta_path.parent
    model_id = str(meta.get("id") or "").strip()
    model_url = normalize_source_url(str(meta.get("url") or ""))
    model_source = normalize_makerworld_source(meta.get("source"), model_url)
    model_title = str(meta.get("title") or meta.get("baseName") or "").strip()
    if not isinstance(resolved_files, dict):
        resolved_files = resolve_model_instance_files(meta, model_root)
    resolved_matches = resolved_files.get("matches") if isinstance(resolved_files, dict) else {}
    seen: set[tuple[str, str]] = set()
    items: list[dict[str, Any]] = []

    raw_instances = meta.get("instances") if isinstance(meta.get("instances"), list) else []
    for index, instance in enumerate(raw_instances):
        if not isinstance(instance, dict):
            continue
        resolved_match = resolved_matches.get(index) if isinstance(resolved_matches, dict) else None
        resolved_path = resolved_match.get("path") if isinstance(resolved_match, dict) else None
        if isinstance(resolved_path, Path) and resolved_path.exists():
            continue
        if is_metadata_only_missing_3mf_placeholder(instance):
            continue
        instance_id = str(instance.get("id") or instance.get("profileId") or instance.get("instanceId") or "").strip()
        title = str(instance.get("title") or instance.get("name") or model_title).strip()
        key = (instance_id, title)
        if key in seen:
            continue
        seen.add(key)
        item_status, item_message = _missing_3mf_message_from_instance(instance, source=model_source, url=model_url)
        items.append(
            {
                "model_id": model_id,
                "model_url": model_url,
                "title": title,
                "instance_id": instance_id,
                "status": item_status,
                "message": item_message,
                "updated_at": _now_iso(),
            }
        )

    return items


def _merge_instances(existing_instances: list[Any], fresh_instances: list[Any], checked_at: str) -> tuple[list[dict[str, Any]], int]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    existing_by_key: dict[str, dict[str, Any]] = {}

    for raw in existing_instances or []:
        if not isinstance(raw, dict):
            continue
        key = _instance_key(raw)
        if key and key not in existing_by_key:
            existing_by_key[key] = copy.deepcopy(raw)

    for raw in fresh_instances or []:
        if not isinstance(raw, dict):
            continue
        item = copy.deepcopy(raw)
        key = _instance_key(item)
        if not key:
            key = hashlib.sha1(json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="ignore")).hexdigest()[:16]
        if key in seen:
            continue
        existing_item = existing_by_key.get(key)
        if existing_item:
            item = _merge_instance_record(existing_item, item)
        item["sourceDeleted"] = False
        item["sourceDeletedAt"] = ""
        item["sourceDeletedMessage"] = ""
        seen.add(key)
        merged.append(item)

    deleted_count = 0
    for raw in existing_instances or []:
        if not isinstance(raw, dict):
            continue
        item = copy.deepcopy(raw)
        key = _instance_key(item)
        if not key or key in seen:
            continue
        item["sourceDeleted"] = True
        item["sourceDeletedAt"] = str(item.get("sourceDeletedAt") or checked_at)
        item["sourceDeletedMessage"] = "源端已删除该打印配置，本地归档保留现有文件。"
        seen.add(key)
        merged.append(item)
        deleted_count += 1

    return merged, deleted_count


def _remote_sync_payload(previous: Any) -> dict[str, Any]:
    if isinstance(previous, dict):
        return copy.deepcopy(previous)
    return {}


def _remote_sync_value(remote_sync: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = remote_sync.get(key)
        if value not in ("", None):
            return value
    return ""


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [copy.deepcopy(item) for item in value if isinstance(item, dict)]


def _attachment_key(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return ""
    for field in ("id", "attachmentId", "fileName", "localName", "name", "title", "downloadUrl", "url"):
        value = str(item.get(field) or "").strip()
        if value:
            return value
    return hashlib.sha1(json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="ignore")).hexdigest()[:16]


def _count_added_items(existing_items: list[Any], fresh_items: list[Any], key_builder) -> int:
    existing_keys = {key_builder(item) for item in existing_items or []}
    fresh_keys = {key_builder(item) for item in fresh_items or []}
    existing_keys.discard("")
    fresh_keys.discard("")
    if not fresh_keys:
        return 0
    return max(len(fresh_keys - existing_keys), 0)


def _summary_signature(value: Any) -> str:
    if isinstance(value, dict):
        parts = []
        for key in ("text", "raw", "html"):
            content = str(value.get(key) or "").strip()
            if content:
                parts.append(content)
        return "\n".join(parts)
    return str(value or "").strip()


def _build_change_labels(
    *,
    added_comments: int = 0,
    added_instances: int = 0,
    deleted_instances: int = 0,
    attachments_added: int = 0,
    summary_changed: bool = False,
) -> list[str]:
    labels: list[str] = []
    if added_comments > 0:
        labels.append(f"评论 +{added_comments}")
    if added_instances > 0:
        labels.append(f"打印配置 +{added_instances}")
    if deleted_instances > 0:
        labels.append(f"配置源删标记 {deleted_instances}")
    if attachments_added > 0:
        labels.append(f"附件 +{attachments_added}")
    if summary_changed:
        labels.append("简介已更新")
    if not labels:
        labels.append("已检查，无远端变化")
    return labels


def _build_success_message(change_labels: list[str]) -> str:
    effective_labels = [label for label in change_labels if label != "已检查，无远端变化"]
    if not effective_labels:
        return "源端刷新完成，已检查，未发现远端内容变化。"
    return f"源端刷新完成：{'，'.join(effective_labels)}。"


def _history_id(model_dir: str, status: str) -> str:
    return f"{model_dir}:{status}:{time.time_ns()}"


def _batch_scope_message(*, eligible_total: int, remaining_total: int) -> str:
    if eligible_total <= 0:
        return "当前没有可刷新的远端模型。"
    return (
        f"当前可刷新 {eligible_total} 个模型，"
        f"剩余 {max(int(remaining_total or 0), 0)} 个待补跑。"
    )


def _empty_batch_metrics() -> dict[str, Any]:
    return {
        "comments": 0,
        "comment_roots": 0,
        "replies": 0,
        "comment_images": 0,
        "avatar_urls": 0,
        "shared_avatar_refs": 0,
        "avatar_cache_hits": 0,
        "avatar_migrated": 0,
        "download_tasks": 0,
        "deduped_downloads": 0,
        "new_3mf_download_queued": 0,
        "total_duration_ms": 0,
        "archive_duration_ms": 0,
        "finalize_duration_ms": 0,
        "disk_wait_ms": 0,
    }


def _merge_batch_metrics(metrics: dict[str, Any], item_metrics: dict[str, Any]) -> None:
    if not isinstance(item_metrics, dict):
        return
    for key in list(metrics.keys()):
        value = item_metrics.get(key)
        if isinstance(value, (int, float)):
            metrics[key] = round(float(metrics.get(key) or 0) + float(value), 1)


def _top_slow_models(items: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    sorted_items = sorted(
        [item for item in items if isinstance(item, dict)],
        key=lambda item: float(item.get("total_duration_ms") or 0),
        reverse=True,
    )
    return sorted_items[: max(int(limit or 1), 1)]


def _archive_comment_metrics(archive_result: dict[str, Any]) -> dict[str, int]:
    stats = archive_result.get("stats") if isinstance(archive_result.get("stats"), dict) else {}
    comments = stats.get("comments") if isinstance(stats.get("comments"), dict) else {}
    return {
        "comments": int(comments.get("comment_total") or 0),
        "comment_roots": int(comments.get("comment_roots") or 0),
        "replies": int(comments.get("reply_total") or 0),
        "comment_images": int(comments.get("comment_images") or 0),
        "avatar_urls": int(comments.get("avatar_urls") or 0),
        "shared_avatar_refs": int(comments.get("shared_avatar_refs") or 0),
        "avatar_cache_hits": int(comments.get("avatar_cache_hits") or 0) + int(comments.get("avatar_shared_reused") or 0),
        "avatar_migrated": int(comments.get("avatar_shared_migrated") or 0),
        "download_tasks": int(comments.get("download_tasks") or 0),
        "deduped_downloads": int(comments.get("deduped_downloads") or 0),
    }


def _resource_wait_delta(start: dict[str, Any]) -> dict[str, Any]:
    current = resource_snapshot()
    delta: dict[str, Any] = {}
    for name, value in current.items():
        before = start.get(name) if isinstance(start.get(name), dict) else {}
        delta[name] = {
            "capacity": value.get("capacity"),
            "active": value.get("active"),
            "wait_count": max(int(value.get("wait_count") or 0) - int(before.get("wait_count") or 0), 0),
            "total_wait_ms": round(float(value.get("total_wait_ms") or 0) - float(before.get("total_wait_ms") or 0), 1),
            "max_wait_ms": value.get("max_wait_ms") or 0,
        }
    return delta


def _finalize_refreshed_meta(meta_path: Path, existing_meta: dict[str, Any]) -> dict[str, Any]:
    fresh_meta = _load_json(meta_path)
    checked_at = _now_iso()
    previous_remote_sync = _remote_sync_payload(existing_meta.get("remoteSync"))
    existing_comments = _list_of_dicts(existing_meta.get("comments"))
    existing_instances = _list_of_dicts(existing_meta.get("instances"))
    fresh_instances = _list_of_dicts(fresh_meta.get("instances"))
    existing_attachments = _list_of_dicts(existing_meta.get("attachments"))
    fresh_attachments = _list_of_dicts(fresh_meta.get("attachments"))
    merged_comments, preserved_comment_count = _merge_comments(
        existing_comments,
        fresh_meta.get("comments") if isinstance(fresh_meta.get("comments"), list) else [],
    )
    merged_comments = normalize_threaded_comments(merged_comments)
    merged_comment_total = _count_comment_threads(merged_comments)
    existing_comment_total = _count_comment_threads(existing_comments)
    merged_instances, deleted_instance_count = _merge_instances(
        existing_instances,
        fresh_instances,
        checked_at,
    )
    added_instance_count = _count_added_items(existing_instances, fresh_instances, _instance_key)
    attachments_added = _count_added_items(existing_attachments, fresh_attachments, _attachment_key)
    summary_changed = _summary_signature(existing_meta.get("summary")) != _summary_signature(fresh_meta.get("summary"))
    existing_instance_keys = {
        key
        for key in (_instance_key(item) for item in existing_instances)
        if key
    }
    added_instance_tokens: set[tuple[str, str]] = set()
    for item in fresh_instances:
        key = _instance_key(item)
        if key and key not in existing_instance_keys:
            added_instance_tokens.update(_instance_match_tokens(item))

    added_comments = max(
        merged_comment_total - existing_comment_total,
        0,
    )
    change_labels = _build_change_labels(
        added_comments=added_comments,
        added_instances=added_instance_count,
        deleted_instances=deleted_instance_count,
        attachments_added=attachments_added,
        summary_changed=summary_changed,
    )
    success_message = _build_success_message(change_labels)

    fresh_meta["comments"] = merged_comments
    fresh_meta["commentSchemaVersion"] = COMMENT_SCHEMA_VERSION
    fresh_meta["commentCount"] = max(
        int(fresh_meta.get("commentCount") or 0),
        merged_comment_total,
        int(existing_meta.get("commentCount") or 0),
    )
    fresh_meta["instances"] = merged_instances
    fresh_meta["remoteSync"] = {
        **previous_remote_sync,
        "enabled": True,
        "lastCheckedAt": checked_at,
        "lastSuccessAt": checked_at,
        "lastErrorAt": "",
        "lastStatus": "success",
        "lastMessage": success_message,
        "sourceDeleted": False,
        "sourceDeletedAt": "",
        "consecutiveErrors": 0,
    }
    _write_json(meta_path, fresh_meta)
    return {
        "meta": fresh_meta,
        "added_comments": added_comments,
        "preserved_comments": preserved_comment_count,
        "added_instances": added_instance_count,
        "deleted_instances": deleted_instance_count,
        "attachments_added": attachments_added,
        "summary_changed": summary_changed,
        "change_labels": change_labels,
        "change_summary": "，".join(change_labels),
        "checked_at": checked_at,
        "added_instance_tokens": added_instance_tokens,
    }


def _update_meta_refresh_error(meta_path: Path, message: str, *, source_deleted: bool) -> None:
    payload = _load_json(meta_path)
    if not payload:
        return
    checked_at = _now_iso()
    previous_remote_sync = _remote_sync_payload(payload.get("remoteSync"))
    consecutive_errors = int(previous_remote_sync.get("consecutiveErrors") or 0)
    payload["remoteSync"] = {
        **previous_remote_sync,
        "enabled": True,
        "lastCheckedAt": checked_at,
        "lastStatus": "source_deleted" if source_deleted else "error",
        "lastMessage": message,
        "sourceDeleted": bool(source_deleted),
        "sourceDeletedAt": checked_at if source_deleted else str(previous_remote_sync.get("sourceDeletedAt") or ""),
        "lastErrorAt": "" if source_deleted else checked_at,
        "consecutiveErrors": 0 if source_deleted else consecutive_errors + 1,
    }
    _write_json(meta_path, payload)


def _supports_remote_refresh(item: dict[str, Any]) -> bool:
    source = str(item.get("source") or "").strip().lower()
    origin_url = normalize_source_url(str(item.get("origin_url") or ""))
    if source not in {"cn", "global"}:
        return False
    if not origin_url:
        return False
    return detect_archive_mode(origin_url) == "single_model"


def _refresh_priority(item: dict[str, Any]) -> tuple[int, int, str]:
    remote_sync = item.get("remote_sync") if isinstance(item.get("remote_sync"), dict) else {}
    last_checked_ts = _parse_ts(
        _remote_sync_value(
            remote_sync,
            "last_checked_at",
            "lastCheckedAt",
            "last_success_at",
            "lastSuccessAt",
        )
    )
    collect_ts = int(item.get("collect_ts") or 0)
    return (last_checked_ts, collect_ts, str(item.get("model_dir") or ""))


class RemoteRefreshManager:
    def __init__(
        self,
        store: Optional[JsonStore] = None,
        task_store: Optional[TaskStateStore] = None,
        archive_manager: Any = None,
    ) -> None:
        self.store = store or JsonStore()
        self.task_store = task_store or TaskStateStore()
        self.archive_manager = archive_manager
        self._loop_lock = threading.Lock()
        self._batch_state_lock = threading.Lock()
        self._batch_launch_lock = threading.Lock()
        self._current_items_lock = threading.Lock()
        self._batch_running = False
        self._current_items: dict[str, dict[str, Any]] = {}
        self._thread: Optional[threading.Thread] = None

    def _set_batch_running(self, running: bool) -> None:
        with self._batch_state_lock:
            self._batch_running = bool(running)

    def _is_batch_running(self) -> bool:
        with self._batch_state_lock:
            return bool(self._batch_running)

    def _reset_current_items(self) -> None:
        with self._current_items_lock:
            self._current_items = {}
        self.task_store.patch_remote_refresh_state(current_item={}, current_items=[])

    def _set_current_item(self, model_dir: str, item: dict[str, Any]) -> None:
        key = str(model_dir or item.get("id") or "").strip()
        if not key:
            return
        with self._current_items_lock:
            self._current_items[key] = dict(item)
            current_items = list(self._current_items.values())
        self.task_store.patch_remote_refresh_state(
            current_item=current_items[0] if current_items else {},
            current_items=current_items,
        )

    def _remove_current_item(self, model_dir: str) -> None:
        key = str(model_dir or "").strip()
        with self._current_items_lock:
            if key:
                self._current_items.pop(key, None)
            current_items = list(self._current_items.values())
        self.task_store.patch_remote_refresh_state(
            current_item=current_items[0] if current_items else {},
            current_items=current_items,
        )

    def start(self) -> None:
        self._ensure_state()
        with self._loop_lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run_loop, name="makerhub-remote-refresh", daemon=True)
            self._thread.start()

    def state_payload(self) -> dict:
        return self._ensure_state()

    def notify_config_updated(self) -> dict:
        return self._ensure_state(force_reschedule=True)

    def trigger_manual_refresh(self) -> dict[str, Any]:
        config = self.store.load()
        current_state = self._ensure_state(force_reschedule=bool(config.remote_refresh.enabled))
        if self._is_batch_running():
            message = "源端刷新已在运行中，无需重复手动同步。"
            state = self.task_store.patch_remote_refresh_state(
                status="running",
                running=True,
                last_message=message,
            )
            return {
                "accepted": False,
                "message": message,
                "config": config.remote_refresh.model_dump(),
                "state": state,
            }

        if self._service_busy():
            message = "当前有归档队列或本地整理任务在运行，请稍后再试手动同步。"
            state = self.task_store.patch_remote_refresh_state(
                status="disabled" if not config.remote_refresh.enabled else "idle",
                running=False,
                last_message=message,
                current_item={},
            )
            _append_remote_refresh_log("manual_trigger_rejected", reason="service_busy")
            append_business_log(
                "remote_refresh",
                "manual_trigger_rejected",
                message,
                level="warning",
            )
            return {
                "accepted": False,
                "message": message,
                "config": config.remote_refresh.model_dump(),
                "state": state,
            }

        if not self._start_batch_async(config):
            message = "源端刷新已在运行中，无需重复手动同步。"
            state = self.task_store.patch_remote_refresh_state(
                status="running",
                running=True,
                last_message=message,
            )
            return {
                "accepted": False,
                "message": message,
                "config": config.remote_refresh.model_dump(),
                "state": state,
            }

        message = "已手动触发一轮源端同步，正在启动。"
        state = self.task_store.patch_remote_refresh_state(
            status="running",
            running=True,
            last_message=message,
            current_item={},
        )
        _append_remote_refresh_log("manual_trigger_accepted", enabled=bool(config.remote_refresh.enabled))
        append_business_log(
            "remote_refresh",
            "manual_trigger_accepted",
            message,
            enabled=bool(config.remote_refresh.enabled),
        )
        return {
            "accepted": True,
            "message": message,
            "config": config.remote_refresh.model_dump(),
            "state": state,
        }

    def _start_batch_async(self, config) -> bool:
        with self._batch_launch_lock:
            if self._is_batch_running():
                return False
            self._set_batch_running(True)

            def _worker() -> None:
                try:
                    self._run_batch(config, prelocked=True)
                except Exception as exc:
                    message = f"手动源端刷新异常：{exc}"
                    self.task_store.patch_remote_refresh_state(
                        status="error",
                        running=False,
                        last_error_at=_now_iso(),
                        last_message=_sanitize_remote_refresh_message(message, "手动源端刷新异常。"),
                        current_item={},
                    )
                    _append_remote_refresh_log(
                        "manual_trigger_error",
                        error=_sanitize_remote_refresh_message(exc, exc.__class__.__name__),
                    )
                    append_business_log(
                        "remote_refresh",
                        "manual_trigger_error",
                        _sanitize_remote_refresh_message(message, "手动源端刷新异常。"),
                        level="error",
                    )
                    self._set_batch_running(False)

            thread = threading.Thread(
                target=_worker,
                name="makerhub-remote-refresh-manual",
                daemon=True,
            )
            try:
                thread.start()
            except Exception:
                self._set_batch_running(False)
                raise
            return True

    def _ensure_state(self, force_reschedule: bool = False) -> dict:
        config = self.store.load()
        refresh_config = config.remote_refresh
        normalized_cron = _validate_cron(refresh_config.cron)
        current = self.task_store.load_remote_refresh_state()
        batch_running = self._is_batch_running()
        stale_running = bool(current.get("running")) and not batch_running

        if not refresh_config.enabled:
            if batch_running:
                return self.task_store.patch_remote_refresh_state(
                    status="running",
                    running=True,
                    last_message=str(current.get("last_message") or "源端刷新进行中。"),
                )
            return self.task_store.patch_remote_refresh_state(
                status="disabled",
                running=False,
                next_run_at="",
                last_message="源端刷新已停用。",
                current_item={},
            )

        next_run_at = str(current.get("next_run_at") or "")
        if force_reschedule:
            next_run_at = _next_run_at(normalized_cron)
        elif stale_running or not next_run_at:
            next_run_at = _next_run_at(normalized_cron)

        return self.task_store.patch_remote_refresh_state(
            status="running" if batch_running else "idle",
            running=batch_running,
            next_run_at=next_run_at,
            last_message=(
                "检测到上次源端刷新未正常结束，已恢复为空闲并重新安排下次执行。"
                if stale_running
                else str(current.get("last_message") or "等待下一轮源端刷新。")
            ),
            current_item={} if stale_running else None,
        )

    def _run_loop(self) -> None:
        while True:
            try:
                self._tick()
            except Exception as exc:
                message = f"源端刷新调度器异常：{exc}"
                self.task_store.patch_remote_refresh_state(
                    status="error",
                    running=False,
                    last_error_at=_now_iso(),
                    last_message=_sanitize_remote_refresh_message(message, "源端刷新调度器异常。"),
                    current_item={},
                )
                _append_remote_refresh_log("scheduler_error", error=_sanitize_remote_refresh_message(exc, exc.__class__.__name__))
                append_business_log(
                    "remote_refresh",
                    "scheduler_error",
                    _sanitize_remote_refresh_message(message, "源端刷新调度器异常。"),
                    level="error",
                )
            time.sleep(REMOTE_REFRESH_POLL_SECONDS)

    def _tick(self) -> None:
        if self._is_batch_running():
            return

        config = self.store.load()
        refresh_config = config.remote_refresh
        if not refresh_config.enabled:
            self.task_store.patch_remote_refresh_state(
                status="disabled",
                running=False,
                next_run_at="",
                last_message="源端刷新已停用。",
                current_item={},
            )
            return

        state = self._ensure_state()
        next_run_at = _parse_iso(state.get("next_run_at"))
        if next_run_at and next_run_at > _now():
            return

        if self._service_busy():
            retry_at = (_now()).timestamp() + 60
            self.task_store.patch_remote_refresh_state(
                status="idle",
                running=False,
                next_run_at=china_from_timestamp(retry_at).isoformat(),
                last_message="当前有归档队列或本地整理任务在运行，源端刷新延后 60 秒。",
                current_item={},
            )
            return

        self._run_batch(config)

    def _service_busy(self) -> bool:
        queue = self.task_store.load_archive_queue()
        if queue.get("active") or queue.get("queued"):
            return True
        organize_tasks = self.task_store.load_organize_tasks()
        for item in organize_tasks.get("items") or []:
            if str(item.get("status") or "").strip().lower() in {"pending", "queued", "running"}:
                return True
        return False

    def _pick_candidates(self) -> tuple[list[dict[str, Any]], dict[str, int]]:
        snapshot = get_archive_snapshot()
        models = list(snapshot.get("models") or [])
        deleted_model_dirs = set(self.task_store.load_model_flags().get("deleted") or [])
        eligible: list[dict[str, Any]] = []
        stats = {
            "local_or_invalid": 0,
            "local_deleted": 0,
            "missing_cookie": 0,
            "eligible_total": 0,
            "selected_total": 0,
            "remaining_total": 0,
        }
        config = self.store.load()

        for item in models:
            model_dir = str(item.get("model_dir") or "").strip().strip("/")
            if model_dir and model_dir in deleted_model_dirs:
                stats["local_deleted"] += 1
                continue
            if not _supports_remote_refresh(item):
                stats["local_or_invalid"] += 1
                continue
            if not _select_cookie(str(item.get("origin_url") or ""), config):
                stats["missing_cookie"] += 1
                continue
            eligible.append(item)

        eligible.sort(key=_refresh_priority)
        selected = list(eligible)
        stats["eligible_total"] = len(eligible)
        stats["selected_total"] = len(selected)
        stats["remaining_total"] = max(len(eligible) - len(selected), 0)
        return selected, stats

    def _run_batch(self, config, *, prelocked: bool = False) -> None:
        if not prelocked:
            self._set_batch_running(True)
        refresh_config = config.remote_refresh
        normalized_cron = _validate_cron(refresh_config.cron)
        started_at = _now_iso()
        batch_started_perf = time.perf_counter()
        resource_wait_baseline = resource_snapshot()
        candidates, stats = self._pick_candidates()
        workers = min(_remote_refresh_model_workers(config), max(len(candidates), 1))

        try:
            self._reset_current_items()
            self.task_store.patch_remote_refresh_state(
                status="running",
                running=True,
                last_run_at=started_at,
                last_batch_total=len(candidates),
                last_batch_succeeded=0,
                last_batch_failed=0,
                last_batch_skipped=0,
                last_eligible_total=int(stats.get("eligible_total") or 0),
                last_remaining_total=int(stats.get("remaining_total") or 0),
                last_skipped_missing_cookie=int(stats.get("missing_cookie") or 0),
                last_skipped_local_or_invalid=int(stats.get("local_or_invalid") or 0),
                current_item={},
                current_items=[],
                last_batch_metrics=_empty_batch_metrics(),
                last_resource_waits=_resource_wait_delta(resource_wait_baseline),
                last_slow_models=[],
                last_message=(
                    f"源端刷新开始，本轮计划处理 {len(candidates)} 个模型，并发 {workers}。{_batch_scope_message(eligible_total=int(stats.get('eligible_total') or 0), remaining_total=int(stats.get('remaining_total') or 0))}"
                    if candidates
                    else "当前没有可执行源端刷新的模型。"
                ),
            )
            _append_remote_refresh_log(
                "batch_started",
                selected=len(candidates),
                workers=workers,
                stats=stats,
            )
            append_business_log(
                "remote_refresh",
                "batch_started",
                (
                    f"源端刷新开始，本轮计划处理 {len(candidates)} 个模型，并发 {workers}。"
                    f"{_batch_scope_message(eligible_total=int(stats.get('eligible_total') or 0), remaining_total=int(stats.get('remaining_total') or 0))}"
                ),
                selected=len(candidates),
                workers=workers,
                stats=stats,
            )

            if not candidates:
                no_candidate_message = "没有可刷新的远端模型，等待下一轮。"
                if stats.get("missing_cookie"):
                    no_candidate_message = (
                        f"当前没有可执行源端刷新的模型，另有 {int(stats.get('missing_cookie') or 0)} 个模型因缺少对应站点 Cookie 被跳过。"
                    )
                self.task_store.patch_remote_refresh_state(
                    status="idle",
                    running=False,
                    next_run_at=_next_run_at(normalized_cron),
                    last_success_at=started_at,
                    last_message=no_candidate_message,
                    last_batch_total=0,
                    last_batch_succeeded=0,
                    last_batch_failed=0,
                    last_eligible_total=int(stats.get("eligible_total") or 0),
                    last_remaining_total=0,
                    last_skipped_missing_cookie=int(stats.get("missing_cookie") or 0),
                    last_skipped_local_or_invalid=int(stats.get("local_or_invalid") or 0),
                    current_item={},
                )
                return

            succeeded = 0
            failed = 0
            skipped = 0
            batch_metrics = _empty_batch_metrics()
            slow_models: list[dict[str, Any]] = []

            with _temporary_proxy_env(config):
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="remote-refresh-model") as executor:
                    future_map = {
                        executor.submit(self._refresh_one, item, index=index, total=len(candidates), config=config): item
                        for index, item in enumerate(candidates, start=1)
                    }
                    for future in as_completed(future_map):
                        item = future_map[future]
                        try:
                            result = future.result()
                        except Exception as exc:
                            result = {
                                "ok": False,
                                "error": _sanitize_remote_refresh_message(exc, exc.__class__.__name__),
                                "metrics": {
                                    "title": str(item.get("title") or item.get("model_dir") or ""),
                                    "model_dir": str(item.get("model_dir") or ""),
                                },
                            }
                        if result.get("ok"):
                            succeeded += 1
                            if result.get("skipped"):
                                skipped += 1
                        else:
                            failed += 1
                        item_metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
                        _merge_batch_metrics(batch_metrics, item_metrics)
                        if item_metrics:
                            slow_models.append(item_metrics)
                        processed_total = succeeded + failed
                        remaining_total = max(int(stats.get("eligible_total") or 0) - processed_total, 0)
                        self.task_store.patch_remote_refresh_state(
                            last_batch_succeeded=succeeded,
                            last_batch_failed=failed,
                            last_batch_skipped=skipped,
                            last_remaining_total=remaining_total,
                            last_batch_metrics=batch_metrics,
                            last_resource_waits=_resource_wait_delta(resource_wait_baseline),
                            last_slow_models=_top_slow_models(slow_models),
                            last_message=(
                                f"源端刷新进行中，并发 {workers}：已完成 {processed_total}/{len(candidates)}，"
                                f"成功 {succeeded}，失败 {failed}。"
                            ),
                        )

            finished_at = _now_iso()
            previous_state = self.task_store.load_remote_refresh_state()
            processed_total = succeeded + failed
            remaining_total = max(int(stats.get("eligible_total") or 0) - processed_total, 0)
            message = (
                f"源端刷新完成，成功 {succeeded} 个，失败 {failed} 个。"
                f"{_batch_scope_message(eligible_total=int(stats.get('eligible_total') or 0), remaining_total=remaining_total)}"
            )
            self.task_store.patch_remote_refresh_state(
                status="idle" if failed == 0 else "error",
                running=False,
                current_item={},
                next_run_at=_next_run_at(normalized_cron),
                last_success_at=finished_at if succeeded else str(previous_state.get("last_success_at") or ""),
                last_error_at=finished_at if failed else str(previous_state.get("last_error_at") or ""),
                last_message=message,
                last_batch_succeeded=succeeded,
                last_batch_failed=failed,
                last_batch_skipped=skipped,
                last_eligible_total=int(stats.get("eligible_total") or 0),
                last_remaining_total=remaining_total,
                last_skipped_missing_cookie=int(stats.get("missing_cookie") or 0),
                last_skipped_local_or_invalid=int(stats.get("local_or_invalid") or 0),
                last_batch_metrics={**batch_metrics, "batch_duration_ms": round((time.perf_counter() - batch_started_perf) * 1000, 1)},
                last_resource_waits=_resource_wait_delta(resource_wait_baseline),
                last_slow_models=_top_slow_models(slow_models),
            )
            _append_remote_refresh_log(
                "batch_finished",
                succeeded=succeeded,
                failed=failed,
                skipped=skipped,
                interrupted=False,
                stats=stats,
                metrics={**batch_metrics, "batch_duration_ms": round((time.perf_counter() - batch_started_perf) * 1000, 1)},
                slow_models=_top_slow_models(slow_models),
                resource_waits=_resource_wait_delta(resource_wait_baseline),
                remaining_total=remaining_total,
            )
            append_business_log(
                "remote_refresh",
                "batch_finished",
                message,
                succeeded=succeeded,
                failed=failed,
                skipped=skipped,
                interrupted=False,
                stats=stats,
                metrics={**batch_metrics, "batch_duration_ms": round((time.perf_counter() - batch_started_perf) * 1000, 1)},
                slow_models=_top_slow_models(slow_models),
                resource_waits=_resource_wait_delta(resource_wait_baseline),
                remaining_total=remaining_total,
            )
        finally:
            self._reset_current_items()
            self._set_batch_running(False)

    def _refresh_one(self, item: dict[str, Any], *, index: int, total: int, config) -> dict[str, Any]:
        model_dir = str(item.get("model_dir") or "").strip().strip("/")
        title = str(item.get("title") or model_dir or "未命名模型")
        origin_url = normalize_source_url(str(item.get("origin_url") or ""))
        meta_path = Path(str(item.get("meta_path") or "")).resolve()
        cookie = _select_cookie(origin_url, config)
        current_item = {
            "id": model_dir,
            "title": title,
            "url": origin_url,
            "status": "running",
            "progress": 0,
            "message": f"源端刷新中 {index}/{total}，等待资源",
            "updated_at": _now_iso(),
            "meta": {
                "model_dir": model_dir,
            },
        }
        self._set_current_item(model_dir, current_item)
        _append_remote_refresh_log("model_started", model_dir=model_dir, title=title, url=origin_url, index=index, total=total)

        model_started_perf = time.perf_counter()
        archive_duration_ms = 0.0
        finalize_duration_ms = 0.0
        disk_wait_ms = 0.0
        existing_meta = _load_json(meta_path)
        if not cookie:
            message = "缺少对应站点 Cookie，已跳过源端刷新。"
            self.task_store.append_remote_refresh_history(
                {
                    "id": _history_id(model_dir, "skipped"),
                    "title": title,
                    "url": origin_url,
                    "status": "skipped",
                    "progress": 0,
                    "message": message,
                    "updated_at": _now_iso(),
                    "meta": {
                        "model_dir": model_dir,
                        "checked_at": _now_iso(),
                        "change_labels": ["缺少 Cookie"],
                        "change_summary": "缺少 Cookie",
                    },
                }
            )
            _append_remote_refresh_log("model_skipped", model_dir=model_dir, reason="missing_cookie")
            self._remove_current_item(model_dir)
            return {
                "ok": True,
                "skipped": True,
                "metrics": {
                    "model_dir": model_dir,
                    "title": title,
                    "total_duration_ms": round((time.perf_counter() - model_started_perf) * 1000, 1),
                },
            }

        def progress_callback(payload: dict[str, Any]) -> None:
            self._set_current_item(
                model_dir,
                {
                    "id": model_dir,
                    "title": title,
                    "url": origin_url,
                    "status": "running",
                    "progress": int(payload.get("percent") or 0),
                    "message": _sanitize_remote_refresh_message(
                        payload.get("message") or f"源端刷新中 {index}/{total}",
                        f"源端刷新中 {index}/{total}",
                    ),
                    "updated_at": _now_iso(),
                    "meta": {
                        "model_dir": model_dir,
                    },
                },
            )

        try:
            limit_guard = _read_three_mf_limit_guard()
            daily_limit_active = _is_three_mf_limit_guard_active_for_url(origin_url, limit_guard)
            skip_three_mf_fetch = True
            skip_three_mf_message = (
                _three_mf_limit_message(limit_guard)
                if daily_limit_active
                else "源端刷新仅检测新增 3MF，下载交给新增 3MF 下载队列。"
            )
            archive_started_perf = time.perf_counter()
            archive_result = run_archive_model_job(
                url=origin_url,
                cookie=cookie,
                download_dir=str(ARCHIVE_DIR),
                logs_dir=str(LOGS_DIR),
                existing_root=str(ARCHIVE_DIR),
                progress_callback=progress_callback,
                skip_three_mf_fetch=skip_three_mf_fetch,
                three_mf_skip_message=skip_three_mf_message,
                three_mf_skip_state="download_limited" if daily_limit_active else "pending_download",
                download_assets=False,
                rebuild_archive=False,
                record_missing_3mf_log=False,
            )
            archive_duration_ms = round((time.perf_counter() - archive_started_perf) * 1000, 1)
            limit_guard_state = limit_guard if daily_limit_active else None
            for missing_item in archive_result.get("missing_3mf") or []:
                if str(missing_item.get("downloadState") or "").strip() != "download_limited":
                    continue
                if _is_three_mf_limit_guard_active_for_url(origin_url, limit_guard_state):
                    continue
                limit_guard_state = _activate_three_mf_limit_guard(
                    message=str(missing_item.get("downloadMessage") or ""),
                    model_id=str(archive_result.get("model_id") or existing_meta.get("id") or ""),
                    model_url=origin_url,
                    instance_id=str(
                        missing_item.get("id")
                        or missing_item.get("profileId")
                        or missing_item.get("instanceId")
                        or ""
                    ),
                )
            finalize_started_perf = time.perf_counter()
            with resource_slot("disk_io", detail=model_dir) as waited_ms:
                disk_wait_ms = round(float(waited_ms or 0), 1)
                finalized = _finalize_refreshed_meta(meta_path, existing_meta)
                if not upsert_archive_snapshot_model(model_dir, reason="remote_refresh_completed"):
                    invalidate_archive_snapshot("remote_refresh_completed")
                invalidate_model_detail_cache(model_dir)
                model_id = str(finalized["meta"].get("id") or existing_meta.get("id") or "").strip()
                missing_3mf_items: list[dict[str, Any]] = []
                new_3mf_download_items: list[dict[str, Any]] = []
                new_3mf_download_result: dict[str, Any] = {}
                if model_id:
                    missing_3mf_items = _build_missing_3mf_items(meta_path, finalized["meta"])
                    pending_download_items, remaining_missing_items = _split_new_instance_missing_3mf_items(
                        missing_3mf_items,
                        finalized.get("added_instance_tokens") or set(),
                    )
                    effective_limit_guard = limit_guard_state or _read_three_mf_limit_guard()
                    can_enqueue_new_download = (
                        bool(pending_download_items)
                        and self.archive_manager is not None
                        and not _is_three_mf_limit_guard_active_for_url(origin_url, effective_limit_guard)
                    )
                    if can_enqueue_new_download:
                        new_3mf_download_result = self.archive_manager.submit_three_mf_download(
                            origin_url,
                            model_id=model_id,
                            title=title,
                            instance_ids=[
                                str(item.get("instance_id") or "").strip()
                                for item in pending_download_items
                                if str(item.get("instance_id") or "").strip()
                            ],
                        )
                        if new_3mf_download_result.get("accepted") or new_3mf_download_result.get("queued"):
                            new_3mf_download_items = pending_download_items
                            missing_3mf_items = remaining_missing_items

                    self.task_store.replace_missing_3mf_for_model(
                        model_id,
                        missing_3mf_items,
                    )
            finalize_duration_ms = round((time.perf_counter() - finalize_started_perf) * 1000, 1)
            message = _sanitize_remote_refresh_message(
                finalized["meta"].get("remoteSync", {}).get("lastMessage") or "源端刷新完成。",
                "源端刷新完成。",
            )
            change_labels = list(finalized.get("change_labels") or [])
            if new_3mf_download_items:
                change_labels.append(f"新增 3MF 下载入队 {len(new_3mf_download_items)}")
                message = f"{message.rstrip('。')}，新增 3MF 已加入下载队列。"
            comment_metrics = _archive_comment_metrics(archive_result)
            total_duration_ms = round((time.perf_counter() - model_started_perf) * 1000, 1)
            model_metrics = {
                "model_dir": model_dir,
                "title": title,
                "total_duration_ms": total_duration_ms,
                "archive_duration_ms": archive_duration_ms,
                "finalize_duration_ms": finalize_duration_ms,
                "disk_wait_ms": disk_wait_ms,
                "new_3mf_download_queued": len(new_3mf_download_items),
                **comment_metrics,
            }
            history_item = {
                "id": _history_id(model_dir, "success"),
                "title": title,
                "url": origin_url,
                "status": "success",
                "progress": 100,
                "message": message,
                "updated_at": _now_iso(),
                "meta": {
                    "model_dir": model_dir,
                    "checked_at": finalized.get("checked_at"),
                    "added_comments": finalized.get("added_comments"),
                    "preserved_comments": finalized.get("preserved_comments"),
                    "added_instances": finalized.get("added_instances"),
                    "deleted_instances": finalized.get("deleted_instances"),
                    "attachments_added": finalized.get("attachments_added"),
                    "summary_changed": finalized.get("summary_changed"),
                    "change_labels": change_labels,
                    "change_summary": "，".join(change_labels),
                    "new_3mf_download_queued": len(new_3mf_download_items),
                    "new_3mf_download_task_id": str(new_3mf_download_result.get("task_id") or ""),
                    "metrics": model_metrics,
                },
            }
            self.task_store.append_remote_refresh_history(history_item)
            _append_remote_refresh_log(
                "model_succeeded",
                model_dir=model_dir,
                added_comments=finalized.get("added_comments"),
                added_instances=finalized.get("added_instances"),
                deleted_instances=finalized.get("deleted_instances"),
                attachments_added=finalized.get("attachments_added"),
                summary_changed=finalized.get("summary_changed"),
                change_labels=change_labels,
                new_3mf_download_queued=len(new_3mf_download_items),
                new_3mf_download_task_id=str(new_3mf_download_result.get("task_id") or ""),
                metrics=model_metrics,
            )
            append_business_log(
                "remote_refresh",
                "model_succeeded",
                message,
                model_dir=model_dir,
                url=origin_url,
                added_comments=finalized.get("added_comments"),
                preserved_comments=finalized.get("preserved_comments"),
                added_instances=finalized.get("added_instances"),
                deleted_instances=finalized.get("deleted_instances"),
                attachments_added=finalized.get("attachments_added"),
                summary_changed=finalized.get("summary_changed"),
                change_labels=change_labels,
                new_3mf_download_queued=len(new_3mf_download_items),
                new_3mf_download_task_id=str(new_3mf_download_result.get("task_id") or ""),
                metrics=model_metrics,
            )
            return {"ok": True, "metrics": model_metrics}
        except Exception as exc:
            deleted_on_source = run_source_deleted_check_job(origin_url, cookie)
            if deleted_on_source and meta_path.exists():
                message = "源端模型已删除，本地保留现有归档。"
                with resource_slot("disk_io", detail=model_dir):
                    _update_meta_refresh_error(meta_path, message, source_deleted=True)
                    invalidate_archive_snapshot("remote_refresh_mark_deleted")
                    invalidate_model_detail_cache(model_dir)
                model_metrics = {
                    "model_dir": model_dir,
                    "title": title,
                    "total_duration_ms": round((time.perf_counter() - model_started_perf) * 1000, 1),
                    "archive_duration_ms": archive_duration_ms,
                    "finalize_duration_ms": finalize_duration_ms,
                    "disk_wait_ms": disk_wait_ms,
                }
                history_item = {
                    "id": _history_id(model_dir, "source_deleted"),
                    "title": title,
                    "url": origin_url,
                    "status": "source_deleted",
                    "progress": 100,
                    "message": message,
                    "updated_at": _now_iso(),
                    "meta": {
                        "model_dir": model_dir,
                        "checked_at": _now_iso(),
                        "change_labels": ["模型源端已删除"],
                        "change_summary": "模型源端已删除",
                        "metrics": model_metrics,
                    },
                }
                self.task_store.append_remote_refresh_history(history_item)
                _append_remote_refresh_log("model_deleted_on_source", model_dir=model_dir, url=origin_url)
                append_business_log(
                    "remote_refresh",
                    "model_deleted_on_source",
                    message,
                    model_dir=model_dir,
                    url=origin_url,
                    metrics=model_metrics,
                )
                return {"ok": True, "source_deleted": True, "metrics": model_metrics}

            message = _sanitize_remote_refresh_message(exc, exc.__class__.__name__)
            if meta_path.exists():
                with resource_slot("disk_io", detail=model_dir):
                    _update_meta_refresh_error(meta_path, message, source_deleted=False)
                    invalidate_archive_snapshot("remote_refresh_error")
                    invalidate_model_detail_cache(model_dir)
            model_metrics = {
                "model_dir": model_dir,
                "title": title,
                "total_duration_ms": round((time.perf_counter() - model_started_perf) * 1000, 1),
                "archive_duration_ms": archive_duration_ms,
                "finalize_duration_ms": finalize_duration_ms,
                "disk_wait_ms": disk_wait_ms,
            }
            history_item = {
                "id": _history_id(model_dir, "failed"),
                "title": title,
                "url": origin_url,
                "status": "failed",
                "progress": 0,
                "message": message,
                "updated_at": _now_iso(),
                "meta": {
                    "model_dir": model_dir,
                    "checked_at": _now_iso(),
                    "change_labels": ["刷新失败"],
                    "change_summary": "刷新失败",
                    "metrics": model_metrics,
                },
            }
            self.task_store.append_remote_refresh_history(history_item)
            _append_remote_refresh_log("model_failed", model_dir=model_dir, url=origin_url, error=message)
            append_business_log(
                "remote_refresh",
                "model_failed",
                message,
                level="error",
                model_dir=model_dir,
                url=origin_url,
                metrics=model_metrics,
            )
            return {"ok": False, "error": message, "metrics": model_metrics}
        finally:
            self._remove_current_item(model_dir)
