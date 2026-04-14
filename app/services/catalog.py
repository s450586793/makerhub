import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from bs4 import BeautifulSoup

from app.core.settings import ARCHIVE_DIR
from app.services.batch_discovery import extract_model_id, normalize_source_url
from app.services.task_state import TaskStateStore


SOURCE_LABELS = {
    "cn": "MakerWorld 国内",
    "global": "MakerWorld 国际",
    "local": "本地模型",
}

LEGACY_CURL_FAILURE_MARKER = "No such file or directory: 'curl'"


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
    if isinstance(value, (int, float)):
        raw = int(value)
        if raw > 10_000_000_000:
            return raw // 1000
        return raw

    if not isinstance(value, str):
        return 0

    raw = value.strip()
    if not raw:
        return 0

    if raw.isdigit():
        digits = int(raw)
        if digits > 10_000_000_000:
            return digits // 1000
        return digits

    try:
        clean = raw.replace("Z", "+00:00")
        return int(datetime.fromisoformat(clean).timestamp())
    except ValueError:
        return 0


def _format_date(value: Any) -> str:
    ts = _parse_timestamp(value)
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


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
    store: TaskStateStore,
    archive_queue: dict,
    archive_models: list[dict],
    missing_items: list[dict],
) -> dict:
    recent_failures = list(archive_queue.get("recent_failures") or [])
    if not recent_failures:
        return archive_queue

    archived_model_ids = {
        str(item.get("id") or "").strip()
        for item in archive_models
        if str(item.get("id") or "").strip()
    }
    archived_urls = {
        normalize_source_url(str(item.get("origin_url") or "").strip())
        for item in archive_models
        if str(item.get("origin_url") or "").strip()
    }
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

    return store.save_archive_queue(
        {
            "active": archive_queue.get("active") or [],
            "queued": archive_queue.get("queued") or [],
            "recent_failures": kept,
        }
    )


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


def _normalize_instances(meta: dict, model_root: Path) -> list[dict]:
    normalized = []
    for item in meta.get("instances") or []:
        if not isinstance(item, dict):
            continue

        instance_key = str(item.get("id") or item.get("profileId") or len(normalized) + 1)
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

        normalized.append(
            {
                "instance_key": instance_key,
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
                "publish_date": _format_date(publish_value),
                "download_count": _safe_int(item.get("downloadCount")),
                "print_count": _safe_int(item.get("printCount")),
                "summary": str(item.get("summary") or item.get("summaryTranslated") or ""),
                "thumbnail_url": thumbnail_url,
                "thumbnail_fallback_url": thumbnail_fallback_url,
                "primary_image_url": (primary_media or {}).get("url") or thumbnail_url,
                "primary_image_fallback_url": (primary_media or {}).get("fallback_url") or thumbnail_fallback_url,
                "media": media_items,
                "file_url": _local_asset_url(
                    model_root,
                    f"instances/{Path(str(item.get('fileName') or '')).name}",
                )
                if item.get("fileName")
                else None,
                "file_name": str(item.get("fileName") or item.get("name") or ""),
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


def _normalize_comments(meta: dict, model_root: Path) -> list[dict]:
    normalized = []
    for item in meta.get("comments") or []:
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
    for item in meta.get("attachments") or []:
        if not isinstance(item, dict):
            continue

        name = str(item.get("name") or item.get("title") or item.get("localName") or item.get("fileName") or "未命名文件")
        category = str(item.get("category") or "other").strip().lower()
        normalized.append(
            {
                "name": name,
                "category": category,
                "category_label": {
                    "guide": "组装指南",
                    "manual": "使用手册",
                    "bom": "BOM 清单",
                }.get(category, "附件文件"),
                "url": _asset_url_from_item(model_root, item) or _pick_remote_url(item) or "",
                "fallback_url": _pick_remote_url(item) or "",
                "ext": Path(name).suffix.lower().lstrip(".") or "file",
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
        "collect_ts": collect_ts,
        "collect_date": _format_date(meta.get("collectDate") or meta.get("update_time")),
        "publish_ts": publish_ts,
        "publish_date": _format_date(publish_value),
    }
    if not include_detail:
        return payload

    summary = meta.get("summary") if isinstance(meta.get("summary"), dict) else {}
    payload.update(
        {
            "gallery": gallery,
            "summary_html": _rewrite_summary_html(model_root, str(summary.get("html") or "")),
            "summary_text": str(summary.get("text") or summary.get("raw") or ""),
            "comments": _normalize_comments(meta, model_root),
            "instances": _normalize_instances(meta, model_root),
            "attachments": _normalize_attachments(meta, model_root),
        }
    )
    return payload


def load_archive_models(include_detail: bool = False) -> list[dict]:
    models = []
    for meta_path in sorted(ARCHIVE_DIR.rglob("meta.json")):
        if not meta_path.is_file():
            continue
        model = _normalize_model(meta_path, include_detail=include_detail)
        if model:
            models.append(model)
    return models


def _sort_models(items: list[dict], sort_key: str) -> list[dict]:
    if sort_key == "downloads":
        return sorted(items, key=lambda item: item["stats"]["downloads"], reverse=True)
    if sort_key == "likes":
        return sorted(items, key=lambda item: item["stats"]["likes"], reverse=True)
    if sort_key == "prints":
        return sorted(items, key=lambda item: item["stats"]["prints"], reverse=True)
    return sorted(items, key=lambda item: (item["collect_ts"], item["title"]), reverse=True)


def _apply_model_flags(items: list[dict]) -> list[dict]:
    flags_store = TaskStateStore().load_model_flags()
    favorite_set = set(flags_store.get("favorites") or [])
    printed_set = set(flags_store.get("printed") or [])

    for item in items:
        model_dir = str(item.get("model_dir") or "").strip().strip("/")
        item["local_flags"] = {
            "favorite": model_dir in favorite_set,
            "printed": model_dir in printed_set,
        }
    return items


def build_models_payload(
    q: str = "",
    source: str = "all",
    tag: str = "",
    sort_key: str = "collectDate",
    page: int = 1,
    page_size: int = 8,
) -> dict:
    all_models = load_archive_models(include_detail=False)
    normalized_query = q.strip().lower()
    normalized_tag = tag.strip().lower()
    normalized_source = source.strip().lower() or "all"
    items = all_models

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
        items = [item for item in items if any(tag_value.lower() == normalized_tag for tag_value in item["tags"])]

    items = _sort_models(items, sort_key)
    safe_page_size = max(1, min(int(page_size or 8), 120))
    safe_page = max(int(page or 1), 1)
    total_filtered = len(items)
    start = (safe_page - 1) * safe_page_size
    end = start + safe_page_size
    paged_items = items[start:end]
    paged_items = _apply_model_flags(paged_items)

    all_tags = sorted({tag_value for model in all_models for tag_value in model["tags"]})
    source_counts = {
        "all": len(all_models),
        "cn": len([item for item in all_models if item["source"] == "cn"]),
        "global": len([item for item in all_models if item["source"] == "global"]),
        "local": len([item for item in all_models if item["source"] == "local"]),
    }

    return {
        "items": paged_items,
        "count": len(paged_items),
        "filtered_total": total_filtered,
        "total": len(all_models),
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
        return None
    detail = _normalize_model(meta_path, include_detail=include_detail)
    if detail is None:
        return None
    _apply_model_flags([detail])
    return detail


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

    return {
        "removed": removed,
        "skipped": skipped,
        "removed_count": len(removed),
        "skipped_count": len(skipped),
        "sidecar_removed_count": sum(item.get("sidecar_removed_count", 0) for item in removed),
    }


def build_tasks_payload(missing_fallback: Optional[list[dict]] = None) -> dict:
    store = TaskStateStore()
    archive_queue = store.load_archive_queue()
    missing_3mf = store.load_missing_3mf(fallback_items=missing_fallback)
    archive_models = load_archive_models(include_detail=False)
    archive_queue = _prune_recent_failures(
        store,
        archive_queue,
        archive_models,
        missing_3mf.get("items") or [],
    )
    organize_tasks = store.load_organize_tasks()

    return {
        "archive_queue": archive_queue,
        "missing_3mf": missing_3mf,
        "organize_tasks": organize_tasks,
        "summary": {
            "running_or_queued": archive_queue["running_count"] + archive_queue["queued_count"],
            "missing_3mf_count": missing_3mf["count"],
            "organize_count": organize_tasks["count"],
        },
    }


def build_dashboard_payload(config) -> dict:
    all_models = _sort_models(load_archive_models(include_detail=False), "collectDate")
    tasks_payload = build_tasks_payload(
        missing_fallback=[
            item.model_dump() if hasattr(item, "model_dump") else item
            for item in getattr(config, "missing_3mf", [])
        ]
    )
    now = int(time.time())
    seven_days_ago = now - 7 * 24 * 60 * 60

    cookie_map = {item.platform: item.cookie for item in config.cookies}
    status_cards = [
        {
            "title": "国内 Cookie",
            "status": "已配置" if cookie_map.get("cn", "").strip() else "未配置",
            "enabled": bool(cookie_map.get("cn", "").strip()),
        },
        {
            "title": "国际 Cookie",
            "status": "已配置" if cookie_map.get("global", "").strip() else "未配置",
            "enabled": bool(cookie_map.get("global", "").strip()),
        },
        {
            "title": "HTTP 代理",
            "status": "启用" if config.proxy.enabled else "停用",
            "enabled": bool(config.proxy.enabled and (config.proxy.http_proxy or config.proxy.https_proxy)),
        },
    ]

    recent_models = sorted(all_models, key=lambda item: item["collect_ts"], reverse=True)[:8]
    recent_week_count = len([item for item in all_models if item["collect_ts"] >= seven_days_ago])

    return {
        "stats": [
            {"label": "模型总数", "value": len(all_models), "hint": "来自 /app/archive/**/meta.json"},
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
        "task_summary": {
            "running": tasks_payload["archive_queue"]["active"],
            "queued_count": tasks_payload["archive_queue"]["queued_count"],
            "recent_failures": tasks_payload["archive_queue"]["recent_failures"][:5],
            "missing_3mf": tasks_payload["missing_3mf"]["items"][:5],
        },
    }
