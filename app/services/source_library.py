import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from app.core.settings import STATE_DIR, ensure_app_dirs
from app.core.store import JsonStore
from app.core.timezone import now as china_now, now_iso as china_now_iso, parse_datetime
from app.services.batch_discovery import normalize_source_url
from app.services.business_logs import append_business_log
from app.services.catalog import (
    _apply_model_flags,
    _apply_subscription_flags,
    _clone_model_items,
    _sort_models,
    _source_counts_from_items,
    _tags_from_items,
    _visible_models,
    get_archive_snapshot,
)
from app.services.cookie_utils import sanitize_cookie_header
from app.services.legacy_archiver import extract_next_data, fetch_html_with_requests, parse_cookies
from app.services.task_state import TaskStateStore


SOURCE_LIBRARY_METADATA_PATH = STATE_DIR / "source_library_metadata.json"
SOURCE_LIBRARY_METADATA_TTL_SECONDS = 12 * 60 * 60
SOURCE_LIBRARY_PREVIEW_LIMIT = 4
SOURCE_LIBRARY_BACKFILL_DELAY_SECONDS = 6

_SOURCE_LIBRARY_LOCK = threading.RLock()

AUTHOR_NAME_KEYS = ("name", "nickname", "displayName", "userName", "username")
AUTHOR_HANDLE_KEYS = ("handle", "userHandle", "user_handle", "username", "userName", "slug")
AUTHOR_AVATAR_KEYS = (
    "avatarUrl",
    "avatar",
    "avatarURI",
    "headIcon",
    "portraitUrl",
    "faceUrl",
    "headPic",
    "profileImage",
)
AUTHOR_FOLLOWER_KEYS = ("followerCount", "followersCount", "fanCount", "fansCount", "followedCount", "followCount")
AUTHOR_LIKE_KEYS = ("likeCount", "likesCount", "likedCount", "thumbsUpCount", "totalLikeCount")
AUTHOR_MODEL_COUNT_KEYS = ("designCount", "designCnt", "modelCount", "modelsCount", "uploadCount", "worksCount")
COLLECTION_NAME_KEYS = ("name", "title", "collectionName", "favoritesName")
COLLECTION_COUNT_KEYS = ("designCount", "designCnt", "modelCount", "modelsCount", "count")
COLLECTION_FOLLOWER_KEYS = ("followerCount", "followersCount", "favoriteCount", "favoritesCount", "followCount")
COLLECTION_COVER_KEYS = ("coverUrl", "coverImage", "imageUrl", "thumbnailUrl", "bannerUrl")
DEFAULT_STATE_SORT_ORDER = {
    "local_favorite": 0,
    "printed": 1,
    "source_deleted": 2,
    "local_deleted": 3,
}


def _now_iso() -> str:
    return china_now_iso()


def _safe_int(value: Any) -> int:
    try:
        if value in ("", None):
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_handle(value: Any) -> str:
    return _normalize_text(value).strip("@")


def _site_from_url(url: str, fallback: str = "") -> str:
    netloc = urlparse(str(url or "")).netloc.lower()
    if "makerworld.com" in netloc and "makerworld.com.cn" not in netloc:
        return "global"
    if "makerworld.com.cn" in netloc:
        return "cn"
    return str(fallback or "").strip().lower() or "cn"


def _site_badge(site: str) -> str:
    if site == "global":
        return "国际"
    if site == "local":
        return "本地"
    return "国区"


def _source_key(kind: str, site: str, reference: str) -> str:
    digest = hashlib.sha1(f"{kind}:{site}:{reference}".encode("utf-8")).hexdigest()[:12]
    return f"{kind}-{site}-{digest}"


def _author_reference(model: dict) -> tuple[str, str, str]:
    author = model.get("author") if isinstance(model.get("author"), dict) else {}
    raw_url = normalize_source_url(str(author.get("url") or ""))
    author_name = _normalize_text(author.get("name") or "未知作者")
    site = _site_from_url(raw_url, fallback=str(model.get("source") or "cn"))
    if raw_url:
        return (_source_key("author", site, raw_url), site, raw_url)
    fallback_ref = f"{site}:{author_name}"
    return (_source_key("author", site, fallback_ref), site, fallback_ref)


def _extract_handle_from_url(url: str) -> str:
    match = re.search(r"/@([^/?#]+)", urlparse(str(url or "")).path or "", re.I)
    if not match:
        return ""
    return _normalize_handle(match.group(1))


def _author_profile_url(url: str) -> str:
    normalized = normalize_source_url(url)
    if not normalized:
        return ""
    parsed = urlparse(normalized)
    path = re.sub(r"/upload/?$", "", parsed.path or "", flags=re.I).rstrip("/")
    if not path:
        return normalized
    return parsed._replace(path=path).geturl()


def _extract_collection_id_from_url(url: str) -> str:
    match = re.search(r"/collections/(\d+)", urlparse(str(url or "")).path or "", re.I)
    if not match:
        return ""
    return _normalize_text(match.group(1))


def _read_metadata_cache_unlocked() -> dict[str, Any]:
    if not SOURCE_LIBRARY_METADATA_PATH.exists():
        return {"items": {}, "updated_at": ""}
    try:
        payload = json.loads(SOURCE_LIBRARY_METADATA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"items": {}, "updated_at": ""}
    if not isinstance(payload, dict):
        return {"items": {}, "updated_at": ""}
    items = payload.get("items") if isinstance(payload.get("items"), dict) else {}
    return {
        "items": {str(key): value for key, value in items.items() if isinstance(value, dict)},
        "updated_at": str(payload.get("updated_at") or ""),
    }


def _write_metadata_cache_unlocked(payload: dict[str, Any]) -> dict[str, Any]:
    ensure_app_dirs()
    normalized = {
        "items": payload.get("items") if isinstance(payload.get("items"), dict) else {},
        "updated_at": str(payload.get("updated_at") or _now_iso()),
    }
    SOURCE_LIBRARY_METADATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = SOURCE_LIBRARY_METADATA_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(SOURCE_LIBRARY_METADATA_PATH)
    return normalized


def load_source_metadata_cache() -> dict[str, Any]:
    with _SOURCE_LIBRARY_LOCK:
        return _read_metadata_cache_unlocked()


def _save_source_metadata_item(source_key: str, payload: dict[str, Any]) -> None:
    with _SOURCE_LIBRARY_LOCK:
        cache = _read_metadata_cache_unlocked()
        items = dict(cache.get("items") or {})
        current = dict(items.get(source_key) or {})
        current.update(payload)
        current["last_synced_at"] = _now_iso()
        items[source_key] = current
        _write_metadata_cache_unlocked({"items": items, "updated_at": _now_iso()})


def _load_models(task_store: Optional[TaskStateStore] = None) -> tuple[list[dict], list[dict]]:
    task_store = task_store or TaskStateStore()
    snapshot = get_archive_snapshot()
    all_models = _clone_model_items(list(snapshot.get("models") or []))
    flags_store = task_store.load_model_flags()
    all_models = _apply_model_flags(all_models, flags_store=flags_store)
    all_models = _apply_subscription_flags(all_models)
    visible_models = _visible_models(all_models)
    return all_models, visible_models


def _preview_items_from_models(models: list[dict]) -> list[dict]:
    previews = []
    for model in _sort_models(list(models), "collectDate")[:SOURCE_LIBRARY_PREVIEW_LIMIT]:
        previews.append(
            {
                "model_dir": str(model.get("model_dir") or ""),
                "title": str(model.get("title") or "未命名模型"),
                "cover_url": str(model.get("cover_url") or ""),
            }
        )
    return previews


def _iter_nodes(payload: Any):
    if isinstance(payload, dict):
        yield payload
        for value in payload.values():
            yield from _iter_nodes(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _iter_nodes(value)


def _pick_first(node: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = node.get(key)
        text = _normalize_text(value)
        if text:
            return text
    return ""


def _pick_first_image(node: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = node.get(key)
        text = _normalize_text(value)
        if text.startswith("http://") or text.startswith("https://"):
            return text
    return ""


def _meta_title_candidates(html_text: str) -> list[str]:
    soup = BeautifulSoup(str(html_text or ""), "html.parser")
    candidates: list[str] = []
    for selector in (
        "meta[property='og:title']",
        "meta[name='og:title']",
        "meta[name='twitter:title']",
    ):
        for node in soup.select(selector):
            content = _normalize_text(node.get("content"))
            if content:
                candidates.append(content)
    title_node = soup.find("title")
    if title_node:
        text = title_node.get_text(" ", strip=True)
        if text:
            candidates.append(_normalize_text(text))
    return candidates


def _normalize_remote_title(text: str) -> str:
    clean = _normalize_text(text)
    for separator in (" - ", " | ", " — ", " – "):
        marker = f"{separator}MakerWorld"
        if clean.endswith(marker):
            clean = clean[: -len(marker)].strip()
            break
    return clean.strip(" -|—–")


def _fetch_listing_html(url: str, raw_cookie: str, proxy_config=None) -> str:
    session = requests.Session()
    session.trust_env = False
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/135.0.0.0 Safari/537.36"
            )
        }
    )
    session.cookies.update(parse_cookies(raw_cookie))
    if getattr(proxy_config, "enabled", False):
        if getattr(proxy_config, "http_proxy", ""):
            session.proxies["http"] = proxy_config.http_proxy
        if getattr(proxy_config, "https_proxy", ""):
            session.proxies["https"] = proxy_config.https_proxy
    html_text = fetch_html_with_requests(session, url, raw_cookie) or ""
    if not html_text:
        raise RuntimeError("页面内容为空。")
    lowered = html_text[:6000].lower()
    if "just a moment" in lowered or "cf-challenge" in lowered or "challenge-platform" in lowered:
        raise RuntimeError("页面返回 Cloudflare 验证页。")
    return html_text


def _extract_author_metadata_from_next_data(next_data: Any, expected_handle: str) -> dict[str, Any]:
    best_score = -1
    best_payload: dict[str, Any] = {}
    expected = expected_handle.lower()
    for node in _iter_nodes(next_data):
        if not isinstance(node, dict):
            continue
        name = _pick_first(node, AUTHOR_NAME_KEYS)
        handle = _normalize_handle(_pick_first(node, AUTHOR_HANDLE_KEYS))
        avatar_url = _pick_first_image(node, AUTHOR_AVATAR_KEYS)
        followers = 0
        likes = 0
        model_count = 0
        for key in AUTHOR_FOLLOWER_KEYS:
            followers = max(followers, _safe_int(node.get(key)))
        for key in AUTHOR_LIKE_KEYS:
            likes = max(likes, _safe_int(node.get(key)))
        for key in AUTHOR_MODEL_COUNT_KEYS:
            model_count = max(model_count, _safe_int(node.get(key)))
        if not any((name, handle, avatar_url, followers, likes, model_count)):
            continue
        score = 0
        if handle:
            score += 3
        if name:
            score += 2
        if avatar_url:
            score += 1
        if followers:
            score += 2
        if likes:
            score += 1
        if model_count:
            score += 1
        if expected and handle and handle.lower() == expected:
            score += 10
        if score > best_score:
            best_score = score
            best_payload = {
                "title": name,
                "subtitle": f"@{handle}" if handle else "",
                "avatar_url": avatar_url,
                "followers_count": followers,
                "likes_count": likes,
                "remote_model_count": model_count,
                "verified": bool(node.get("verified") or node.get("isVerified") or node.get("officialVerify")),
            }
    return best_payload


def _extract_collection_metadata_from_next_data(next_data: Any, expected_collection_id: str) -> dict[str, Any]:
    best_score = -1
    best_payload: dict[str, Any] = {}
    expected = expected_collection_id.strip()
    for node in _iter_nodes(next_data):
        if not isinstance(node, dict):
            continue
        candidate_id = _normalize_text(
            node.get("collectionId")
            or node.get("favoriteId")
            or node.get("favoritesId")
            or node.get("listId")
            or node.get("id")
        )
        title = _pick_first(node, COLLECTION_NAME_KEYS)
        cover_url = _pick_first_image(node, COLLECTION_COVER_KEYS)
        model_count = 0
        follower_count = 0
        for key in COLLECTION_COUNT_KEYS:
            model_count = max(model_count, _safe_int(node.get(key)))
        for key in COLLECTION_FOLLOWER_KEYS:
            follower_count = max(follower_count, _safe_int(node.get(key)))
        owner_name = ""
        owner_avatar_url = ""
        for candidate in (node.get("user"), node.get("userInfo"), node.get("creator"), node.get("owner")):
            if isinstance(candidate, dict):
                owner_name = owner_name or _pick_first(candidate, AUTHOR_NAME_KEYS)
                owner_avatar_url = owner_avatar_url or _pick_first_image(candidate, AUTHOR_AVATAR_KEYS)
        if not any((candidate_id, title, cover_url, model_count, follower_count, owner_name)):
            continue
        score = 0
        if title:
            score += 3
        if model_count:
            score += 2
        if follower_count:
            score += 2
        if cover_url:
            score += 1
        if owner_name:
            score += 1
        if expected and candidate_id and candidate_id == expected:
            score += 12
        if score > best_score:
            best_score = score
            best_payload = {
                "title": title,
                "subtitle": owner_name,
                "cover_url": cover_url,
                "avatar_url": owner_avatar_url,
                "followers_count": follower_count,
                "remote_model_count": model_count,
            }
    return best_payload


def _select_cookie(url: str, config) -> str:
    platform = "global" if _site_from_url(url) == "global" else "cn"
    cookie_map = {item.platform: item.cookie for item in config.cookies}
    return sanitize_cookie_header(cookie_map.get(platform) or "")


def _fetch_author_metadata(url: str, config) -> dict[str, Any]:
    html_text = _fetch_listing_html(url, _select_cookie(url, config), proxy_config=config.proxy)
    payload: dict[str, Any] = {}
    handle = _extract_handle_from_url(url)
    try:
        payload.update(_extract_author_metadata_from_next_data(extract_next_data(html_text), handle))
    except Exception:
        pass
    if not payload.get("title"):
        for candidate in _meta_title_candidates(html_text):
            normalized = _normalize_remote_title(candidate)
            if normalized and normalized.lower() != "makerworld":
                payload["title"] = normalized
                break
    if not payload.get("subtitle") and handle:
        payload["subtitle"] = f"@{handle}"
    return payload


def _fetch_collection_metadata(url: str, config) -> dict[str, Any]:
    html_text = _fetch_listing_html(url, _select_cookie(url, config), proxy_config=config.proxy)
    payload: dict[str, Any] = {}
    collection_id = _extract_collection_id_from_url(url)
    try:
        payload.update(_extract_collection_metadata_from_next_data(extract_next_data(html_text), collection_id))
    except Exception:
        pass
    if not payload.get("title"):
        for candidate in _meta_title_candidates(html_text):
            normalized = _normalize_remote_title(candidate)
            if normalized and normalized.lower() != "makerworld":
                payload["title"] = normalized
                break
    return payload


def _subscription_state_map(task_store: TaskStateStore) -> dict[str, dict[str, Any]]:
    payload = task_store.load_subscriptions_state()
    return {
        str(item.get("id") or "").strip(): item
        for item in payload.get("items") or []
        if str(item.get("id") or "").strip()
    }


def _task_key_lookup(models: list[dict]) -> dict[str, dict]:
    lookup: dict[str, dict] = {}
    for model in models:
        model_id = _normalize_text(model.get("id"))
        origin_url = normalize_source_url(str(model.get("origin_url") or ""))
        if model_id:
            lookup[f"model:{model_id}"] = model
        if origin_url:
            lookup[origin_url] = model
    return lookup


def _base_group(
    *,
    key: str,
    kind: str,
    card_kind: str,
    title: str,
    subtitle: str,
    site: str,
    canonical_url: str = "",
    route_kind: str = "source",
) -> dict[str, Any]:
    return {
        "key": key,
        "kind": kind,
        "card_kind": card_kind,
        "route_kind": route_kind,
        "title": title,
        "subtitle": subtitle,
        "site": site,
        "site_badge": _site_badge(site),
        "canonical_url": canonical_url,
        "avatar_url": "",
        "cover_url": "",
        "verified": False,
        "followers_count": 0,
        "likes_count": 0,
        "remote_model_count": 0,
        "secondary_count": 0,
        "model_dirs": [],
        "_model_dir_set": set(),
        "preview_models": [],
        "stats": [],
        "description": "",
        "sort_score": 0,
    }


def _append_group_model(group: dict[str, Any], model: dict[str, Any]) -> None:
    model_dir = str(model.get("model_dir") or "").strip()
    if not model_dir or model_dir in group["_model_dir_set"]:
        return
    group["_model_dir_set"].add(model_dir)
    group["model_dirs"].append(model_dir)


def _author_avatar_from_members(members: list[dict]) -> str:
    for model in members:
        author = model.get("author") if isinstance(model.get("author"), dict) else {}
        for key in ("avatar_url", "avatar_remote_url"):
            avatar_url = str(author.get(key) or "").strip()
            if avatar_url:
                return avatar_url
    return ""


def _finalize_group(group: dict[str, Any], models_by_dir: dict[str, dict], metadata: dict[str, Any]) -> dict[str, Any]:
    members = [models_by_dir[item] for item in group.get("model_dirs") or [] if item in models_by_dir]
    preview_models = _preview_items_from_models(members)
    group["preview_models"] = preview_models
    if metadata.get("title"):
        group["title"] = metadata["title"]
    if metadata.get("subtitle"):
        group["subtitle"] = metadata["subtitle"]
    if group.get("kind") == "author":
        group["avatar_url"] = _author_avatar_from_members(members) or str(group.get("avatar_url") or "")
    elif metadata.get("avatar_url"):
        group["avatar_url"] = metadata["avatar_url"]
    if metadata.get("cover_url"):
        group["cover_url"] = metadata["cover_url"]
    elif preview_models:
        group["cover_url"] = str(preview_models[0].get("cover_url") or "")
    group["verified"] = bool(metadata.get("verified") or group.get("verified"))
    group["followers_count"] = _safe_int(metadata.get("followers_count") or group.get("followers_count"))
    group["likes_count"] = _safe_int(metadata.get("likes_count") or group.get("likes_count"))
    group["remote_model_count"] = _safe_int(metadata.get("remote_model_count") or group.get("remote_model_count"))
    if metadata.get("description"):
        group["description"] = metadata["description"]
    local_model_count = len(group.get("model_dirs") or [])
    group["local_model_count"] = local_model_count
    group["model_count"] = max(local_model_count, _safe_int(group.get("remote_model_count")))
    group["stats"] = _build_group_stats(group, members)
    group["sort_score"] = max(group["followers_count"], group["model_count"], group["likes_count"])
    group.pop("_model_dir_set", None)
    return group


def _build_group_stats(group: dict[str, Any], members: list[dict]) -> list[dict[str, Any]]:
    kind = str(group.get("kind") or "")
    model_count = _safe_int(group.get("model_count")) or len(members)
    if kind == "author":
        likes_value = group.get("likes_count") or sum(_safe_int(item.get("stats", {}).get("likes")) for item in members)
        return [
            {"label": "粉丝数", "value": _safe_int(group.get("followers_count"))},
            {"label": "获赞", "value": _safe_int(likes_value)},
            {"label": "模型", "value": model_count},
        ]
    if kind in {"collection", "favorite"}:
        return [
            {"label": "模型", "value": model_count},
            {"label": "粉丝", "value": _safe_int(group.get("followers_count"))},
        ]
    if kind == "local":
        authors = len({str(item.get("author", {}).get("name") or "").strip() for item in members if str(item.get("author", {}).get("name") or "").strip()})
        return [
            {"label": "模型", "value": model_count},
            {"label": "作者", "value": authors},
        ]
    if kind == "local_favorite":
        local_count = len([item for item in members if item.get("source") == "local"])
        return [
            {"label": "模型", "value": model_count},
            {"label": "本地", "value": local_count},
        ]
    if kind == "printed":
        local_count = len([item for item in members if item.get("source") == "local"])
        return [
            {"label": "模型", "value": model_count},
            {"label": "本地", "value": local_count},
        ]
    if kind == "source_deleted":
        source_ids = set()
        for item in members:
            for source in item.get("subscription_flags", {}).get("deleted_sources") or []:
                source_ids.add(str(source.get("id") or source.get("url") or source.get("name") or ""))
        return [
            {"label": "模型", "value": model_count},
            {"label": "来源", "value": len([value for value in source_ids if value])},
        ]
    if kind == "local_deleted":
        local_count = len([item for item in members if item.get("source") == "local"])
        return [
            {"label": "模型", "value": model_count},
            {"label": "本地", "value": local_count},
        ]
    return [{"label": "模型", "value": model_count}]


def _group_local_sources(visible_models: list[dict]) -> list[dict]:
    local_models = [item for item in visible_models if item.get("source") == "local"]
    group = _base_group(
        key="local-organizer",
        kind="local",
        card_kind="collection",
        title="本地整理",
        subtitle="本地 3MF 归档",
        site="local",
    )
    for model in local_models:
        _append_group_model(group, model)
    return [group]


def _group_state_cards(all_models: list[dict], visible_models: list[dict]) -> list[dict]:
    defs = [
        ("local_favorite", "本地收藏", "已在 MakerHub 标记收藏"),
        ("printed", "已打印", "已在 MakerHub 标记已打印"),
        ("source_deleted", "源端删除", "订阅来源或源端刷新判定已删除"),
        ("local_deleted", "本地删除", "已在 MakerHub 本地隐藏"),
    ]
    groups: list[dict] = []
    for kind, title, subtitle in defs:
        group = _base_group(
            key=kind,
            kind=kind,
            card_kind="collection",
            title=title,
            subtitle=subtitle,
            site="local",
            route_kind="state",
        )
        base_models = visible_models
        if kind == "local_favorite":
            matched = [item for item in visible_models if item.get("local_flags", {}).get("favorite")]
        elif kind == "printed":
            matched = [item for item in visible_models if item.get("local_flags", {}).get("printed")]
        elif kind == "source_deleted":
            matched = [item for item in visible_models if item.get("subscription_flags", {}).get("deleted_on_source")]
        else:
            base_models = all_models
            matched = [item for item in all_models if item.get("local_flags", {}).get("deleted")]
        for model in matched:
            _append_group_model(group, model)
        groups.append(group)
    return groups


def _group_author_sources(visible_models: list[dict]) -> list[dict]:
    groups: dict[str, dict[str, Any]] = {}
    for model in visible_models:
        if model.get("source") == "local":
            continue
        key, site, reference = _author_reference(model)
        author = model.get("author") if isinstance(model.get("author"), dict) else {}
        group = groups.setdefault(
            key,
            _base_group(
                key=key,
                kind="author",
                card_kind="author",
                title=_normalize_text(author.get("name") or "未知作者"),
                subtitle=f"@{_extract_handle_from_url(reference)}" if reference.startswith("http") else "",
                site=site,
                canonical_url=reference if reference.startswith("http") else "",
            ),
        )
        if not group.get("avatar_url"):
            group["avatar_url"] = str(author.get("avatar_url") or "")
        group["likes_count"] += _safe_int(model.get("stats", {}).get("likes"))
        _append_group_model(group, model)
    return list(groups.values())


def _subscription_kind(url: str) -> str:
    path = (urlparse(str(url or "")).path or "").lower()
    if "/collections/" in path:
        return "collection"
    return "favorite"


def _group_subscription_sources(
    visible_models: list[dict],
    store: JsonStore,
    task_store: TaskStateStore,
) -> tuple[list[dict], list[dict], list[dict]]:
    config = store.load()
    state_map = _subscription_state_map(task_store)
    lookup = _task_key_lookup(visible_models)
    authors: list[dict] = []
    collections: list[dict] = []
    favorites: list[dict] = []

    for subscription in getattr(config, "subscriptions", []):
        source_url = normalize_source_url(subscription.url)
        if not source_url:
            continue
        site = _site_from_url(source_url)
        mode = str(subscription.mode or "").strip()
        if mode == "author_upload":
            author_url = _author_profile_url(source_url) or source_url
            handle = _extract_handle_from_url(author_url or source_url)
            title = _normalize_text(subscription.name) or (f"@{handle}" if handle else "作者订阅")
            group = _base_group(
                key=_source_key("author", site, author_url or source_url),
                kind="author",
                card_kind="author",
                title=title,
                subtitle=f"@{handle}" if handle else "MakerWorld 作者",
                site=site,
                canonical_url=source_url,
            )
        elif mode == "collection_models":
            kind = _subscription_kind(source_url)
            title = _normalize_text(subscription.name)
            if not title:
                title = "合集" if kind == "collection" else "收藏夹"
            group = _base_group(
                key=_source_key(kind, site, source_url),
                kind=kind,
                card_kind="collection",
                title=title,
                subtitle="MakerWorld 来源",
                site=site,
                canonical_url=source_url,
            )
        else:
            continue
        group["subscription_id"] = str(subscription.id or "")
        group["subscription_mode"] = mode
        group["subscription_enabled"] = bool(subscription.enabled)
        group["subscription_updated_at"] = str(subscription.updated_at or "")
        group["subscription_created_at"] = str(subscription.created_at or "")
        state_item = state_map.get(subscription.id) or {}
        current_items = state_item.get("current_items") or []
        tracked_items = state_item.get("tracked_items") or []
        source_items = current_items or tracked_items
        source_model_count = max(len(source_items), _safe_int(state_item.get("last_discovered_count")))
        if source_model_count:
            group["remote_model_count"] = source_model_count
        for child in source_items:
            task_key = _normalize_text(child.get("task_key"))
            url_key = normalize_source_url(str(child.get("url") or ""))
            matched = lookup.get(task_key) or lookup.get(url_key)
            if matched:
                _append_group_model(group, matched)
        if mode == "author_upload":
            if not group.get("model_dirs"):
                author_profile_url = _author_profile_url(source_url)
                for model in visible_models:
                    _, _, model_ref = _author_reference(model)
                    if _author_profile_url(model_ref) == author_profile_url:
                        _append_group_model(group, model)
            authors.append(group)
        elif group.get("kind") == "collection":
            collections.append(group)
        elif group.get("kind") == "favorite":
            favorites.append(group)
    return authors, collections, favorites


def _group_sort_timestamp(group: dict[str, Any]) -> int:
    for key in ("subscription_updated_at", "subscription_created_at"):
        raw = str(group.get(key) or "").strip()
        if not raw:
            continue
        parsed = parse_datetime(raw)
        if parsed is not None:
            return int(parsed.timestamp())
    return 0


def _sort_source_groups(groups: list[dict], sort_key: str) -> list[dict]:
    clean_sort = str(sort_key or "").strip().lower()
    if clean_sort == "followers":
        return sorted(
            groups,
            key=lambda item: (
                -_safe_int(item.get("followers_count")),
                -int(item.get("model_count") or 0),
                str(item.get("title") or ""),
            ),
        )
    if clean_sort == "models":
        return sorted(
            groups,
            key=lambda item: (
                -int(item.get("model_count") or 0),
                -_safe_int(item.get("followers_count")),
                str(item.get("title") or ""),
            ),
        )
    return sorted(
        groups,
        key=lambda item: (
            -_group_sort_timestamp(item),
            -int(item.get("model_count") or 0),
            str(item.get("title") or ""),
        ),
    )


def _group_models(store: Optional[JsonStore] = None, task_store: Optional[TaskStateStore] = None) -> tuple[dict[str, dict[str, Any]], list[dict], list[dict]]:
    store = store or JsonStore()
    task_store = task_store or TaskStateStore()
    all_models, visible_models = _load_models(task_store=task_store)
    models_by_dir = {str(item.get("model_dir") or ""): item for item in all_models}
    visible_by_dir = {str(item.get("model_dir") or ""): item for item in visible_models}
    metadata_cache = load_source_metadata_cache().get("items") or {}

    author_groups_raw, collection_groups_raw, favorite_groups_raw = _group_subscription_sources(visible_models, store=store, task_store=task_store)
    author_groups = [_finalize_group(group, visible_by_dir, metadata_cache.get(group["key"]) or {}) for group in author_groups_raw]
    collection_groups = [_finalize_group(group, visible_by_dir, metadata_cache.get(group["key"]) or {}) for group in collection_groups_raw]
    favorite_groups = [_finalize_group(group, visible_by_dir, metadata_cache.get(group["key"]) or {}) for group in favorite_groups_raw]
    local_groups = [_finalize_group(group, visible_by_dir, {}) for group in _group_local_sources(visible_models)]
    state_groups = [_finalize_group(group, models_by_dir, {}) for group in _group_state_cards(all_models, visible_models)]

    groups = {
        group["key"]: group
        for group in [*author_groups, *collection_groups, *favorite_groups, *local_groups, *state_groups]
    }
    sections = [
        {"key": "authors", "label": "作者", "items": _sort_source_groups(author_groups, "recent")},
        {"key": "collections", "label": "合集", "items": _sort_source_groups(collection_groups, "recent")},
        {"key": "favorites", "label": "收藏夹", "items": _sort_source_groups(favorite_groups, "recent")},
        {"key": "locals", "label": "本地库", "items": local_groups},
        {"key": "states", "label": "状态", "items": sorted(state_groups, key=lambda item: DEFAULT_STATE_SORT_ORDER.get(str(item.get("key") or ""), 99))},
    ]
    return groups, all_models, sections


def build_source_library_payload(q: str = "", store: Optional[JsonStore] = None, task_store: Optional[TaskStateStore] = None) -> dict[str, Any]:
    groups, all_models, sections = _group_models(store=store, task_store=task_store)
    normalized_query = _normalize_text(q).lower()
    total_cards = 0
    filtered_sections = []
    for section in sections:
        items = list(section.get("items") or [])
        if normalized_query:
            items = [
                item
                for item in items
                if normalized_query in str(item.get("title") or "").lower()
                or normalized_query in str(item.get("subtitle") or "").lower()
                or normalized_query in str(item.get("description") or "").lower()
            ]
        total_cards += len(items)
        filtered_sections.append(
            {
                "key": section.get("key"),
                "label": section.get("label"),
                "count": len(items),
                "items": items,
            }
        )
    return {
        "sections": filtered_sections,
        "count": total_cards,
        "filters": {"q": q},
        "summary": {
            "card_count": total_cards,
            "model_count": len(_visible_models(all_models)),
        },
    }


def build_subscription_overview_payload(
    *,
    store: Optional[JsonStore] = None,
    task_store: Optional[TaskStateStore] = None,
) -> dict[str, Any]:
    store = store or JsonStore()
    task_store = task_store or TaskStateStore()
    config = store.load()
    settings = config.subscription_settings.model_dump()
    all_models, visible_models = _load_models(task_store=task_store)
    models_by_dir = {str(item.get("model_dir") or ""): item for item in all_models}
    visible_by_dir = {str(item.get("model_dir") or ""): item for item in visible_models}
    metadata_cache = load_source_metadata_cache().get("items") or {}
    authors_raw, collection_groups_raw, favorite_groups_raw = _group_subscription_sources(visible_models, store=store, task_store=task_store)
    source_groups = [
        *[_finalize_group(group, visible_by_dir, metadata_cache.get(group["key"]) or {}) for group in authors_raw],
        *[_finalize_group(group, visible_by_dir, metadata_cache.get(group["key"]) or {}) for group in collection_groups_raw],
        *[_finalize_group(group, visible_by_dir, metadata_cache.get(group["key"]) or {}) for group in favorite_groups_raw],
    ]
    if settings.get("hide_disabled_from_cards"):
        source_groups = [item for item in source_groups if item.get("subscription_enabled")]
    source_groups = _sort_source_groups(source_groups, str(settings.get("card_sort") or "recent"))
    state_groups = sorted(
        [_finalize_group(group, models_by_dir, {}) for group in _group_state_cards(all_models, visible_models)],
        key=lambda item: DEFAULT_STATE_SORT_ORDER.get(str(item.get("key") or ""), 99),
    )
    return {
        "sections": [
            {
                "key": "subscription_sources",
                "label": "订阅来源",
                "count": len(source_groups),
                "items": source_groups,
            },
            {
                "key": "subscription_states",
                "label": "本地状态",
                "count": len(state_groups),
                "items": state_groups,
            },
        ],
        "settings": settings,
    }


def _subset_models_payload(
    items: list[dict],
    *,
    q: str = "",
    source: str = "all",
    tag: str = "",
    sort_key: str = "collectDate",
    page: int = 1,
    page_size: int = 8,
    default_include_deleted: bool = False,
) -> dict[str, Any]:
    normalized_query = q.strip().lower()
    normalized_tag = tag.strip().lower()
    normalized_source = source.strip().lower() or "all"
    all_subset = list(items)
    visible_subset = _visible_models(all_subset)
    base_subset = list(all_subset if default_include_deleted else visible_subset)
    selected = list(base_subset)

    if normalized_tag == "__local_deleted__":
        selected = [item for item in all_subset if item.get("local_flags", {}).get("deleted")]
    elif normalized_tag == "__source_deleted__":
        selected = [item for item in base_subset if item.get("subscription_flags", {}).get("deleted_on_source")]

    if normalized_query:
        selected = [
            item
            for item in selected
            if normalized_query in item["title"].lower()
            or normalized_query in item["author"]["name"].lower()
            or any(normalized_query in tag_value.lower() for tag_value in item["tags"])
        ]

    if normalized_source != "all":
        selected = [item for item in selected if item["source"] == normalized_source]

    if normalized_tag:
        if normalized_tag == "__favorite__":
            selected = [item for item in selected if item.get("local_flags", {}).get("favorite")]
        elif normalized_tag == "__printed__":
            selected = [item for item in selected if item.get("local_flags", {}).get("printed")]
        elif normalized_tag == "__source_deleted__":
            selected = [item for item in selected if item.get("subscription_flags", {}).get("deleted_on_source")]
        elif normalized_tag == "__local_deleted__":
            selected = [item for item in selected if item.get("local_flags", {}).get("deleted")]
        else:
            selected = [item for item in selected if any(tag_value.lower() == normalized_tag for tag_value in item["tags"])]

    selected = _sort_models(selected, sort_key)
    safe_page_size = max(1, min(int(page_size or 8), 120))
    safe_page = max(int(page or 1), 1)
    total_filtered = len(selected)
    start = (safe_page - 1) * safe_page_size
    end = start + safe_page_size
    paged_items = selected[start:end]
    base_tags = _tags_from_items(base_subset)
    base_source_counts = _source_counts_from_items(base_subset)

    return {
        "items": paged_items,
        "count": len(paged_items),
        "filtered_total": total_filtered,
        "total": len(base_subset),
        "page": safe_page,
        "page_size": safe_page_size,
        "has_more": end < total_filtered,
        "tags": base_tags,
        "source_counts": base_source_counts,
        "filters": {
            "q": q,
            "source": normalized_source,
            "tag": tag,
            "sort": sort_key,
        },
    }


def build_source_group_models_payload(
    source_type: str,
    source_key: str,
    *,
    q: str = "",
    source: str = "all",
    tag: str = "",
    sort_key: str = "collectDate",
    page: int = 1,
    page_size: int = 8,
    store: Optional[JsonStore] = None,
    task_store: Optional[TaskStateStore] = None,
) -> Optional[dict[str, Any]]:
    groups, all_models, _ = _group_models(store=store, task_store=task_store)
    group = groups.get(source_key)
    if not group or str(group.get("route_kind") or "") != "source":
        return None
    if str(group.get("kind") or "") != str(source_type or "").strip():
        return None
    all_models_by_dir = {str(item.get("model_dir") or ""): item for item in all_models}
    members = [all_models_by_dir[item] for item in group.get("model_dirs") or [] if item in all_models_by_dir]
    payload = _subset_models_payload(
        members,
        q=q,
        source=source,
        tag=tag,
        sort_key=sort_key,
        page=page,
        page_size=page_size,
        default_include_deleted=str(group.get("key") or "") == "local_deleted",
    )
    payload["view"] = group
    return payload


def build_state_group_models_payload(
    state_key: str,
    *,
    q: str = "",
    source: str = "all",
    tag: str = "",
    sort_key: str = "collectDate",
    page: int = 1,
    page_size: int = 8,
    store: Optional[JsonStore] = None,
    task_store: Optional[TaskStateStore] = None,
) -> Optional[dict[str, Any]]:
    groups, all_models, _ = _group_models(store=store, task_store=task_store)
    group = groups.get(state_key)
    if not group or str(group.get("route_kind") or "") != "state":
        return None
    all_models_by_dir = {str(item.get("model_dir") or ""): item for item in all_models}
    members = [all_models_by_dir[item] for item in group.get("model_dirs") or [] if item in all_models_by_dir]
    payload = _subset_models_payload(
        members,
        q=q,
        source=source,
        tag=tag,
        sort_key=sort_key,
        page=page,
        page_size=page_size,
        default_include_deleted=str(group.get("key") or "") == "local_deleted",
    )
    payload["view"] = group
    return payload


def _stale_metadata(item: dict[str, Any], *, force: bool) -> bool:
    if force:
        return True
    last_synced = str(item.get("last_synced_at") or "")
    if not last_synced:
        return True
    synced_at = parse_datetime(last_synced)
    if synced_at is None:
        return True
    age_seconds = (china_now() - synced_at).total_seconds()
    if age_seconds >= SOURCE_LIBRARY_METADATA_TTL_SECONDS:
        return True
    primary_fields = (
        str(item.get("title") or "").strip(),
        str(item.get("avatar_url") or "").strip(),
        str(item.get("cover_url") or "").strip(),
    )
    return not any(primary_fields)


def _source_identity_from_subscription(url: str, mode: str) -> Optional[dict[str, str]]:
    source_url = normalize_source_url(url)
    if not source_url:
        return None
    clean_mode = str(mode or "").strip()
    site = _site_from_url(source_url)
    if clean_mode == "author_upload":
        author_url = _author_profile_url(source_url) or source_url
        return {
            "key": _source_key("author", site, author_url),
            "kind": "author",
            "site": site,
            "canonical_url": source_url,
            "fetch_url": author_url,
        }
    if clean_mode == "collection_models":
        kind = _subscription_kind(source_url)
        return {
            "key": _source_key(kind, site, source_url),
            "kind": kind,
            "site": site,
            "canonical_url": source_url,
            "fetch_url": source_url,
        }
    return None


def _positive_int(value: Any) -> int:
    numeric = _safe_int(value)
    return numeric if numeric > 0 else 0


def refresh_subscription_source_metadata(
    *,
    url: str,
    mode: str,
    config,
    source_model_count: Any = 0,
) -> dict[str, Any]:
    identity = _source_identity_from_subscription(url, mode)
    if not identity:
        return {"refreshed": False, "reason": "unsupported"}

    kind = identity["kind"]
    fetch_url = identity["fetch_url"]
    if kind == "author":
        payload = _fetch_author_metadata(fetch_url, config)
    else:
        payload = _fetch_collection_metadata(fetch_url, config)

    remote_model_count = _positive_int(source_model_count) or _positive_int(payload.get("remote_model_count"))
    if remote_model_count:
        payload["remote_model_count"] = remote_model_count

    _save_source_metadata_item(
        identity["key"],
        {
            **payload,
            "kind": kind,
            "canonical_url": identity["canonical_url"],
            "site": identity["site"],
            "error": "",
        },
    )
    return {
        "refreshed": True,
        "source_key": identity["key"],
        "kind": kind,
        "remote_model_count": remote_model_count,
        "title": str(payload.get("title") or ""),
    }


def refresh_source_metadata(force: bool = False, store: Optional[JsonStore] = None, task_store: Optional[TaskStateStore] = None) -> dict[str, Any]:
    store = store or JsonStore()
    task_store = task_store or TaskStateStore()
    groups, _, _ = _group_models(store=store, task_store=task_store)
    config = store.load()
    metadata_cache = load_source_metadata_cache().get("items") or {}

    total = 0
    refreshed = 0
    failed = 0
    for group in groups.values():
        if str(group.get("kind") or "") not in {"author", "collection", "favorite"}:
            continue
        source_key = str(group.get("key") or "")
        canonical_url = str(group.get("canonical_url") or "")
        if not source_key or not canonical_url:
            continue
        total += 1
        cached = metadata_cache.get(source_key) or {}
        if not _stale_metadata(cached, force=force):
            continue
        try:
            if group.get("kind") == "author":
                payload = _fetch_author_metadata(canonical_url, config)
            else:
                payload = _fetch_collection_metadata(canonical_url, config)
            if not payload.get("title"):
                payload["title"] = str(group.get("title") or "")
            _save_source_metadata_item(
                source_key,
                {
                    **payload,
                    "kind": group.get("kind"),
                    "canonical_url": canonical_url,
                    "site": group.get("site"),
                    "error": "",
                },
            )
            refreshed += 1
        except Exception as exc:
            failed += 1
            _save_source_metadata_item(
                source_key,
                {
                    "kind": group.get("kind"),
                    "canonical_url": canonical_url,
                    "site": group.get("site"),
                    "error": _normalize_text(str(exc))[:240],
                },
            )
    if total:
        append_business_log(
            "source_library",
            "metadata_refreshed",
            "来源卡元数据补全完成。",
            total=total,
            refreshed=refreshed,
            failed=failed,
            force=force,
        )
    return {
        "total": total,
        "refreshed": refreshed,
        "failed": failed,
    }


class SourceLibraryManager:
    def __init__(self, store: Optional[JsonStore] = None, task_store: Optional[TaskStateStore] = None) -> None:
        self.store = store or JsonStore()
        self.task_store = task_store or TaskStateStore()
        self._thread: Optional[threading.Thread] = None
        self._thread_lock = threading.Lock()

    def start(self) -> None:
        with self._thread_lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._background_backfill,
                name="makerhub-source-library",
                daemon=True,
            )
            self._thread.start()

    def _background_backfill(self) -> None:
        time.sleep(SOURCE_LIBRARY_BACKFILL_DELAY_SECONDS)
        try:
            refresh_source_metadata(force=False, store=self.store, task_store=self.task_store)
        except Exception as exc:
            append_business_log(
                "source_library",
                "metadata_refresh_failed",
                "来源卡元数据补全失败。",
                level="warning",
                error=_normalize_text(str(exc))[:240],
            )
