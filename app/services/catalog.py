import json
import mimetypes
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from bs4 import BeautifulSoup

from app.core.settings import ARCHIVE_DIR, CONFIG_PATH, STATE_DIR
from app.core.store import JsonStore
from app.core.timezone import from_timestamp as china_from_timestamp, parse_timestamp as china_parse_timestamp
from app.services.batch_discovery import extract_model_id, normalize_source_url
from app.services.model_attachments import (
    ATTACHMENT_CATEGORY_LABELS,
    MANUAL_ATTACHMENTS_RELATIVE_DIR,
    MANUAL_ATTACHMENTS_SIDECAR,
    load_manual_attachments,
)
from app.services.source_health import build_source_health_cards
from app.services.task_state import SUBSCRIPTIONS_STATE_PATH, TaskStateStore
from app.services.three_mf import describe_three_mf_failure, normalize_makerworld_source, resolve_model_instance_files


SOURCE_LABELS = {
    "cn": "MakerWorld 国内",
    "global": "MakerWorld 国际",
    "local": "本地模型",
}

LEGACY_CURL_FAILURE_MARKER = "No such file or directory: 'curl'"
DETAIL_COMMENTS_PAGE_SIZE = 20
_ARCHIVE_SNAPSHOT_LOCK = threading.RLock()
_ARCHIVE_SNAPSHOT_MARKER_PATH = STATE_DIR / "archive_snapshot.marker"
_ARCHIVE_SNAPSHOT_CACHE: dict[str, Any] = {
    "snapshot": None,
    "dirty": True,
    "dirty_reason": "startup",
    "built_at": 0.0,
    "marker_token": "",
}
_MODEL_DETAIL_CACHE_LOCK = threading.RLock()
_MODEL_DETAIL_CACHE: dict[str, dict[str, Any]] = {}
_SUBSCRIPTION_FLAGS_INDEX_LOCK = threading.RLock()
_SUBSCRIPTION_FLAGS_INDEX_CACHE: dict[str, Any] = {
    "signature": None,
    "deleted_by_key": {},
}


def _read_archive_snapshot_marker() -> str:
    try:
        return _ARCHIVE_SNAPSHOT_MARKER_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_archive_snapshot_marker(reason: str = "") -> str:
    token = f"{time.time_ns()}:{str(reason or '').strip()}"
    try:
        _ARCHIVE_SNAPSHOT_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
        _ARCHIVE_SNAPSHOT_MARKER_PATH.write_text(token, encoding="utf-8")
    except OSError:
        return ""
    return token


def invalidate_archive_snapshot(reason: str = "") -> None:
    _write_archive_snapshot_marker(reason)
    with _ARCHIVE_SNAPSHOT_LOCK:
        _ARCHIVE_SNAPSHOT_CACHE["dirty"] = True
        if reason:
            _ARCHIVE_SNAPSHOT_CACHE["dirty_reason"] = reason


def invalidate_model_detail_cache(model_dir: str = "") -> None:
    clean_value = str(model_dir or "").strip().strip("/")
    with _MODEL_DETAIL_CACHE_LOCK:
        if not clean_value:
            _MODEL_DETAIL_CACHE.clear()
            return
        _MODEL_DETAIL_CACHE.pop(clean_value, None)


def _model_detail_signature(model_root: Path, meta_path: Path) -> tuple[int, int, int]:
    meta_mtime = meta_path.stat().st_mtime_ns if meta_path.exists() else 0
    sidecar_path = model_root / MANUAL_ATTACHMENTS_SIDECAR
    manual_dir = model_root / MANUAL_ATTACHMENTS_RELATIVE_DIR
    sidecar_mtime = sidecar_path.stat().st_mtime_ns if sidecar_path.exists() else 0
    manual_dir_mtime = manual_dir.stat().st_mtime_ns if manual_dir.exists() else 0
    return (meta_mtime, sidecar_mtime, manual_dir_mtime)


def _clone_model_items(items: list[dict]) -> list[dict]:
    return [item.copy() for item in items]


def _file_signature(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
    except OSError:
        return (0, 0)
    return (int(stat.st_mtime_ns), int(stat.st_size))


def _compose_archive_snapshot(models: list[dict]) -> dict[str, Any]:
    ordered_models = sorted(models, key=lambda item: str(item.get("model_dir") or ""))
    archived_keys: set[str] = set()
    archived_model_ids: set[str] = set()
    archived_urls: set[str] = set()

    for model in ordered_models:
        model_id = str(model.get("id") or "").strip()
        if model_id:
            archived_model_ids.add(model_id)
            archived_keys.add(f"model:{model_id}")

        origin_url = normalize_source_url(str(model.get("origin_url") or "").strip())
        if origin_url:
            archived_urls.add(origin_url)
            archived_keys.add(origin_url)

    return {
        "models": tuple(ordered_models),
        "collect_sorted": tuple(_sort_models(ordered_models, "collectDate")),
        "tags": tuple(_tags_from_items(ordered_models)),
        "source_counts": _source_counts_from_items(ordered_models),
        "archived_keys": frozenset(archived_keys),
        "archived_model_ids": frozenset(archived_model_ids),
        "archived_urls": frozenset(archived_urls),
        "total": len(ordered_models),
    }


def _build_archive_snapshot() -> dict[str, Any]:
    models: list[dict] = []

    for meta_path in sorted(ARCHIVE_DIR.rglob("meta.json")):
        if not meta_path.is_file():
            continue
        model = _normalize_model(meta_path, include_detail=False)
        if not model:
            continue

        models.append(model)
    return _compose_archive_snapshot(models)


def get_archive_snapshot(force: bool = False) -> dict[str, Any]:
    marker_token = _read_archive_snapshot_marker()
    with _ARCHIVE_SNAPSHOT_LOCK:
        snapshot = _ARCHIVE_SNAPSHOT_CACHE.get("snapshot")
        cached_marker_token = str(_ARCHIVE_SNAPSHOT_CACHE.get("marker_token") or "")
        marker_changed = marker_token != cached_marker_token
        if force or snapshot is None or _ARCHIVE_SNAPSHOT_CACHE.get("dirty", False) or marker_changed:
            snapshot = _build_archive_snapshot()
            _ARCHIVE_SNAPSHOT_CACHE["snapshot"] = snapshot
            _ARCHIVE_SNAPSHOT_CACHE["dirty"] = False
            _ARCHIVE_SNAPSHOT_CACHE["built_at"] = time.time()
            _ARCHIVE_SNAPSHOT_CACHE["marker_token"] = marker_token
        return snapshot


def upsert_archive_snapshot_model(model_dir: str, reason: str = "") -> bool:
    clean_model_dir = str(model_dir or "").strip().strip("/")
    if not clean_model_dir:
        return False

    archive_root = ARCHIVE_DIR.resolve()
    target = (archive_root / clean_model_dir).resolve()
    try:
        relative_dir = target.relative_to(archive_root)
    except ValueError:
        return False

    meta_path = target / "meta.json"
    if not meta_path.is_file():
        return False

    model = _normalize_model(meta_path, include_detail=False)
    if not model:
        return False

    marker_token = _read_archive_snapshot_marker()
    normalized_model_dir = relative_dir.as_posix()
    with _ARCHIVE_SNAPSHOT_LOCK:
        snapshot = _ARCHIVE_SNAPSHOT_CACHE.get("snapshot")
        cached_marker_token = str(_ARCHIVE_SNAPSHOT_CACHE.get("marker_token") or "")
        if snapshot is None or _ARCHIVE_SNAPSHOT_CACHE.get("dirty", False) or marker_token != cached_marker_token:
            return False

        models = list(snapshot.get("models") or ())
        for index, item in enumerate(models):
            if str(item.get("model_dir") or "").strip().strip("/") == normalized_model_dir:
                models[index] = model
                break
        else:
            models.append(model)

        _ARCHIVE_SNAPSHOT_CACHE["snapshot"] = _compose_archive_snapshot(models)
        _ARCHIVE_SNAPSHOT_CACHE["dirty"] = False
        _ARCHIVE_SNAPSHOT_CACHE["dirty_reason"] = ""
        _ARCHIVE_SNAPSHOT_CACHE["built_at"] = time.time()
        _ARCHIVE_SNAPSHOT_CACHE["marker_token"] = (
            _write_archive_snapshot_marker(reason) or cached_marker_token
        )
    return True


def _safe_int(value: Any) -> int:
    try:
        if value in ("", None):
            return 0
        if isinstance(value, str) and "." in value:
            return int(float(value))
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_timestamp(value: Any) -> int:
    return china_parse_timestamp(value)


def _format_date(value: Any) -> str:
    ts = _parse_timestamp(value)
    if not ts:
        return ""
    return china_from_timestamp(ts).strftime("%Y-%m-%d")


def _format_datetime(value: Any) -> str:
    ts = _parse_timestamp(value)
    if not ts:
        return ""
    return china_from_timestamp(ts).strftime("%Y-%m-%d %H:%M")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _archive_url(relative_path: Path) -> str:
    return f"/archive/{quote(relative_path.as_posix(), safe='/')}"


def _iter_local_candidates(model_root: Path, ref: str) -> list[Path]:
    clean = ref.split("#", 1)[0].split("?", 1)[0].strip().lstrip("/")
    if not clean:
        return []

    primary = model_root / clean
    candidates = [primary]
    if "/" not in clean:
        name = Path(clean).name
        candidates.extend(
            [
                model_root / "images" / name,
                model_root / "instances" / name,
                model_root / "attachments" / name,
            ]
        )
    return candidates


def _local_asset_url(model_root: Path, ref: str) -> Optional[str]:
    if not ref or ref.startswith(("http://", "https://", "data:", "//")):
        return None

    candidates = _iter_local_candidates(model_root, ref)
    if not candidates:
        return None

    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return _archive_url(candidate.relative_to(ARCHIVE_DIR))

    try:
        return _archive_url(candidates[0].relative_to(ARCHIVE_DIR))
    except ValueError:
        return None


def _existing_local_asset_url(model_root: Path, ref: str) -> Optional[str]:
    if not ref or ref.startswith(("http://", "https://", "data:", "//")):
        return None

    candidates = _iter_local_candidates(model_root, ref)
    if not candidates:
        return None

    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            try:
                return _archive_url(candidate.relative_to(ARCHIVE_DIR))
            except ValueError:
                return None
    return None


def _remote_asset_url(*values: Any) -> Optional[str]:
    for value in values:
        if isinstance(value, str) and value.strip():
            raw = value.strip()
            if raw.startswith("//"):
                return f"https:{raw}"
            if raw.startswith(("http://", "https://", "data:")):
                return raw
    return None


def _failure_identity(item: dict) -> tuple[str, str]:
    raw_url = str(item.get("url") or "").strip()
    raw_title = str(item.get("title") or "").strip()
    normalized_url = normalize_source_url(raw_url) if raw_url.startswith(("http://", "https://", "/")) else ""
    model_id = extract_model_id(raw_url) or extract_model_id(raw_title)
    return model_id, normalized_url


def _prune_recent_failures(
    archive_queue: dict,
    archive_snapshot: dict[str, Any],
    missing_items: list[dict],
) -> dict:
    recent_failures = list(archive_queue.get("recent_failures") or [])
    if not recent_failures:
        return archive_queue

    archived_model_ids = set(archive_snapshot.get("archived_model_ids") or [])
    archived_urls = set(archive_snapshot.get("archived_urls") or [])
    pending_missing_ids = {
        str(item.get("model_id") or "").strip()
        for item in missing_items
        if str(item.get("model_id") or "").strip()
    }

    kept: list[dict] = []
    changed = False
    for item in recent_failures:
        model_id, normalized_url = _failure_identity(item)
        message = str(item.get("message") or "")
        archived_match = (
            (model_id and model_id in archived_model_ids)
            or (normalized_url and normalized_url in archived_urls)
        )
        legacy_curl_failure = LEGACY_CURL_FAILURE_MARKER in message

        if archived_match and (not model_id or model_id not in pending_missing_ids):
            changed = True
            continue
        if legacy_curl_failure and (not model_id or model_id not in pending_missing_ids):
            changed = True
            continue
        kept.append(item)

    if not changed:
        return archive_queue

    payload = dict(archive_queue)
    payload["recent_failures"] = kept
    payload["running_count"] = len(payload.get("active") or [])
    payload["queued_count"] = len(payload.get("queued") or [])
    payload["failed_count"] = len(kept)
    return payload


def _count_active_organize_tasks(organize_tasks: dict) -> int:
    active_statuses = {"pending", "queued", "running"}
    count = 0
    for item in organize_tasks.get("items") or []:
        status = str(item.get("status") or "").strip().lower()
        if status in active_statuses:
            count += 1
    return count


def _subscription_deleted_count(state: dict) -> int:
    current_items = state.get("current_items") if isinstance(state.get("current_items"), list) else []
    tracked_items = state.get("tracked_items") if isinstance(state.get("tracked_items"), list) else []
    current_keys = {
        str(item.get("task_key") or item.get("url") or item.get("model_id") or "").strip()
        for item in current_items
        if isinstance(item, dict)
    }
    tracked_keys = {
        str(item.get("task_key") or item.get("url") or item.get("model_id") or "").strip()
        for item in tracked_items
        if isinstance(item, dict)
    }
    current_keys.discard("")
    tracked_keys.discard("")
    return max(len(tracked_keys - current_keys), 0)


def _build_dashboard_subscriptions(
    config,
    task_store: TaskStateStore,
    state_payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    records = list(getattr(config, "subscriptions", []) or [])
    state_payload = state_payload if isinstance(state_payload, dict) else task_store.load_subscriptions_state()
    state_items = state_payload.get("items") or []
    state_map = {
        str(item.get("id") or "").strip(): item
        for item in state_items
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }

    merged_items: list[dict[str, Any]] = []
    for record in records:
        record_id = str(getattr(record, "id", "") or "").strip()
        if not record_id:
            continue
        state = state_map.get(record_id, {})
        merged_items.append(
            {
                "id": record_id,
                "name": str(getattr(record, "name", "") or "").strip() or str(getattr(record, "url", "") or "").strip(),
                "url": str(getattr(record, "url", "") or "").strip(),
                "mode": str(getattr(record, "mode", "") or "").strip(),
                "enabled": bool(getattr(record, "enabled", False)),
                "running": bool(state.get("running", False)),
                "status": str(state.get("status") or "idle"),
                "next_run_at": str(state.get("next_run_at") or ""),
                "last_run_at": str(state.get("last_run_at") or ""),
                "last_success_at": str(state.get("last_success_at") or ""),
                "last_error_at": str(state.get("last_error_at") or ""),
                "last_message": str(state.get("last_message") or ""),
                "last_discovered_count": int(state.get("last_discovered_count") or 0),
                "last_new_count": int(state.get("last_new_count") or 0),
                "last_enqueued_count": int(state.get("last_enqueued_count") or 0),
                "last_deleted_count": int(state.get("last_deleted_count") or 0),
                "current_count": len(state.get("current_items") or []),
                "tracked_count": len(state.get("tracked_items") or []),
                "deleted_count": _subscription_deleted_count(state),
            }
        )

    running_items = [item for item in merged_items if item.get("running")]
    enabled_items = [item for item in merged_items if item.get("enabled")]
    last_results = sorted(
        [item for item in merged_items if str(item.get("last_run_at") or "").strip()],
        key=lambda item: str(item.get("last_run_at") or ""),
        reverse=True,
    )
    next_runs = sorted(
        [
            item for item in merged_items
            if item.get("enabled") and str(item.get("next_run_at") or "").strip()
        ],
        key=lambda item: str(item.get("next_run_at") or ""),
    )

    return {
        "count": len(merged_items),
        "enabled_count": len(enabled_items),
        "running_count": len(running_items),
        "deleted_marked_count": sum(int(item.get("deleted_count") or 0) for item in merged_items),
        "recent_items": last_results[:3],
        "next_items": next_runs[:3],
    }


def _asset_url_from_item(model_root: Path, item: Any) -> Optional[str]:
    if isinstance(item, str):
        return _local_asset_url(model_root, item) or _remote_asset_url(item)

    if not isinstance(item, dict):
        return None

    local_keys = [
        "relPath",
        "localName",
        "fileName",
        "path",
        "avatarRelPath",
        "avatarLocal",
        "avatar",
    ]
    for key in local_keys:
        candidate = item.get(key)
        if isinstance(candidate, str):
            local = _local_asset_url(model_root, candidate)
            if local:
                return local

    return _remote_asset_url(
        item.get("url"),
        item.get("originalUrl"),
        item.get("imageUrl"),
        item.get("coverUrl"),
        item.get("thumbnail"),
        item.get("avatarUrl"),
        item.get("previewImage"),
    )


def _pick_image_url(model_root: Path, *items: Any) -> Optional[str]:
    for item in items:
        url = _asset_url_from_item(model_root, item)
        if url:
            return url
    return None


def _pick_remote_url(*items: Any) -> Optional[str]:
    for item in items:
        if isinstance(item, dict):
            remote = _remote_asset_url(
                item.get("url"),
                item.get("originalUrl"),
                item.get("imageUrl"),
                item.get("coverUrl"),
                item.get("thumbnail"),
                item.get("avatarUrl"),
                item.get("previewImage"),
            )
            if remote:
                return remote
        else:
            remote = _remote_asset_url(item)
            if remote:
                return remote
    return None


def _extract_tags(meta: dict) -> list[str]:
    result: list[str] = []
    for raw_list in (meta.get("tags") or [], meta.get("tagsOriginal") or []):
        if not isinstance(raw_list, list):
            continue
        for item in raw_list:
            if isinstance(item, dict):
                value = str(item.get("name") or item.get("label") or "").strip()
            else:
                value = str(item).strip()
            if value and value not in result:
                result.append(value)
    return result


def _rewrite_summary_html(model_root: Path, html: str) -> str:
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(src=True):
        raw = str(tag.get("src") or "").strip()
        local = _local_asset_url(model_root, raw)
        if local:
            tag["src"] = local

    for tag in soup.find_all(href=True):
        raw = str(tag.get("href") or "").strip()
        if raw.startswith(("http://", "https://", "mailto:", "tel:", "#", "javascript:")):
            continue
        local = _local_asset_url(model_root, raw)
        if local:
            tag["href"] = local

    return str(soup)


def _normalize_stats(meta: dict) -> dict:
    stats = meta.get("stats") or meta.get("counts") or {}
    return {
        "likes": _safe_int(stats.get("likes") or stats.get("like")),
        "favorites": _safe_int(stats.get("favorites") or stats.get("favorite")),
        "downloads": _safe_int(stats.get("downloads") or stats.get("download")),
        "prints": _safe_int(stats.get("prints") or stats.get("print")),
        "views": _safe_int(stats.get("views") or stats.get("read") or stats.get("reads")),
        "comments": _safe_int(stats.get("comments") or meta.get("commentCount")),
    }


def _normalize_remote_sync(meta: dict) -> dict:
    remote_sync = meta.get("remoteSync") if isinstance(meta.get("remoteSync"), dict) else {}
    return {
        "enabled": bool(remote_sync.get("enabled", False)),
        "last_checked_at": str(remote_sync.get("lastCheckedAt") or ""),
        "last_success_at": str(remote_sync.get("lastSuccessAt") or ""),
        "last_error_at": str(remote_sync.get("lastErrorAt") or ""),
        "last_status": str(remote_sync.get("lastStatus") or ""),
        "last_message": str(remote_sync.get("lastMessage") or ""),
        "source_deleted": bool(remote_sync.get("sourceDeleted", False)),
        "source_deleted_at": str(remote_sync.get("sourceDeletedAt") or ""),
        "consecutive_errors": _safe_int(remote_sync.get("consecutiveErrors") or 0),
    }


def _format_duration(value: Any) -> str:
    if value in ("", None):
        return ""
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return ""
        if any(char.isalpha() for char in raw):
            return raw
        value = _safe_int(raw)

    seconds = _safe_int(value)
    if not seconds:
        return ""
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 60:.1f} min"


def _normalize_instance_overview(item: dict) -> dict:
    duration = (
        item.get("time")
        or item.get("timeText")
        or item.get("durationText")
        or _format_duration(item.get("printTimeSeconds") or item.get("duration"))
    )
    plate_items = item.get("plates") if isinstance(item.get("plates"), list) else []
    plates = _safe_int(item.get("plateCount") or item.get("plateNum")) or len(plate_items)
    rating_value = item.get("rating") or item.get("score") or item.get("stars")
    rating = str(rating_value).strip() if rating_value not in ("", None) else ""
    return {
        "title": str(
            item.get("name")
            or item.get("title")
            or item.get("profileName")
            or item.get("fileName")
            or "未命名打印配置"
        ),
        "machine": str(
            item.get("machine")
            or item.get("machineName")
            or item.get("printerModel")
            or item.get("printer")
            or item.get("device")
            or "通用"
        ),
        "time": str(duration),
        "plates": plates,
        "rating": rating,
        "download_count": _safe_int(item.get("downloadCount")),
        "print_count": _safe_int(item.get("printCount")),
        "summary": str(item.get("summary") or item.get("summaryTranslated") or ""),
    }


def _instance_overview_score(item: dict) -> int:
    return (
        (4 if str(item.get("time") or "").strip() else 0)
        + (3 if _safe_int(item.get("plates")) > 0 else 0)
        + (3 if str(item.get("rating") or "").strip() else 0)
        + (1 if _safe_int(item.get("download_count")) > 0 else 0)
        + (1 if _safe_int(item.get("print_count")) > 0 else 0)
    )


def _normalize_model_profile_summary(meta: dict) -> dict:
    overviews: list[dict] = []
    for item in meta.get("instances") or []:
        if not isinstance(item, dict):
            continue
        overviews.append(_normalize_instance_overview(item))

    if not overviews:
        return {
            "title": "",
            "machine": "",
            "time": "",
            "plates": 0,
            "rating": "",
            "download_count": 0,
            "print_count": 0,
            "profile_count": 0,
        }

    selected = overviews[0]
    selected_score = _instance_overview_score(selected)
    for item in overviews[1:]:
        score = _instance_overview_score(item)
        if score > selected_score:
            selected = item
            selected_score = score

    return {
        **selected,
        "profile_count": len(overviews),
    }


def _normalize_instances(meta: dict, model_root: Path) -> list[dict]:
    normalized = []
    resolved_files = resolve_model_instance_files(meta, model_root)
    resolved_matches = resolved_files.get("matches") if isinstance(resolved_files, dict) else {}
    model_source = normalize_makerworld_source(meta.get("source"), meta.get("url"))
    for index, item in enumerate(meta.get("instances") or []):
        if not isinstance(item, dict):
            continue

        instance_key = str(item.get("id") or item.get("profileId") or len(normalized) + 1)
        overview = _normalize_instance_overview(item)
        plate_items = item.get("plates") if isinstance(item.get("plates"), list) else []
        publish_value = (
            item.get("publishTime")
            or item.get("publishedAt")
            or item.get("createTime")
            or item.get("createdAt")
            or ""
        )

        media_items = []
        for picture_index, picture in enumerate(item.get("pictures") or [], start=1):
            if not isinstance(picture, dict):
                continue
            picture_url = _asset_url_from_item(model_root, picture)
            if not picture_url:
                continue
            media_items.append(
                {
                    "label": f"图{picture.get('index') or picture_index}",
                    "kind": "picture",
                    "url": picture_url,
                    "fallback_url": _pick_remote_url(picture) or "",
                }
            )

        for plate_index, plate in enumerate(plate_items, start=1):
            if not isinstance(plate, dict):
                continue
            plate_url = _pick_image_url(
                model_root,
                {
                    "relPath": plate.get("thumbnailRelPath"),
                    "localName": plate.get("thumbnailFile"),
                    "url": plate.get("thumbnailUrl"),
                },
            )
            if not plate_url:
                continue
            media_items.append(
                {
                    "label": f"P{plate.get('index') or plate_index}",
                    "kind": "plate",
                    "url": plate_url,
                    "fallback_url": _pick_remote_url(
                        {
                            "url": plate.get("thumbnailUrl"),
                            "originalUrl": plate.get("thumbnailUrl"),
                        }
                    )
                    or "",
                }
            )

        thumbnail_url = _pick_image_url(
            model_root,
            (item.get("pictures") or [None])[0],
            {
                "relPath": item.get("thumbnailLocal"),
                "url": item.get("thumbnailUrl"),
                "originalUrl": item.get("thumbnail"),
            },
            item.get("cover"),
            item.get("previewImage"),
            item.get("thumbnail"),
        )
        thumbnail_fallback_url = (
            _pick_remote_url((item.get("pictures") or [None])[0])
            or _pick_remote_url(
                {
                    "url": item.get("thumbnailUrl"),
                    "originalUrl": item.get("thumbnail"),
                    "coverUrl": item.get("cover"),
                    "previewImage": item.get("previewImage"),
                }
            )
            or ""
        )
        primary_media = media_items[0] if media_items else None
        resolved_match = resolved_matches.get(index) if isinstance(resolved_matches, dict) else None
        resolved_path = resolved_match.get("path") if isinstance(resolved_match, dict) else None
        file_name = (
            resolved_path.name
            if isinstance(resolved_path, Path)
            else Path(str(item.get("fileName") or "")).name
        )
        file_ref = f"instances/{file_name}" if file_name else ""
        file_url = _existing_local_asset_url(model_root, file_ref) if file_ref else None
        if file_url:
            file_status_message = "3MF 已获取完成，可直接下载。"
        elif item.get("downloadState") or item.get("downloadMessage"):
            file_status_message = describe_three_mf_failure(
                item.get("downloadState"),
                item.get("downloadMessage"),
                source=model_source,
                url=meta.get("url"),
            )
        elif file_name:
            file_status_message = "3MF 还未获取到，可能仍在归档中，或需要到任务页执行缺失 3MF 重新下载。"
        else:
            file_status_message = "当前打印配置没有可用的 3MF 文件。"

        normalized.append(
            {
                "instance_key": instance_key,
                "title": overview["title"],
                "machine": overview["machine"],
                "time": overview["time"],
                "plates": overview["plates"],
                "rating": overview["rating"],
                "publish_date": _format_date(publish_value),
                "download_count": overview["download_count"],
                "print_count": overview["print_count"],
                "summary": overview["summary"],
                "thumbnail_url": thumbnail_url,
                "thumbnail_fallback_url": thumbnail_fallback_url,
                "primary_image_url": (primary_media or {}).get("url") or thumbnail_url,
                "primary_image_fallback_url": (primary_media or {}).get("fallback_url") or thumbnail_fallback_url,
                "media": media_items,
                "file_url": file_url,
                "file_name": file_name or str(item.get("name") or ""),
                "file_available": bool(file_url),
                "file_status_message": file_status_message,
                "source_deleted": bool(item.get("sourceDeleted", False)),
                "source_deleted_at": _format_datetime(item.get("sourceDeletedAt")),
                "source_deleted_message": str(item.get("sourceDeletedMessage") or ""),
            }
        )
    return normalized


def _extract_publish_value(meta: dict) -> Any:
    direct_candidates = [
        meta.get("publishTime"),
        meta.get("publishedAt"),
        meta.get("createTime"),
        meta.get("createdAt"),
        meta.get("onlineTime"),
        meta.get("releaseTime"),
    ]
    for candidate in direct_candidates:
        if _parse_timestamp(candidate):
            return candidate

    instance_values = []
    for item in meta.get("instances") or []:
        if not isinstance(item, dict):
            continue
        for candidate in (
            item.get("publishTime"),
            item.get("publishedAt"),
            item.get("createTime"),
            item.get("createdAt"),
        ):
            ts = _parse_timestamp(candidate)
            if ts:
                instance_values.append((ts, candidate))

    if instance_values:
        instance_values.sort(key=lambda item: item[0])
        return instance_values[0][1]

    return ""


def _normalize_comments(meta: dict, model_root: Path, offset: int = 0, limit: Optional[int] = None) -> list[dict]:
    normalized = []
    comment_items = meta.get("comments") if isinstance(meta.get("comments"), list) else []
    start = max(int(offset or 0), 0)
    selected = comment_items[start:] if limit is None else comment_items[start : start + max(int(limit or 0), 0)]
    for item in selected:
        if not isinstance(item, dict):
            continue

        comment_images = []
        for image in item.get("images") or []:
            url = _asset_url_from_item(model_root, image)
            if url:
                comment_images.append(
                    {
                        "thumb_url": url,
                        "full_url": url,
                        "fallback_url": _pick_remote_url(image) or "",
                    }
                )

        author_raw = item.get("author")
        author_name = ""
        author_avatar_url = None
        author_avatar_remote_url = ""
        if isinstance(author_raw, dict):
            author_name = str(author_raw.get("name") or author_raw.get("nickname") or author_raw.get("username") or "").strip()
            author_avatar_url = _pick_image_url(
                model_root,
                {
                    "avatarRelPath": author_raw.get("avatarRelPath"),
                    "avatarLocal": author_raw.get("avatarLocal"),
                    "avatarUrl": author_raw.get("avatarUrl"),
                },
            )
            author_avatar_remote_url = _pick_remote_url(author_raw) or ""
        elif isinstance(author_raw, str):
            author_name = author_raw.strip()

        fallback_author = ""
        for candidate in (
            item.get("userName"),
            item.get("nickname"),
            item.get("authorName"),
            item.get("creatorName"),
        ):
            if isinstance(candidate, str) and candidate.strip():
                fallback_author = candidate.strip()
                break

        normalized.append(
            {
                "author": str(
                    author_name
                    or fallback_author
                    or "匿名用户"
                ),
                "time": str(
                    item.get("time")
                    or item.get("createdAt")
                    or item.get("createTime")
                    or item.get("updatedAt")
                    or ""
                ),
                "content": str(
                    item.get("content")
                    or item.get("comment")
                    or item.get("text")
                    or item.get("message")
                    or ""
                ),
                "avatar_url": _pick_image_url(
                    model_root,
                    {
                        "avatarRelPath": item.get("avatarRelPath"),
                        "avatarLocal": item.get("avatarLocal"),
                        "avatarUrl": item.get("avatarUrl"),
                    },
                )
                or author_avatar_url,
                "avatar_remote_url": (
                    _pick_remote_url(
                        {
                            "avatarUrl": item.get("avatarUrl"),
                        }
                    )
                    or author_avatar_remote_url
                ),
                "images": comment_images,
            }
        )
    return normalized


def _normalize_attachments(meta: dict, model_root: Path) -> list[dict]:
    normalized = []
    attachment_items = []
    attachment_items.extend(meta.get("attachments") or [])
    attachment_items.extend(load_manual_attachments(model_root))

    for item in attachment_items:
        if not isinstance(item, dict):
            continue

        name = str(item.get("name") or item.get("title") or item.get("localName") or item.get("fileName") or "未命名文件")
        category = str(item.get("category") or "other").strip().lower()
        ref_name = str(item.get("localName") or item.get("relPath") or item.get("fileName") or name)
        ext = Path(ref_name).suffix.lower().lstrip(".") or "file"
        mime_type = str(item.get("mimeType") or mimetypes.guess_type(ref_name)[0] or "")
        attachment_id = str(item.get("id") or item.get("localName") or item.get("relPath") or item.get("url") or name)
        source = str(item.get("source") or "origin").strip().lower() or "origin"
        asset_url = _asset_url_from_item(model_root, item) or _pick_remote_url(item) or ""
        normalized.append(
            {
                "id": attachment_id,
                "name": name,
                "category": category,
                "category_label": ATTACHMENT_CATEGORY_LABELS.get(category, "附件文件"),
                "url": asset_url,
                "fallback_url": _pick_remote_url(item) or "",
                "ext": ext,
                "mime_type": mime_type,
                "is_image": mime_type.startswith("image/") or ext in {"png", "jpg", "jpeg", "webp", "gif", "bmp", "svg", "avif"},
                "is_manual": source == "manual",
                "can_delete": source == "manual",
                "uploaded_at": str(item.get("uploadedAt") or ""),
                "uploaded_at_label": _format_datetime(item.get("uploadedAt")),
                "source": source,
            }
        )
    return normalized


def _normalize_source(meta: dict, relative_dir: Path) -> str:
    source_raw = str(meta.get("source") or "").strip().lower()
    if relative_dir.parts and relative_dir.parts[0] == "local":
        return "local"
    if source_raw in {"mw_global", "global", "makerworld_global"}:
        return "global"
    if source_raw in {"mw_cn", "cn", "makerworld_cn"}:
        return "cn"
    return "local"


def _sample_cover(title: str) -> str:
    return f"https://placehold.co/960x960/f3f4f6/111827?text={quote(title or 'Makerhub')}"


def _normalize_model(meta_path: Path, include_detail: bool = False) -> Optional[dict]:
    try:
        meta = _read_json(meta_path)
    except (json.JSONDecodeError, OSError):
        return None

    model_root = meta_path.parent
    relative_dir = model_root.relative_to(ARCHIVE_DIR)
    source = _normalize_source(meta, relative_dir)
    stats = _normalize_stats(meta)
    tags = _extract_tags(meta)

    cover_url = _pick_image_url(
        model_root,
        meta.get("cover"),
        {"relPath": (meta.get("images") or {}).get("cover"), "url": meta.get("coverUrl")},
    )
    cover_remote_url = _pick_remote_url(
        meta.get("cover"),
        {"url": meta.get("coverUrl")},
    ) or ""

    gallery: list[dict] = []
    if include_detail:
        gallery_seen = set()

        def add_gallery(items: list[Any], kind: str) -> None:
            for item in items:
                url = _asset_url_from_item(model_root, item)
                if not url or url in gallery_seen:
                    continue
                gallery_seen.add(url)
                gallery.append(
                    {
                        "url": url,
                        "kind": kind,
                        "fallback_url": _pick_remote_url(item) or "",
                    }
                )

        if cover_url:
            gallery_seen.add(cover_url)
            gallery.append({"url": cover_url, "kind": "cover", "fallback_url": cover_remote_url})

        add_gallery(meta.get("designImages") or [], "design")
        add_gallery(meta.get("summaryImages") or [], "summary")

        if not gallery and cover_url:
            gallery.append({"url": cover_url, "kind": "cover"})

    if not cover_url:
        cover_url = gallery[0]["url"] if gallery else _sample_cover(str(meta.get("title") or "Makerhub"))

    author = meta.get("author") if isinstance(meta.get("author"), dict) else {}
    author_name = str(author.get("name") or meta.get("author") or "未知作者")
    collect_ts = _parse_timestamp(meta.get("collectDate") or meta.get("update_time"))
    publish_value = _extract_publish_value(meta)
    publish_ts = _parse_timestamp(publish_value)

    payload = {
        "model_dir": relative_dir.as_posix(),
        "detail_path": f"/models/{quote(relative_dir.as_posix(), safe='/')}",
        "meta_path": meta_path.as_posix(),
        "title": str(meta.get("title") or relative_dir.name),
        "id": str(meta.get("id") or ""),
        "source": source,
        "source_label": SOURCE_LABELS.get(source, "本地模型"),
        "origin_url": str(meta.get("url") or ""),
        "author": {
            "name": author_name,
            "url": str(author.get("url") or ""),
            "avatar_url": _pick_image_url(
                model_root,
                {
                    "avatarRelPath": author.get("avatarRelPath"),
                    "avatarLocal": author.get("avatarLocal"),
                    "avatarUrl": author.get("avatarUrl"),
                },
            ),
            "avatar_remote_url": _pick_remote_url({"avatarUrl": author.get("avatarUrl")}) or "",
        },
        "cover_url": cover_url,
        "cover_remote_url": cover_remote_url,
        "gallery": gallery,
        "tags": tags,
        "stats": stats,
        "profile_summary": _normalize_model_profile_summary(meta),
        "collect_ts": collect_ts,
        "collect_date": _format_date(meta.get("collectDate") or meta.get("update_time")),
        "publish_ts": publish_ts,
        "publish_date": _format_date(publish_value),
        "remote_sync": _normalize_remote_sync(meta),
    }
    if not include_detail:
        return payload

    summary = meta.get("summary") if isinstance(meta.get("summary"), dict) else {}
    raw_comments = meta.get("comments") if isinstance(meta.get("comments"), list) else []
    initial_comments = _normalize_comments(meta, model_root, offset=0, limit=DETAIL_COMMENTS_PAGE_SIZE)
    total_comments = max(len(raw_comments), _safe_int(stats.get("comments")))
    payload.update(
        {
            "gallery": gallery,
            "summary_html": _rewrite_summary_html(model_root, str(summary.get("html") or "")),
            "summary_text": str(summary.get("text") or summary.get("raw") or ""),
            "comments": initial_comments,
            "comments_total": total_comments,
            "comments_next_offset": len(initial_comments) if len(raw_comments) > len(initial_comments) else None,
            "instances": _normalize_instances(meta, model_root),
            "attachments": _normalize_attachments(meta, model_root),
        }
    )
    return payload


def load_archive_models(include_detail: bool = False) -> list[dict]:
    if not include_detail:
        snapshot = get_archive_snapshot()
        return _clone_model_items(list(snapshot.get("models") or []))

    models = []
    for meta_path in sorted(ARCHIVE_DIR.rglob("meta.json")):
        if not meta_path.is_file():
            continue
        model = _normalize_model(meta_path, include_detail=include_detail)
        if model:
            models.append(model)
    return models


def _sort_models(items: list[dict], sort_key: str) -> list[dict]:
    if sort_key == "publishDate":
        return sorted(items, key=lambda item: (item["publish_ts"], item["title"]), reverse=True)
    if sort_key == "downloads":
        return sorted(items, key=lambda item: item["stats"]["downloads"], reverse=True)
    if sort_key == "likes":
        return sorted(items, key=lambda item: item["stats"]["likes"], reverse=True)
    if sort_key == "prints":
        return sorted(items, key=lambda item: item["stats"]["prints"], reverse=True)
    return sorted(items, key=lambda item: (item["collect_ts"], item["title"]), reverse=True)


def _apply_model_flags(items: list[dict], flags_store: Optional[dict] = None) -> list[dict]:
    flags_store = flags_store or TaskStateStore().load_model_flags()
    favorite_set = set(flags_store.get("favorites") or [])
    printed_set = set(flags_store.get("printed") or [])
    deleted_set = set(flags_store.get("deleted") or [])

    for item in items:
        model_dir = str(item.get("model_dir") or "").strip().strip("/")
        item["local_flags"] = {
            "favorite": model_dir in favorite_set,
            "printed": model_dir in printed_set,
            "deleted": model_dir in deleted_set,
        }
    return items


def _build_subscription_deleted_index(config: Any, state_payload: dict[str, Any]) -> dict[str, list[dict]]:
    subscriptions = {item.id: item for item in getattr(config, "subscriptions", [])}

    deleted_by_key: dict[str, list[dict]] = {}
    for state_item in state_payload.get("items") or []:
        subscription_id = str(state_item.get("id") or "").strip()
        subscription = subscriptions.get(subscription_id)
        if not subscription:
            continue

        current_keys = {
            str(child.get("task_key") or "").strip()
            for child in state_item.get("current_items") or []
            if str(child.get("task_key") or "").strip()
        }
        for child in state_item.get("tracked_items") or []:
            key = str(child.get("task_key") or "").strip()
            if not key or key in current_keys:
                continue
            deleted_by_key.setdefault(key, []).append(
                {
                    "id": subscription.id,
                    "name": subscription.name,
                    "mode": subscription.mode,
                    "url": subscription.url,
                }
            )
    return deleted_by_key


def _subscription_flags_cache_signature() -> tuple[tuple[int, int], tuple[int, int]]:
    return (_file_signature(CONFIG_PATH), _file_signature(SUBSCRIPTIONS_STATE_PATH))


def _get_subscription_deleted_index(
    config: Optional[Any] = None,
    state_payload: Optional[dict[str, Any]] = None,
) -> dict[str, list[dict]]:
    signature = _subscription_flags_cache_signature()
    with _SUBSCRIPTION_FLAGS_INDEX_LOCK:
        if _SUBSCRIPTION_FLAGS_INDEX_CACHE.get("signature") == signature:
            cached = _SUBSCRIPTION_FLAGS_INDEX_CACHE.get("deleted_by_key")
            if isinstance(cached, dict):
                return cached

    resolved_config = config if config is not None else JsonStore().load()
    resolved_state = state_payload if isinstance(state_payload, dict) else TaskStateStore().load_subscriptions_state()
    deleted_by_key = _build_subscription_deleted_index(resolved_config, resolved_state)

    with _SUBSCRIPTION_FLAGS_INDEX_LOCK:
        _SUBSCRIPTION_FLAGS_INDEX_CACHE["signature"] = signature
        _SUBSCRIPTION_FLAGS_INDEX_CACHE["deleted_by_key"] = deleted_by_key
    return deleted_by_key


def _apply_subscription_flags(
    items: list[dict],
    *,
    config: Optional[Any] = None,
    state_payload: Optional[dict[str, Any]] = None,
    deleted_by_key: Optional[dict[str, list[dict]]] = None,
) -> list[dict]:
    if deleted_by_key is None:
        deleted_by_key = _get_subscription_deleted_index(config=config, state_payload=state_payload)
    for item in items:
        model_id = str(item.get("id") or "").strip()
        origin_url = normalize_source_url(str(item.get("origin_url") or "").strip())
        keys = []
        if model_id:
            keys.append(f"model:{model_id}")
        if origin_url:
            keys.append(origin_url)

        deleted_sources: list[dict] = []
        seen_source_ids: set[str] = set()
        for key in keys:
            for source in deleted_by_key.get(key, []):
                source_id = str(source.get("id") or "")
                if source_id in seen_source_ids:
                    continue
                seen_source_ids.add(source_id)
                deleted_sources.append(source)

        remote_sync = item.get("remote_sync") if isinstance(item.get("remote_sync"), dict) else {}
        if remote_sync.get("source_deleted"):
            deleted_sources.append(
                {
                    "id": "remote_refresh",
                    "name": "源端刷新",
                    "mode": "remote_refresh",
                    "url": origin_url or "",
                }
            )

        item["subscription_flags"] = {
            "deleted_on_source": bool(deleted_sources),
            "deleted_sources": deleted_sources,
        }
    return items


def _visible_models(items: list[dict]) -> list[dict]:
    return [item for item in items if not item.get("local_flags", {}).get("deleted")]


def _source_counts_from_items(items: list[dict]) -> dict[str, int]:
    counts = {"all": 0, "cn": 0, "global": 0, "local": 0}
    for item in items:
        counts["all"] += 1
        source = str(item.get("source") or "").strip().lower()
        if source in counts:
            counts[source] += 1
    return counts


def _tags_from_items(items: list[dict]) -> list[str]:
    tags: set[str] = set()
    for item in items:
        tags.update(str(tag_value) for tag_value in item.get("tags") or [] if str(tag_value).strip())
    return sorted(tags)


def build_models_payload(
    q: str = "",
    source: str = "all",
    tag: str = "",
    sort_key: str = "collectDate",
    page: int = 1,
    page_size: int = 8,
) -> dict:
    archive_snapshot = get_archive_snapshot()
    all_models = _clone_model_items(list(archive_snapshot.get("models") or []))
    flags_store = TaskStateStore().load_model_flags()
    all_models = _apply_model_flags(all_models, flags_store=flags_store)
    all_models = _apply_subscription_flags(all_models)
    visible_models = _visible_models(all_models)
    normalized_query = q.strip().lower()
    normalized_tag = tag.strip().lower()
    normalized_source = source.strip().lower() or "all"
    if normalized_tag == "__local_deleted__":
        items = [item for item in all_models if item.get("local_flags", {}).get("deleted")]
    elif normalized_tag == "__source_deleted__":
        items = [item for item in all_models if item.get("subscription_flags", {}).get("deleted_on_source")]
    else:
        items = visible_models

    if normalized_query:
        items = [
            item
            for item in items
            if normalized_query in item["title"].lower()
            or normalized_query in item["author"]["name"].lower()
            or any(normalized_query in tag_value.lower() for tag_value in item["tags"])
        ]

    if normalized_source != "all":
        items = [item for item in items if item["source"] == normalized_source]

    if normalized_tag:
        if normalized_tag == "__favorite__":
            items = [item for item in items if item.get("local_flags", {}).get("favorite")]
        elif normalized_tag == "__printed__":
            items = [item for item in items if item.get("local_flags", {}).get("printed")]
        elif normalized_tag == "__source_deleted__":
            items = [item for item in items if item.get("subscription_flags", {}).get("deleted_on_source")]
        elif normalized_tag == "__local_deleted__":
            items = [item for item in items if item.get("local_flags", {}).get("deleted")]
        else:
            items = [item for item in items if any(tag_value.lower() == normalized_tag for tag_value in item["tags"])]

    items = _sort_models(items, sort_key)
    safe_page_size = max(1, min(int(page_size or 8), 120))
    safe_page = max(int(page or 1), 1)
    total_filtered = len(items)
    start = (safe_page - 1) * safe_page_size
    end = start + safe_page_size
    paged_items = items[start:end]

    all_tags = _tags_from_items(visible_models)
    source_counts = _source_counts_from_items(visible_models)

    return {
        "items": paged_items,
        "count": len(paged_items),
        "filtered_total": total_filtered,
        "total": len(visible_models),
        "page": safe_page,
        "page_size": safe_page_size,
        "has_more": end < total_filtered,
        "tags": all_tags,
        "source_counts": source_counts,
        "filters": {
            "q": q,
            "source": normalized_source,
            "tag": tag,
            "sort": sort_key,
        },
    }


def get_model_detail(model_dir: str, include_detail: bool = True) -> Optional[dict]:
    target = (ARCHIVE_DIR / model_dir).resolve()
    try:
        target.relative_to(ARCHIVE_DIR.resolve())
    except ValueError:
        return None

    meta_path = target / "meta.json"
    if not meta_path.exists():
        invalidate_model_detail_cache(model_dir)
        return None

    clean_model_dir = str(model_dir or "").strip().strip("/")
    detail: Optional[dict]
    if include_detail:
        signature = _model_detail_signature(target, meta_path)
        cached: Optional[dict[str, Any]] = None
        with _MODEL_DETAIL_CACHE_LOCK:
            cached = _MODEL_DETAIL_CACHE.get(clean_model_dir)
        if cached and cached.get("signature") == signature:
            payload = cached.get("payload")
            detail = dict(payload) if isinstance(payload, dict) else None
        else:
            detail = _normalize_model(meta_path, include_detail=True)
            if isinstance(detail, dict):
                with _MODEL_DETAIL_CACHE_LOCK:
                    _MODEL_DETAIL_CACHE[clean_model_dir] = {
                        "signature": signature,
                        "payload": detail,
                    }
                detail = dict(detail)
    else:
        detail = _normalize_model(meta_path, include_detail=False)

    if detail is None:
        invalidate_model_detail_cache(clean_model_dir)
        return None
    _apply_model_flags([detail])
    _apply_subscription_flags([detail])
    return detail


def get_model_comments_page(model_dir: str, offset: int = 0, limit: int = DETAIL_COMMENTS_PAGE_SIZE) -> Optional[dict]:
    target = (ARCHIVE_DIR / model_dir).resolve()
    try:
        target.relative_to(ARCHIVE_DIR.resolve())
    except ValueError:
        return None

    meta_path = target / "meta.json"
    if not meta_path.exists():
        return None

    try:
        meta = _read_json(meta_path)
    except (json.JSONDecodeError, OSError):
        return None

    raw_comments = meta.get("comments") if isinstance(meta.get("comments"), list) else []
    safe_offset = max(int(offset or 0), 0)
    safe_limit = max(int(limit or 0), 1)
    items = _normalize_comments(meta, target, offset=safe_offset, limit=safe_limit)
    next_offset = safe_offset + len(items)
    if next_offset >= len(raw_comments):
        next_offset = None

    return {
        "items": items,
        "offset": safe_offset,
        "limit": safe_limit,
        "next_offset": next_offset,
        "total": max(len(raw_comments), _safe_int((_normalize_stats(meta) or {}).get("comments"))),
    }


def _delete_root_sidecar_files(archive_root: Path, model_dir: str) -> list[str]:
    removed: list[str] = []
    prefixes = (f"{model_dir}_",)
    exact_names = {f"{model_dir}_meta.json"}
    for candidate in archive_root.iterdir():
        if not candidate.is_file():
            continue
        name = candidate.name
        if name not in exact_names and not name.startswith(prefixes):
            continue
        candidate.unlink()
        removed.append(name)
    return removed


def delete_archived_models(model_dirs: list[str]) -> dict:
    removed: list[dict] = []
    skipped: list[dict] = []

    archive_root = ARCHIVE_DIR.resolve()
    for raw_value in model_dirs:
        clean_value = str(raw_value or "").strip().strip("/")
        if not clean_value:
            continue

        target = (ARCHIVE_DIR / clean_value).resolve()
        try:
            target.relative_to(archive_root)
        except ValueError:
            skipped.append({"model_dir": clean_value, "reason": "非法路径"})
            continue

        if not target.exists() or not target.is_dir():
            sidecar_files = _delete_root_sidecar_files(archive_root, clean_value)
            if sidecar_files:
                removed.append(
                    {
                        "model_dir": clean_value,
                        "id": str(extract_model_id(clean_value) or ""),
                        "title": clean_value,
                        "sidecar_removed_count": len(sidecar_files),
                    }
                )
                continue
            skipped.append({"model_dir": clean_value, "reason": "目录不存在"})
            continue

        detail = get_model_detail(clean_value, include_detail=False) or {}
        shutil.rmtree(target)
        sidecar_files = _delete_root_sidecar_files(archive_root, clean_value)
        removed.append(
            {
                "model_dir": clean_value,
                "id": str(detail.get("id") or ""),
                "title": str(detail.get("title") or clean_value),
                "sidecar_removed_count": len(sidecar_files),
            }
        )

    result = {
        "removed": removed,
        "skipped": skipped,
        "removed_count": len(removed),
        "skipped_count": len(skipped),
        "sidecar_removed_count": sum(item.get("sidecar_removed_count", 0) for item in removed),
    }
    if result["removed_count"] > 0:
        invalidate_archive_snapshot("delete_archived_models")
        for item in removed:
            invalidate_model_detail_cache(str(item.get("model_dir") or ""))
    return result


def build_tasks_payload(
    missing_fallback: Optional[list[dict]] = None,
    archive_snapshot: Optional[dict[str, Any]] = None,
) -> dict:
    store = TaskStateStore()
    archive_queue = store.load_archive_queue()
    missing_3mf = store.load_missing_3mf(fallback_items=missing_fallback)
    snapshot = archive_snapshot or get_archive_snapshot()
    model_dir_by_id = {
        str(item.get("id") or "").strip(): str(item.get("model_dir") or "").strip().strip("/")
        for item in snapshot.get("models") or []
        if str(item.get("id") or "").strip() and str(item.get("model_dir") or "").strip()
    }
    missing_items: list[dict[str, Any]] = []
    for raw_item in missing_3mf.get("items") or []:
        item = dict(raw_item or {})
        model_id = str(item.get("model_id") or "").strip()
        item["model_dir"] = str(item.get("model_dir") or model_dir_by_id.get(model_id, "")).strip().strip("/")
        missing_items.append(item)
    missing_3mf["items"] = missing_items
    archive_queue = _prune_recent_failures(
        archive_queue,
        snapshot,
        missing_3mf.get("items") or [],
    )
    organize_tasks = store.load_organize_tasks()
    remote_refresh = store.load_remote_refresh_state()
    active_organize_count = _count_active_organize_tasks(organize_tasks)
    organize_tasks["active_count"] = active_organize_count
    organize_tasks["queued_count"] = int(organize_tasks.get("queued_count") or 0)
    organize_tasks["running_count"] = int(organize_tasks.get("running_count") or 0)
    organize_tasks["detected_total"] = int(organize_tasks.get("detected_total") or 0)

    return {
        "archive_queue": archive_queue,
        "missing_3mf": missing_3mf,
        "organize_tasks": organize_tasks,
        "remote_refresh": remote_refresh,
        "summary": {
            "running_or_queued": archive_queue["running_count"] + archive_queue["queued_count"],
            "missing_3mf_count": missing_3mf["count"],
            "organize_count": active_organize_count,
        },
    }


def build_dashboard_payload(config) -> dict:
    archive_snapshot = get_archive_snapshot()
    all_models = _clone_model_items(list(archive_snapshot.get("collect_sorted") or []))
    flags_store = TaskStateStore().load_model_flags()
    task_store = TaskStateStore()
    subscription_state = task_store.load_subscriptions_state()
    all_models = _apply_model_flags(all_models, flags_store=flags_store)
    all_models = _apply_subscription_flags(all_models, config=config, state_payload=subscription_state)
    visible_models = _visible_models(all_models)
    tasks_payload = build_tasks_payload(
        missing_fallback=[
            item.model_dump() if hasattr(item, "model_dump") else item
            for item in getattr(config, "missing_3mf", [])
        ],
        archive_snapshot=archive_snapshot,
    )
    subscriptions_summary = _build_dashboard_subscriptions(config, task_store, state_payload=subscription_state)
    now = int(time.time())
    seven_days_ago = now - 7 * 24 * 60 * 60

    status_cards = [
        *build_source_health_cards(config),
        {
            "key": "proxy",
            "title": "HTTP 代理",
            "status": "已启用" if config.proxy.enabled else "未启用",
            "detail": "当前已配置代理地址。" if config.proxy.enabled and (config.proxy.http_proxy or config.proxy.https_proxy) else "当前未启用代理。",
            "tone": "ok" if config.proxy.enabled and (config.proxy.http_proxy or config.proxy.https_proxy) else "neutral",
        },
    ]

    recent_models = _clone_model_items(visible_models[:8])
    recent_week_count = len([item for item in visible_models if item["collect_ts"] >= seven_days_ago])
    organize_tasks = tasks_payload["organize_tasks"]
    remote_refresh = tasks_payload["remote_refresh"]

    return {
        "stats": [
            {"label": "模型总数", "value": len(visible_models), "hint": "默认不含 MakerHub 本地删除项"},
            {"label": "最近 7 天新增", "value": recent_week_count, "hint": "按 collectDate 统计"},
            {"label": "缺失 3MF", "value": tasks_payload["missing_3mf"]["count"], "hint": "等待重新下载"},
            {
                "label": "运行中/排队任务",
                "value": tasks_payload["summary"]["running_or_queued"],
                "hint": "归档队列当前状态",
            },
        ],
        "recent_models": recent_models,
        "system_status": status_cards,
        "automation_overview": {
            "subscriptions": {
                "count": subscriptions_summary["count"],
                "enabled_count": subscriptions_summary["enabled_count"],
                "running_count": subscriptions_summary["running_count"],
                "deleted_marked_count": subscriptions_summary["deleted_marked_count"],
                "recent_items": subscriptions_summary["recent_items"],
                "next_items": subscriptions_summary["next_items"],
            },
            "remote_refresh": {
                "enabled": bool(getattr(config.remote_refresh, "enabled", False)),
                "status": str(remote_refresh.get("status") or "idle"),
                "running": bool(remote_refresh.get("running", False)),
                "last_batch_total": int(remote_refresh.get("last_batch_total") or 0),
                "last_batch_succeeded": int(remote_refresh.get("last_batch_succeeded") or 0),
                "last_batch_failed": int(remote_refresh.get("last_batch_failed") or 0),
                "last_eligible_total": int(remote_refresh.get("last_eligible_total") or 0),
                "last_remaining_total": int(remote_refresh.get("last_remaining_total") or 0),
                "last_skipped_missing_cookie": int(remote_refresh.get("last_skipped_missing_cookie") or 0),
                "next_run_at": str(remote_refresh.get("next_run_at") or ""),
                "last_run_at": str(remote_refresh.get("last_run_at") or ""),
                "last_success_at": str(remote_refresh.get("last_success_at") or ""),
                "last_message": str(remote_refresh.get("last_message") or ""),
            },
            "organizer": {
                "source_dir": str(getattr(config.organizer, "source_dir", "") or ""),
                "target_dir": str(getattr(config.organizer, "target_dir", "") or ""),
                "move_files": bool(getattr(config.organizer, "move_files", False)),
                "detected_total": int(organize_tasks.get("detected_total") or 0),
                "running_count": int(organize_tasks.get("running_count") or 0),
                "queued_count": int(organize_tasks.get("queued_count") or 0),
                "active_count": int(organize_tasks.get("active_count") or 0),
                "recent_items": _clone_model_items([]),
                "items": list(organize_tasks.get("items") or [])[:3],
            },
        },
        "task_summary": {
            "running": tasks_payload["archive_queue"]["active"],
            "queued_count": tasks_payload["archive_queue"]["queued_count"],
            "recent_failures": tasks_payload["archive_queue"]["recent_failures"][:5],
            "missing_3mf_count": tasks_payload["missing_3mf"]["count"],
            "missing_3mf": tasks_payload["missing_3mf"]["items"][:5],
        },
    }
