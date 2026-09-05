"""MakerWorld 来源列表和账号数据的纯解析器。"""

import re
from typing import Any, Optional
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup

from app.services.makerworld_parsers.common import (
    COLLECTION_DETAIL_RE,
    MODEL_PATH_RE,
    extract_model_id,
    normalize_model_url,
    normalize_source_url,
)
from app.services.makerworld_parsers.model import extract_next_data


ACCOUNT_AVATAR_KEYS = (
    "avatar",
    "avatarUrl",
    "avatar_url",
    "avatarImageUrl",
    "avatarURI",
    "headIcon",
    "portraitUrl",
    "faceUrl",
    "headPic",
    "profileImage",
)


def _iter_nodes(node: Any):
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _iter_nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_nodes(item)


def _iter_dicts(node: Any):
    for item in _iter_nodes(node):
        if isinstance(item, dict):
            yield item


def _coerce_numeric_string(value: Any) -> str:
    try:
        if value in (None, ""):
            return ""
        return str(int(str(value).strip()))
    except Exception:
        return ""


def _extract_handle_from_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("@"):
        raw = f"/zh/{raw}"
    parsed = urlparse(raw)
    path = parsed.path or raw
    match = re.search(r"(?:^|/)@([A-Za-z0-9_.-]+)(?:[/?#]|$)", path)
    return match.group(1).strip("@").strip() if match else ""


def _extract_node_uid(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    for key in ("uid", "userId", "ownerUid", "creatorUid", "authorUid", "id"):
        candidate = _coerce_numeric_string(node.get(key))
        if candidate:
            return candidate
    return ""


def _extract_node_handle(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    for key in ("username", "userName", "slug", "handle", "userHandle", "user_handle", "creatorUsername"):
        candidate = str(node.get(key) or "").strip().lstrip("@")
        if candidate:
            return candidate
    for key in ("url", "homepage", "profileUrl", "authorUrl", "link", "href"):
        candidate = _extract_handle_from_url(str(node.get(key) or ""))
        if candidate:
            return candidate
    return ""


def _extract_avatar_url(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    for key in ACCOUNT_AVATAR_KEYS:
        value = node.get(key)
        if isinstance(value, dict):
            for nested_key in ("url", "src", "avatarUrl", "imageUrl"):
                nested = str(value.get(nested_key) or "").strip()
                if nested:
                    return nested
        elif isinstance(value, list):
            for item in value:
                nested = _extract_avatar_url(item)
                if nested:
                    return nested
        else:
            candidate = str(value or "").strip()
            if candidate:
                return candidate
    return ""


def _safe_positive_int(value: Any) -> Optional[int]:
    try:
        if value in (None, ""):
            return None
        parsed = int(str(value).replace(",", "").strip())
        return parsed if parsed > 0 else None
    except Exception:
        return None


def _extract_design_id_from_hit(hit: Any) -> str:
    if not isinstance(hit, dict):
        return ""
    for key in ("designId", "id", "modelId"):
        candidate = _coerce_numeric_string(hit.get(key))
        if candidate and any(
            field in hit
            for field in ("title", "name", "coverUrl", "downloadCount", "printCount", "likeCount", "commentCount")
        ):
            return candidate
    for key in ("design", "model", "item"):
        candidate = _extract_design_id_from_hit(hit.get(key))
        if candidate:
            return candidate
    return ""


def _looks_like_design_hit(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    if _extract_design_id_from_hit(node):
        return True
    return any(
        key in node
        for key in (
            "title",
            "name",
            "coverUrl",
            "designCreator",
            "downloadCount",
            "printCount",
            "likeCount",
            "commentCount",
        )
    )


def extract_total_count(payload: dict, fallback: int) -> Optional[int]:
    for key in ("total", "count", "totalCount"):
        try:
            value = payload.get(key)
            if value in (None, ""):
                continue
            return max(int(value), 0)
        except Exception:
            continue
    return fallback if fallback >= 0 else None


def extract_has_next(payload: dict) -> Optional[bool]:
    value = payload.get("hasNext")
    if isinstance(value, bool):
        return value
    return None


def extract_hits_payload(payload: Any) -> Optional[dict]:
    best_node: Optional[dict] = None
    best_score: tuple[int, int, int, int] = (-1, -1, -1, -1)
    for node in _iter_dicts(payload):
        hits = node.get("hits")
        if not isinstance(hits, list):
            continue
        total = extract_total_count(node, len(hits))
        design_like_count = sum(1 for hit in hits[:8] if _looks_like_design_hit(hit))
        score = (
            1 if design_like_count > 0 else 0,
            max(int(total or 0), 0),
            design_like_count,
            len(hits),
        )
        if score > best_score:
            best_node = node
            best_score = score
    return best_node


def _collect_model_urls_from_node(node: object, found: set[str], base_url: str) -> None:
    if isinstance(node, str):
        normalized = normalize_model_url(node, fallback_base=base_url)
        if normalized:
            found.add(normalized)
        return

    if isinstance(node, list):
        for item in node:
            _collect_model_urls_from_node(item, found, base_url)
        return

    if not isinstance(node, dict):
        return

    for key in ("url", "link", "href", "designUrl", "modelUrl"):
        normalized = normalize_model_url(str(node.get(key) or ""), fallback_base=base_url)
        if normalized:
            found.add(normalized)

    title = str(node.get("title") or node.get("name") or "").strip()
    candidate_id = node.get("designId") or node.get("id") or node.get("modelId")
    try:
        candidate_id = str(int(candidate_id))
    except Exception:
        candidate_id = ""
    if candidate_id and title and any(key in node for key in ("coverUrl", "downloadCount", "printCount", "likeCount", "designCreator", "user")):
        found.add(normalize_model_url(f"/zh/models/{candidate_id}", fallback_base=base_url))

    for value in node.values():
        _collect_model_urls_from_node(value, found, base_url)


def _extract_time_value(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    for key in (
        "favoritedAt",
        "favoriteTime",
        "favoritedTime",
        "collectionTime",
        "collectedAt",
        "collectedTime",
        "createTime",
        "createdAt",
        "updatedAt",
    ):
        value = node.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def extract_model_source_items(payload: dict, base_url: str, start_order: int = 0) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    hits = payload.get("hits") or []
    for hit in hits:
        hit_urls: list[str] = []
        hit_url_set: set[str] = set()
        raw_url_set: set[str] = set()
        _collect_model_urls_from_node(hit, raw_url_set, base_url)
        design_id = _extract_design_id_from_hit(hit)
        if design_id:
            raw_url_set.add(normalize_model_url(f"/zh/models/{design_id}", fallback_base=base_url))
        raw_urls = sorted(raw_url_set, key=lambda url: (0 if design_id and extract_model_id(url) == design_id else 1, url))
        for url in raw_urls:
            if url and url not in hit_url_set:
                hit_url_set.add(url)
                hit_urls.append(url)
        favorited_at = _extract_time_value(hit)
        for url in hit_urls:
            if not url or url in seen:
                continue
            seen.add(url)
            source_order = start_order + len(found)
            model_id = extract_model_id(url)
            item = {
                "url": url,
                "model_id": model_id,
                "task_key": f"model:{model_id}" if model_id else url,
                "source_order": source_order,
                "source_position": source_order,
            }
            if favorited_at:
                item["favorited_at"] = favorited_at
            found.append(item)
    return found


def _platform_origin(platform: str) -> str:
    return "https://makerworld.com" if str(platform or "").strip().lower() == "global" else "https://makerworld.com.cn"


def _makerworld_model_path_lang(platform: str) -> str:
    return "en" if str(platform or "").strip().lower() == "global" else "zh"


def extract_account_profile(payload: Any) -> dict[str, Any]:
    best: dict[str, Any] = {}
    best_score = -1
    for node in _iter_dicts(payload):
        uid = _extract_node_uid(node)
        handle = _extract_node_handle(node)
        name = str(node.get("name") or node.get("nickname") or node.get("nickName") or node.get("displayName") or "").strip()
        avatar = _extract_avatar_url(node)
        follow_count = _safe_positive_int(node.get("followCount"))
        liked_collection_count = _safe_positive_int(node.get("likeCount"))
        collection_count = _safe_positive_int(node.get("collectionCount"))
        lowered_keys = {str(key).lower() for key in node.keys()}
        profile_keys = {"name", "username", "handle", "userhandle", "user_handle", "nickname", "displayname", "avatar", "avatarurl", "avatar_url"}
        if not (handle or profile_keys & lowered_keys):
            continue
        score = 0
        if uid:
            score += 4
        if handle:
            score += 4
        if name:
            score += 2
        if avatar:
            score += 1
        if follow_count is not None:
            score += 2
        if liked_collection_count is not None:
            score += 2
        if collection_count is not None:
            score += 1
        if profile_keys & lowered_keys:
            score += 2
        if score > best_score:
            best_score = score
            best = {"uid": uid, "handle": handle, "name": name, "avatar_url": avatar}
            if follow_count is not None:
                best["follow_count"] = follow_count
            if liked_collection_count is not None:
                best["liked_collection_count"] = liked_collection_count
            if collection_count is not None:
                best["collection_count"] = collection_count
    return best


def extract_user_info_from_next_data(payload: Any) -> dict[str, Any]:
    best: dict[str, Any] = {}
    best_score = -1
    for node in _iter_dicts(payload):
        handle = _extract_node_handle(node)
        uid = _extract_node_uid(node)
        name = str(node.get("name") or node.get("nickname") or node.get("nickName") or node.get("displayName") or "").strip()
        avatar = _extract_avatar_url(node)
        if not (handle or uid or name or avatar):
            continue
        score = (8 if "userInfo" in node else 0) + (4 if uid else 0) + (4 if handle else 0) + (2 if name else 0) + (1 if avatar else 0)
        for key in ("followCount", "likeCount", "favoritesCount", "collectionCount"):
            if key in node:
                score += 2
        if score <= best_score:
            continue
        best_score = score
        best = {
            "uid": uid,
            "handle": handle,
            "name": name,
            "avatar_url": avatar,
            "follow_count": _safe_positive_int(node.get("followCount")),
            "liked_collection_count": _safe_positive_int(node.get("likeCount")),
            "collection_count": _safe_positive_int(node.get("collectionCount")),
        }
        favorites_count = node.get("favoritesCount")
        if isinstance(favorites_count, dict):
            best["favorites_public_count"] = _safe_positive_int(favorites_count.get("publicCount"))
            best["favorites_private_count"] = _safe_positive_int(favorites_count.get("privateCount"))
            best["favorites_liked_count"] = _safe_positive_int(favorites_count.get("likedCount"))
    return best


def _looks_like_author_follow_node(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    if not (_extract_node_handle(node) or _extract_node_uid(node)):
        return False
    if _extract_design_id_from_hit(node):
        return False
    if any(key in node for key in ("designId", "modelId", "coverLandscape", "coverUrl", "cover")):
        return False
    return any(key in node for key in ("uid", "userId", "name", "nickname", "avatar", "avatarUrl", "fanCount", "fansCount", "followCount", "followerCount", "publicInstanceUploadCount", "isFollowed"))


def extract_followed_authors(payload: Any, platform: str) -> list[dict[str, str]]:
    source_origin = _platform_origin(platform)
    lang = _makerworld_model_path_lang(platform)
    authors: list[dict[str, str]] = []
    seen: set[str] = set()
    candidate_nodes: list[dict[str, Any]] = []
    for node in _iter_dicts(payload):
        hits = node.get("hits")
        if isinstance(hits, list):
            candidate_nodes.extend(item for item in hits if isinstance(item, dict))
        for key in ("items", "list", "records", "users", "followers", "followings", "data"):
            values = node.get(key)
            if isinstance(values, list):
                candidate_nodes.extend(item for item in values if isinstance(item, dict))
    if not candidate_nodes:
        candidate_nodes = [node for node in _iter_dicts(payload) if isinstance(node, dict)]
    for node in candidate_nodes:
        if not _looks_like_author_follow_node(node):
            continue
        handle = _extract_node_handle(node)
        if not handle or handle.lower() in seen:
            continue
        seen.add(handle.lower())
        title = str(node.get("name") or node.get("nickname") or node.get("nickName") or handle).strip() or handle
        authors.append({
            "title": title,
            "handle": handle,
            "uid": _extract_node_uid(node),
            "avatar_url": _extract_avatar_url(node),
            "url": f"{source_origin}/{lang}/@{quote(handle, safe='@._-')}/upload",
        })
    return authors


def _extract_collection_entry_id(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    for key in ("collectionId", "favoriteId", "favoritesId", "listId", "id"):
        candidate = _coerce_numeric_string(node.get(key))
        if candidate:
            return candidate
    return ""


def _extract_collection_entry_count(node: dict) -> Optional[int]:
    for key in ("designCount", "designCnt", "modelCount", "modelsCount"):
        try:
            value = node.get(key)
            if value not in (None, ""):
                return max(int(value), 0)
        except Exception:
            continue
    designs = node.get("designs")
    return len(designs) if isinstance(designs, list) and designs else None


def _looks_like_collection_entry(node: Any, owner_uid: str) -> bool:
    if not isinstance(node, dict):
        return False
    collection_id = _extract_collection_entry_id(node)
    if not collection_id or collection_id == owner_uid:
        return False
    if node.get("designId") or node.get("modelId"):
        return False
    if any(key in node for key in ("downloadCount", "printCount", "commentCount", "collectionCount", "coverLandscape", "boostCnt", "bomsNeeded")):
        return False
    return any(key in node for key in ("designCount", "designCnt", "modelCount", "modelsCount", "collectionType", "privacy", "isDefault", "isPublic", "hiddenCnt", "hiddenIds", "designCover", "inCollection"))


def extract_collection_entries(payload: Any, owner_uid: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    candidate_nodes: list[dict[str, Any]] = []
    for node in _iter_dicts(payload):
        hits = node.get("hits")
        if isinstance(hits, list):
            candidate_nodes.extend(hit for hit in hits if isinstance(hit, dict))
    if not candidate_nodes:
        candidate_nodes = [node for node in _iter_dicts(payload) if isinstance(node, dict)]
    for node in candidate_nodes:
        if not _looks_like_collection_entry(node, owner_uid):
            continue
        collection_id = _extract_collection_entry_id(node)
        if collection_id in seen:
            continue
        seen.add(collection_id)
        entries.append({
            "id": collection_id,
            "name": str(node.get("name") or node.get("title") or "").strip(),
            "count": _extract_collection_entry_count(node),
        })
    return entries


def _collection_detail_url_from_entry(entry: dict[str, Any], platform: str) -> str:
    for key in ("url", "link", "href", "collectionUrl", "favoriteUrl"):
        candidate = str(entry.get(key) or "").strip()
        if candidate:
            parsed_url = urljoin(_platform_origin(platform), candidate)
            if COLLECTION_DETAIL_RE.search(urlparse(parsed_url).path or ""):
                return normalize_source_url(parsed_url)
    collection_id = str(entry.get("id") or entry.get("collectionId") or entry.get("favoriteId") or "").strip()
    if not collection_id:
        return ""
    slug = str(entry.get("slug") or entry.get("name") or entry.get("title") or "").strip()
    suffix = f"-{quote(slug, safe='')}" if slug else ""
    return f"{_platform_origin(platform)}/{_makerworld_model_path_lang(platform)}/collections/{collection_id}{suffix}"


def extract_followed_collections(payload: Any, platform: str) -> list[dict[str, Any]]:
    collections: list[dict[str, Any]] = []
    seen: set[str] = set()
    candidate_nodes: list[dict[str, Any]] = []
    for node in _iter_dicts(payload):
        hits = node.get("hits")
        if isinstance(hits, list):
            candidate_nodes.extend(item for item in hits if isinstance(item, dict))
        for key in ("items", "list", "records", "collections", "favorites", "data"):
            values = node.get(key)
            if isinstance(values, list):
                candidate_nodes.extend(item for item in values if isinstance(item, dict))
    if not candidate_nodes:
        candidate_nodes = [node for node in _iter_dicts(payload) if isinstance(node, dict)]
    for node in candidate_nodes:
        if not _looks_like_collection_entry(node, ""):
            continue
        url = _collection_detail_url_from_entry(node, platform)
        if not url or url in seen:
            continue
        seen.add(url)
        collections.append({
            "title": str(node.get("name") or node.get("title") or "关注收藏夹").strip() or "关注收藏夹",
            "id": _extract_collection_entry_id(node),
            "url": url,
            "count": _extract_collection_entry_count(node),
        })
    return collections


def extract_page_links(html_text: str, base_url: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    expanded = str(html_text or "").replace("\\/", "/").replace("\\u002F", "/")

    def add_link(raw_url: str) -> None:
        normalized = normalize_model_url(raw_url, fallback_base=base_url)
        if normalized and normalized not in seen:
            seen.add(normalized)
            found.append(normalized)

    for raw in MODEL_PATH_RE.findall(expanded):
        add_link(f"/zh/models/{raw}")
    soup = BeautifulSoup(expanded, "html.parser")
    for link in soup.find_all("a", href=True):
        add_link(link.get("href") or "")
    try:
        next_data = extract_next_data(expanded)
    except Exception:
        next_data = {}
    next_data_links: set[str] = set()
    _collect_model_urls_from_node(next_data, next_data_links, base_url)
    for link in sorted(next_data_links):
        add_link(link)
    return found
