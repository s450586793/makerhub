import copy
import hashlib
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests
from croniter import CroniterBadCronError, croniter

from app.core.settings import ARCHIVE_DIR, LOGS_DIR
from app.core.store import JsonStore
from app.services.archive_worker import detect_archive_mode
from app.services.batch_discovery import normalize_source_url
from app.services.business_logs import append_business_log
from app.services.catalog import get_archive_snapshot, invalidate_archive_snapshot, invalidate_model_detail_cache
from app.services.legacy_archiver import archive_model as legacy_archive_model
from app.services.task_state import TaskStateStore
from app.services.three_mf import resolve_model_instance_files


REMOTE_REFRESH_LOG_PATH = LOGS_DIR / "remote_refresh.log"
REMOTE_REFRESH_POLL_SECONDS = 20
DEFAULT_REMOTE_REFRESH_CRON = "0 */2 * * *"


def _now() -> datetime:
    return datetime.now()


def _now_iso() -> str:
    return _now().isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


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
    return croniter(normalized, base or _now()).get_next(datetime).isoformat()


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
        return str(cookie_map.get("global") or "").strip()
    return str(cookie_map.get("cn") or "").strip()


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


def _merge_comments(existing_comments: list[Any], fresh_comments: list[Any]) -> tuple[list[dict[str, Any]], int]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    fresh_count = 0

    for raw in fresh_comments or []:
        if not isinstance(raw, dict):
            continue
        item = copy.deepcopy(raw)
        key = _comment_key(item)
        if not key or key in seen:
            continue
        item["id"] = key
        seen.add(key)
        merged.append(item)
        fresh_count += 1

    added_count = 0
    for raw in existing_comments or []:
        if not isinstance(raw, dict):
            continue
        item = copy.deepcopy(raw)
        key = _comment_key(item)
        if not key or key in seen:
            continue
        item["id"] = key
        seen.add(key)
        merged.append(item)
        added_count += 1

    return merged, max(len(merged) - (len(fresh_comments or [])), 0 if fresh_count else added_count)


def _instance_key(instance: Any) -> str:
    if not isinstance(instance, dict):
        return ""
    for field in ("id", "profileId", "instanceId", "fileName", "name", "title"):
        value = str(instance.get(field) or "").strip()
        if value:
            return value
    return ""


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


def _missing_3mf_message_from_instance(instance: dict[str, Any]) -> tuple[str, str]:
    download_state = str(instance.get("downloadState") or "").strip()
    download_message = str(instance.get("downloadMessage") or "").strip()

    if download_state == "download_limited":
        return "missing", download_message or "已达到 MakerWorld 每日下载上限，今日暂停自动重试。"
    if download_state == "auth_required":
        return "missing", download_message or "下载 3MF 需要有效登录 Cookie，请检查 Cookie 是否过期。"
    if download_state == "cloudflare":
        return "missing", download_message or "下载 3MF 时触发了 Cloudflare 校验，请更新 Cookie 或调整代理。"
    if download_state == "not_found":
        return "missing", download_message or "源端没有返回该打印配置的 3MF 下载地址。"
    if download_message:
        return "missing", download_message
    return "missing", "等待重新下载"


def _build_missing_3mf_items(
    meta_path: Path,
    meta: dict[str, Any],
    resolved_files: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    model_root = meta_path.parent
    model_id = str(meta.get("id") or "").strip()
    model_url = normalize_source_url(str(meta.get("url") or ""))
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
        instance_id = str(instance.get("id") or instance.get("profileId") or instance.get("instanceId") or "").strip()
        title = str(instance.get("title") or instance.get("name") or model_title).strip()
        key = (instance_id, title)
        if key in seen:
            continue
        seen.add(key)
        item_status, item_message = _missing_3mf_message_from_instance(instance)
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


def _finalize_refreshed_meta(meta_path: Path, existing_meta: dict[str, Any]) -> dict[str, Any]:
    fresh_meta = _load_json(meta_path)
    checked_at = _now_iso()
    previous_remote_sync = _remote_sync_payload(existing_meta.get("remoteSync"))
    existing_instances = _list_of_dicts(existing_meta.get("instances"))
    fresh_instances = _list_of_dicts(fresh_meta.get("instances"))
    existing_attachments = _list_of_dicts(existing_meta.get("attachments"))
    fresh_attachments = _list_of_dicts(fresh_meta.get("attachments"))
    merged_comments, preserved_comment_count = _merge_comments(
        existing_meta.get("comments") if isinstance(existing_meta.get("comments"), list) else [],
        fresh_meta.get("comments") if isinstance(fresh_meta.get("comments"), list) else [],
    )
    merged_instances, deleted_instance_count = _merge_instances(
        existing_instances,
        fresh_instances,
        checked_at,
    )
    added_instance_count = _count_added_items(existing_instances, fresh_instances, _instance_key)
    attachments_added = _count_added_items(existing_attachments, fresh_attachments, _attachment_key)
    summary_changed = _summary_signature(existing_meta.get("summary")) != _summary_signature(fresh_meta.get("summary"))

    added_comments = max(
        len(merged_comments)
        - len(existing_meta.get("comments") if isinstance(existing_meta.get("comments"), list) else []),
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
    fresh_meta["commentCount"] = max(
        int(fresh_meta.get("commentCount") or 0),
        len(merged_comments),
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


def _source_looks_deleted(url: str, cookie: str) -> bool:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "User-Agent": "Mozilla/5.0 (Makerhub Remote Refresh)",
    }
    if cookie:
        headers["Cookie"] = cookie
    try:
        response = requests.get(
            normalize_source_url(url),
            headers=headers,
            timeout=(6, 12),
            allow_redirects=True,
        )
    except Exception:
        return False
    if response.status_code == 404:
        return True
    final_url = str(response.url or "")
    return "/404" in final_url or "not found" in response.text[:400].lower()


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
    def __init__(self, store: Optional[JsonStore] = None, task_store: Optional[TaskStateStore] = None) -> None:
        self.store = store or JsonStore()
        self.task_store = task_store or TaskStateStore()
        self._loop_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._ensure_state()
        with self._loop_lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run_loop, name="makerhub-remote-refresh", daemon=True)
            self._thread.start()

    def state_payload(self) -> dict:
        return self.task_store.load_remote_refresh_state()

    def notify_config_updated(self) -> dict:
        return self._ensure_state(force_reschedule=True)

    def _ensure_state(self, force_reschedule: bool = False) -> dict:
        config = self.store.load()
        refresh_config = config.remote_refresh
        normalized_cron = _validate_cron(refresh_config.cron)
        current = self.task_store.load_remote_refresh_state()

        if not refresh_config.enabled:
            return self.task_store.patch_remote_refresh_state(
                status="disabled",
                running=False,
                next_run_at="",
                last_message="源端刷新已停用。",
                current_item={},
            )

        next_run_at = str(current.get("next_run_at") or "")
        if force_reschedule:
            next_run_at = _now().isoformat()
        elif not next_run_at:
            base = _now()
            if not str(current.get("last_run_at") or "").strip():
                next_run_at = base.isoformat()
            else:
                next_run_at = _next_run_at(normalized_cron, base)

        return self.task_store.patch_remote_refresh_state(
            status="running" if current.get("running") else "idle",
            next_run_at=next_run_at,
            last_message=str(current.get("last_message") or "等待下一轮源端刷新。"),
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
                next_run_at=datetime.fromtimestamp(retry_at).isoformat(),
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

    def _run_batch(self, config) -> None:
        refresh_config = config.remote_refresh
        normalized_cron = _validate_cron(refresh_config.cron)
        started_at = _now_iso()
        candidates, stats = self._pick_candidates()

        self.task_store.patch_remote_refresh_state(
            status="running",
            running=True,
            last_run_at=started_at,
            last_batch_total=len(candidates),
            last_batch_succeeded=0,
            last_batch_failed=0,
            last_eligible_total=int(stats.get("eligible_total") or 0),
            last_remaining_total=int(stats.get("remaining_total") or 0),
            last_skipped_missing_cookie=int(stats.get("missing_cookie") or 0),
            last_skipped_local_or_invalid=int(stats.get("local_or_invalid") or 0),
            current_item={},
            last_message=(
                f"源端刷新开始，本轮计划处理 {len(candidates)} 个模型。{_batch_scope_message(eligible_total=int(stats.get('eligible_total') or 0), remaining_total=int(stats.get('remaining_total') or 0))}"
                if candidates
                else "当前没有可执行源端刷新的模型。"
            ),
        )
        _append_remote_refresh_log(
            "batch_started",
            selected=len(candidates),
            stats=stats,
        )
        append_business_log(
            "remote_refresh",
            "batch_started",
            (
                f"源端刷新开始，本轮计划处理 {len(candidates)} 个模型。"
                f"{_batch_scope_message(eligible_total=int(stats.get('eligible_total') or 0), remaining_total=int(stats.get('remaining_total') or 0))}"
            ),
            selected=len(candidates),
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

        for index, item in enumerate(candidates, start=1):
            result = self._refresh_one(item, index=index, total=len(candidates), config=config)
            if result.get("ok"):
                succeeded += 1
            else:
                failed += 1
            processed_total = succeeded + failed
            self.task_store.patch_remote_refresh_state(
                last_batch_succeeded=succeeded,
                last_batch_failed=failed,
                last_remaining_total=max(int(stats.get("eligible_total") or 0) - processed_total, 0),
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
            last_eligible_total=int(stats.get("eligible_total") or 0),
            last_remaining_total=remaining_total,
            last_skipped_missing_cookie=int(stats.get("missing_cookie") or 0),
            last_skipped_local_or_invalid=int(stats.get("local_or_invalid") or 0),
        )
        _append_remote_refresh_log(
            "batch_finished",
            succeeded=succeeded,
            failed=failed,
            interrupted=False,
            stats=stats,
            remaining_total=remaining_total,
        )
        append_business_log(
            "remote_refresh",
            "batch_finished",
            message,
            succeeded=succeeded,
            failed=failed,
            interrupted=False,
            stats=stats,
            remaining_total=remaining_total,
        )

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
            "message": f"源端刷新中 {index}/{total}",
            "updated_at": _now_iso(),
            "meta": {
                "model_dir": model_dir,
            },
        }
        self.task_store.patch_remote_refresh_state(current_item=current_item)
        _append_remote_refresh_log("model_started", model_dir=model_dir, title=title, url=origin_url, index=index, total=total)

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
            return {"ok": True, "skipped": True}

        def progress_callback(payload: dict[str, Any]) -> None:
            self.task_store.patch_remote_refresh_state(
                current_item={
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
                }
            )

        try:
            with _temporary_proxy_env(config):
                legacy_archive_model(
                    url=origin_url,
                    cookie=cookie,
                    download_dir=ARCHIVE_DIR,
                    logs_dir=LOGS_DIR,
                    existing_root=ARCHIVE_DIR,
                    progress_callback=progress_callback,
                )
            finalized = _finalize_refreshed_meta(meta_path, existing_meta)
            invalidate_archive_snapshot("remote_refresh_completed")
            invalidate_model_detail_cache(model_dir)
            model_id = str(finalized["meta"].get("id") or existing_meta.get("id") or "").strip()
            if model_id:
                self.task_store.replace_missing_3mf_for_model(
                    model_id,
                    _build_missing_3mf_items(meta_path, finalized["meta"]),
                )
            message = _sanitize_remote_refresh_message(
                finalized["meta"].get("remoteSync", {}).get("lastMessage") or "源端刷新完成。",
                "源端刷新完成。",
            )
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
                    "change_labels": finalized.get("change_labels"),
                    "change_summary": finalized.get("change_summary"),
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
                change_labels=finalized.get("change_labels"),
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
                change_labels=finalized.get("change_labels"),
            )
            return {"ok": True}
        except Exception as exc:
            with _temporary_proxy_env(config):
                deleted_on_source = _source_looks_deleted(origin_url, cookie)
            if deleted_on_source and meta_path.exists():
                message = "源端模型已删除，本地保留现有归档。"
                _update_meta_refresh_error(meta_path, message, source_deleted=True)
                invalidate_archive_snapshot("remote_refresh_mark_deleted")
                invalidate_model_detail_cache(model_dir)
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
                )
                return {"ok": True, "source_deleted": True}

            message = _sanitize_remote_refresh_message(exc, exc.__class__.__name__)
            if meta_path.exists():
                _update_meta_refresh_error(meta_path, message, source_deleted=False)
                invalidate_archive_snapshot("remote_refresh_error")
                invalidate_model_detail_cache(model_dir)
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
            )
            return {"ok": False, "error": message}
