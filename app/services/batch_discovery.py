import json
import re
import time
from typing import Any, Optional
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from app.core.settings import LOGS_DIR
from app.services.cookie_utils import extract_auth_token, sanitize_cookie_header
from app.services.legacy_archiver import (
    extract_next_data,
    fetch_design_from_api,
    fetch_html_with_curl,
    fetch_html_with_requests,
    parse_cookies,
)


MODEL_PATH_RE = re.compile(r"/(?:[a-z]{2}/)?models/(\d+)(?:[^\"'\\s<>]*)?", re.I)
AUTHOR_UPLOAD_RE = re.compile(r"/(?:[a-z]{2}/)?@([^/?#]+)/upload(?:[/?#]|$)", re.I)
AUTHOR_ROOT_RE = re.compile(r"^/(?:[a-z]{2}/)?@[^/?#]+/?$", re.I)
COLLECTION_DETAIL_RE = re.compile(r"/(?:[a-z]{2}/)?collections/(\d+)(?:-[^/?#]+)?(?:[/?#]|$)", re.I)

API_BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "X-BBL-Client-Type": "web",
    "X-BBL-Client-Version": "00.00.00.01",
    "X-BBL-App-Source": "makerworld",
    "X-BBL-Client-Name": "MakerWorld",
}

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)

AUTHOR_BATCH_PAGE_LIMIT = 100
COLLECTION_BATCH_PAGE_LIMIT = 20
HTML_BATCH_PAGE_LIMIT = 20
DISCOVERY_DEBUG_LOG = LOGS_DIR / "batch_discovery.log"


def normalize_source_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    if raw.startswith("/"):
        absolute = urljoin("https://makerworld.com.cn", raw)
    elif not raw.startswith("http://") and not raw.startswith("https://"):
        absolute = f"https://{raw.lstrip('/')}"
    else:
        absolute = raw

    parsed = urlparse(absolute)
    path = parsed.path or ""
    if AUTHOR_ROOT_RE.fullmatch(path):
        normalized_path = f"{path.rstrip('/')}/upload"
        return urlunparse(parsed._replace(path=normalized_path))

    return absolute


def _append_discovery_debug(event: str, **payload: Any) -> None:
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with DISCOVERY_DEBUG_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": event, **payload}, ensure_ascii=False) + "\n")
    except Exception:
        return


def normalize_model_url(url: str, fallback_base: str = "https://makerworld.com.cn") -> str:
    raw = str(url or "").strip().replace("\\/", "/").replace("\\u002F", "/")
    if not raw:
        return ""
    absolute = urljoin(fallback_base, raw)
    parsed = urlparse(absolute)
    match = MODEL_PATH_RE.search(parsed.path or "")
    if not match:
        return ""
    design_id = match.group(1)
    path = f"/zh/models/{design_id}"
    return urlunparse(("https", parsed.netloc or "makerworld.com.cn", path, "", "", ""))


def extract_model_id(url: str) -> str:
    normalized = normalize_model_url(url)
    match = MODEL_PATH_RE.search(urlparse(normalized).path or "")
    return match.group(1) if match else ""


def _fetch_listing_html(session: requests.Session, page_url: str, raw_cookie: str) -> str:
    html = fetch_html_with_requests(session, page_url, raw_cookie)
    if html:
        return html
    return fetch_html_with_curl(page_url, raw_cookie)


def _extract_auth_token(raw_cookie: str) -> str:
    return extract_auth_token(raw_cookie or "")


def _looks_like_html_response(text: str) -> bool:
    sample = str(text or "").lstrip()[:240].lower()
    if not sample:
        return False
    return (
        sample.startswith("<!doctype html")
        or sample.startswith("<html")
        or "<html" in sample
        or "cf-browser-verification" in sample
        or "cf-challenge" in sample
        or "__next" in sample
    )


def _origin_from_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return "https://makerworld.com.cn"


def _api_base_candidates(source_url: str) -> list[str]:
    parsed = urlparse(source_url)
    netloc = (parsed.netloc or "").lower()
    if "makerworld.com" in netloc and "makerworld.com.cn" not in netloc:
        preferred_site = "https://makerworld.com"
        preferred_api = "https://api.bambulab.com"
        fallback_candidates = []
    else:
        preferred_site = "https://makerworld.com.cn"
        preferred_api = "https://api.bambulab.cn"
        fallback_candidates = []

    bases: list[str] = []
    for candidate in (_origin_from_url(source_url), preferred_site, preferred_api, *fallback_candidates):
        if candidate and candidate not in bases:
            bases.append(candidate.rstrip("/"))
    return bases


def _service_endpoint_candidates(source_url: str, service_name: str, path: str) -> list[str]:
    clean_path = "/" + str(path or "").lstrip("/")
    endpoints: list[str] = []
    for base in _api_base_candidates(source_url):
        for prefix in (f"/api/v1/{service_name}", f"/v1/{service_name}"):
            api_url = f"{base}{prefix}{clean_path}"
            if api_url not in endpoints:
                endpoints.append(api_url)
    return endpoints


def _build_api_headers(session: requests.Session, raw_cookie: str, referer: str) -> dict[str, str]:
    cookie_header = sanitize_cookie_header(raw_cookie)
    headers = dict(API_BROWSER_HEADERS)
    headers["Referer"] = referer or "https://makerworld.com.cn/"
    headers["User-Agent"] = session.headers.get("User-Agent", BROWSER_USER_AGENT)
    headers["Origin"] = _origin_from_url(referer or "https://makerworld.com.cn/")
    if cookie_header:
        headers["Cookie"] = cookie_header
    return headers


def _api_get_json(
    session: requests.Session,
    source_url: str,
    raw_cookie: str,
    service_name: str,
    path: str,
    params: Optional[dict[str, Any]] = None,
) -> Optional[dict]:
    headers = _build_api_headers(session, raw_cookie, source_url)
    for api_url in _service_endpoint_candidates(source_url, service_name, path):
        started = time.time()
        try:
            response = session.get(api_url, params=params or None, headers=headers, timeout=(5, 12))
        except Exception as exc:
            _append_discovery_debug(
                "api_error",
                api_url=api_url,
                service=service_name,
                path=path,
                params=params or {},
                error=str(exc),
                elapsed_ms=round((time.time() - started) * 1000, 1),
            )
            continue
        if response.status_code >= 400:
            _append_discovery_debug(
                "api_response",
                api_url=api_url,
                service=service_name,
                path=path,
                params=params or {},
                status_code=response.status_code,
                elapsed_ms=round((time.time() - started) * 1000, 1),
            )
            continue
        text = response.text or ""
        if _looks_like_html_response(text):
            _append_discovery_debug(
                "api_html_response",
                api_url=api_url,
                service=service_name,
                path=path,
                params=params or {},
                status_code=response.status_code,
                elapsed_ms=round((time.time() - started) * 1000, 1),
            )
            continue
        try:
            payload = response.json()
        except Exception:
            _append_discovery_debug(
                "api_non_json",
                api_url=api_url,
                service=service_name,
                path=path,
                params=params or {},
                status_code=response.status_code,
                elapsed_ms=round((time.time() - started) * 1000, 1),
            )
            continue
        if isinstance(payload, dict):
            hits_payload = _extract_hits_payload(payload)
            payload_summary = _payload_debug_summary(payload) if "/favorites" in path else []
            _append_discovery_debug(
                "api_ok",
                api_url=api_url,
                service=service_name,
                path=path,
                params=params or {},
                status_code=response.status_code,
                elapsed_ms=round((time.time() - started) * 1000, 1),
                uid=_extract_uid(payload),
                hits=len((hits_payload or {}).get("hits") or []),
                total=(hits_payload or {}).get("total"),
                has_next=(hits_payload or {}).get("hasNext"),
                search_session_id=_extract_search_session_id(hits_payload or payload),
                payload_summary=payload_summary,
            )
            return payload
    return None


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


def _extract_uid(payload: Any) -> str:
    for node in _iter_dicts(payload):
        for key in ("uid", "userId", "ownerUid", "creatorUid", "authorUid"):
            candidate = _coerce_numeric_string(node.get(key))
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


def _extract_hits_payload(payload: Any) -> Optional[dict]:
    best_node: Optional[dict] = None
    best_score: tuple[int, int, int, int] = (-1, -1, -1, -1)
    for node in _iter_dicts(payload):
        hits = node.get("hits")
        if not isinstance(hits, list):
            continue
        total = _extract_total_count(node, len(hits))
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


def _extract_total_count(payload: dict, fallback: int) -> Optional[int]:
    for key in ("total", "count", "totalCount"):
        try:
            value = payload.get(key)
            if value in (None, ""):
                continue
            return max(int(value), 0)
        except Exception:
            continue
    return fallback if fallback >= 0 else None


def _extract_has_next(payload: dict) -> Optional[bool]:
    value = payload.get("hasNext")
    if isinstance(value, bool):
        return value
    return None


def _extract_search_session_id(payload: Any) -> str:
    preferred_keys = ("searchSessionId", "search_session_id", "sessionId", "session_id")
    fallback_keys = ("refreshId", "refresh_id")

    for key_group in (preferred_keys, fallback_keys):
        for node in _iter_dicts(payload):
            for key in key_group:
                value = node.get(key)
                if value in (None, ""):
                    continue
                candidate = str(value).strip()
                if candidate:
                    return candidate
    return ""


def _payload_debug_summary(payload: Any) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    seen: set[str] = set()

    for node in _iter_dicts(payload):
        if not isinstance(node.get("hits"), list):
            continue

        hits = node.get("hits") or []
        first_ids: list[str] = []
        first_keys: list[str] = []
        for hit in hits[:3]:
            if isinstance(hit, dict):
                design_id = _extract_design_id_from_hit(hit)
                if design_id:
                    first_ids.append(design_id)
                for key in sorted(hit.keys())[:8]:
                    if key not in first_keys:
                        first_keys.append(key)
            else:
                rendered = str(hit).strip()
                if rendered:
                    first_ids.append(rendered[:48])

        summary = {
            "keys": sorted(node.keys())[:12],
            "hits": len(hits),
            "total": _extract_total_count(node, len(hits)),
            "has_next": _extract_has_next(node),
            "design_like_hits": sum(1 for hit in hits[:8] if _looks_like_design_hit(hit)),
            "search_session_id": _extract_search_session_id(node),
            "first_ids": first_ids[:6],
            "first_hit_keys": first_keys[:12],
        }
        key = json.dumps(summary, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        summaries.append(summary)
        if len(summaries) >= 8:
            break

    return summaries


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


def _extract_model_urls_from_hits(payload: dict, base_url: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    hits = payload.get("hits") or []
    for hit in hits:
        hit_urls: set[str] = set()
        _collect_model_urls_from_node(hit, hit_urls, base_url)
        design_id = _extract_design_id_from_hit(hit)
        if design_id:
            hit_urls.add(normalize_model_url(f"/zh/models/{design_id}", fallback_base=base_url))
        for url in hit_urls:
            if not url or url in seen:
                continue
            seen.add(url)
            found.append(url)
    return found


def _author_published_param_candidates(offset: int, limit: int) -> list[dict[str, int]]:
    page_index = max((offset // max(limit, 1)) + 1, 1)
    candidates = [
        {"offset": offset, "limit": limit},
        {"page": page_index, "limit": limit},
        {"pageNum": page_index, "limit": limit},
        {"pageNo": page_index, "limit": limit},
        {"current": page_index, "size": limit},
        {"offset": offset, "page": page_index, "limit": limit},
    ]
    deduped: list[dict[str, int]] = []
    seen: set[tuple[tuple[str, int], ...]] = set()
    for candidate in candidates:
        key = tuple(sorted(candidate.items()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _fetch_author_hits_payload(
    session: requests.Session,
    source_url: str,
    raw_cookie: str,
    uid: str,
    offset: int,
    limit: int,
    seen_links: set[str],
) -> tuple[Optional[dict], Optional[dict], list[str]]:
    first_payload: Optional[dict] = None
    first_hits_payload: Optional[dict] = None
    first_page_links: list[str] = []

    for params in _author_published_param_candidates(offset, limit):
        payload = _api_get_json(
            session,
            source_url=source_url,
            raw_cookie=raw_cookie,
            service_name="design-service",
            path=f"/published/{uid}/design",
            params=params,
        )
        if payload is None:
            continue

        hits_payload = _extract_hits_payload(payload)
        if hits_payload is None:
            continue

        page_links = _extract_model_urls_from_hits(hits_payload, source_url)
        if first_payload is None:
            first_payload = payload
            first_hits_payload = hits_payload
            first_page_links = page_links

        if offset <= 0:
            _append_discovery_debug(
                "author_page_selected",
                offset=offset,
                limit=limit,
                params=params,
                page_links=len(page_links),
                new_links=len(page_links),
            )
            return payload, hits_payload, page_links

        new_links = [link for link in page_links if link not in seen_links]
        if new_links:
            _append_discovery_debug(
                "author_page_selected",
                offset=offset,
                limit=limit,
                params=params,
                page_links=len(page_links),
                new_links=len(new_links),
            )
            return payload, hits_payload, page_links

        _append_discovery_debug(
            "author_page_duplicate",
            offset=offset,
            limit=limit,
            params=params,
            page_links=len(page_links),
        )

    return first_payload, first_hits_payload, first_page_links


def _extract_author_handle(source_url: str) -> str:
    match = AUTHOR_UPLOAD_RE.search(urlparse(source_url).path or "")
    if not match:
        return ""
    return match.group(1).strip("@").strip()


def _is_collection_models_url(source_url: str) -> bool:
    path = (urlparse(source_url).path or "").lower()
    return "/collections/models" in path or COLLECTION_DETAIL_RE.search(path) is not None


def _is_collection_detail_url(source_url: str) -> bool:
    return COLLECTION_DETAIL_RE.search(urlparse(source_url).path or "") is not None


def _extract_collection_id(source_url: str) -> str:
    match = COLLECTION_DETAIL_RE.search(urlparse(source_url).path or "")
    if not match:
        return ""
    return match.group(1).strip()


def _extract_collection_handle(source_url: str) -> str:
    if not _is_collection_models_url(source_url) or _is_collection_detail_url(source_url):
        return ""
    return _extract_handle_from_url(source_url)


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


def _extract_author_handles_from_design(design: Any) -> set[str]:
    handles: set[str] = set()
    nodes: list[Any] = [design]
    if isinstance(design, dict):
        nodes.extend(
            [
                design.get("designCreator"),
                design.get("user"),
                design.get("author"),
                design.get("creator"),
            ]
        )

    for node in nodes:
        if not isinstance(node, dict):
            continue
        for key in ("username", "userName", "slug", "handle", "creatorUsername"):
            candidate = str(node.get(key) or "").strip().lstrip("@")
            if candidate:
                handles.add(candidate.lower())
        for key in ("url", "homepage", "profileUrl", "authorUrl"):
            candidate = _extract_handle_from_url(str(node.get(key) or ""))
            if candidate:
                handles.add(candidate.lower())
    return handles


def _normalize_handle_value(value: Any) -> str:
    return str(value or "").strip().lstrip("@").strip().lower()


def _node_matches_handle(node: Any, handle: str) -> bool:
    if not isinstance(node, dict):
        return False

    expected_handle = _normalize_handle_value(handle)
    if not expected_handle:
        return False

    for key in ("username", "userName", "slug", "handle", "userHandle", "user_handle", "creatorUsername"):
        if _normalize_handle_value(node.get(key)) == expected_handle:
            return True

    for key in ("url", "homepage", "profileUrl", "authorUrl", "link", "href"):
        candidate = _extract_handle_from_url(str(node.get(key) or ""))
        if _normalize_handle_value(candidate) == expected_handle:
            return True

    return False


def _collect_uid_votes_for_handle(payload: Any, handle: str) -> dict[str, int]:
    votes: dict[str, int] = {}
    for node in _iter_dicts(payload):
        if not _node_matches_handle(node, handle):
            continue
        uid = _extract_uid(node)
        if not uid:
            continue
        votes[uid] = votes.get(uid, 0) + 1
    return votes


def _resolve_uid_from_listing_html(
    session: requests.Session,
    source_url: str,
    raw_cookie: str,
    handle: str,
    event_prefix: str,
) -> str:
    html_text = _fetch_listing_html(session, source_url, raw_cookie)
    if not html_text:
        _append_discovery_debug(f"{event_prefix}_html_missing", handle=handle, source_url=source_url)
        return ""

    try:
        next_data = extract_next_data(html_text)
    except Exception as exc:
        _append_discovery_debug(
            f"{event_prefix}_next_data_missing",
            handle=handle,
            source_url=source_url,
            error=str(exc),
        )
        return ""

    if _is_collection_models_url(source_url):
        _append_discovery_debug(
            f"{event_prefix}_next_data_snapshot",
            handle=handle,
            source_url=source_url,
            snapshots=_summarize_collection_next_data(next_data, handle),
        )

    votes = _collect_uid_votes_for_handle(next_data, handle)
    if not votes:
        _append_discovery_debug(
            f"{event_prefix}_next_data_no_match",
            handle=handle,
            source_url=source_url,
        )
        return ""

    uid = max(votes.items(), key=lambda item: item[1])[0]
    _append_discovery_debug(
        f"{event_prefix}_resolved",
        handle=handle,
        uid=uid,
        mode="next_data",
        votes=votes,
    )
    return uid


def _collect_handle_variants_from_node(node: dict) -> list[str]:
    values: list[str] = []
    for key in ("username", "userName", "slug", "handle", "userHandle", "user_handle", "creatorUsername", "name"):
        candidate = str(node.get(key) or "").strip()
        if candidate:
            values.append(candidate)
    for key in ("url", "homepage", "profileUrl", "authorUrl", "link", "href"):
        candidate = _extract_handle_from_url(str(node.get(key) or ""))
        if candidate:
            values.append(candidate)
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip().lstrip("@")
        lowered = normalized.lower()
        if not lowered or lowered in seen:
            continue
        seen.add(lowered)
        result.append(normalized)
    return result


def _summarize_collection_next_data(next_data: Any, handle: str) -> list[dict[str, Any]]:
    expected_handle = _normalize_handle_value(handle)
    snapshots: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in _iter_dicts(next_data):
        user_node = node.get("userInfo") if isinstance(node.get("userInfo"), dict) else None
        designs_node = node.get("designsData") if isinstance(node.get("designsData"), dict) else None
        counts_node = node.get("collectionsCounts") if isinstance(node.get("collectionsCounts"), dict) else None
        if not any((user_node, designs_node, counts_node)):
            continue

        candidate_node = user_node or node
        handles = _collect_handle_variants_from_node(candidate_node) if isinstance(candidate_node, dict) else []
        uid = _extract_uid(candidate_node) if isinstance(candidate_node, dict) else ""
        if expected_handle and handles and expected_handle not in {item.lower() for item in handles}:
            continue

        design_hits = (designs_node or {}).get("hits") or []
        design_ids = []
        for hit in design_hits[:6]:
            design_id = _extract_design_id_from_hit(hit)
            if design_id:
                design_ids.append(design_id)

        summary = {
            "uid": uid,
            "handles": handles[:6],
            "designs_total": _extract_total_count(designs_node or {}, len(design_hits)) if designs_node else None,
            "designs_hits": len(design_hits),
            "design_ids": design_ids,
            "collections_counts": dict(counts_node or {}),
        }
        key = json.dumps(summary, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        snapshots.append(summary)
        if len(snapshots) >= 6:
            break
    return snapshots


def _resolve_uid_by_handle_api(
    session: requests.Session,
    source_url: str,
    raw_cookie: str,
    handle: str,
    event_prefix: str,
) -> str:
    normalized_handle = str(handle or "").strip("@").strip()
    if not normalized_handle:
        return ""

    profile_payload = _api_get_json(
        session,
        source_url=source_url,
        raw_cookie=raw_cookie,
        service_name="design-service",
        path=f"/profile/{quote(normalized_handle, safe='@._-')}",
    )
    uid = _extract_uid(profile_payload)
    if uid:
        _append_discovery_debug(f"{event_prefix}_resolved", handle=normalized_handle, uid=uid, mode="profile")
        return uid

    handle_variants = []
    for candidate in (normalized_handle, f"@{normalized_handle}"):
        if candidate and candidate not in handle_variants:
            handle_variants.append(candidate)

    for param_name in ("handle", "userHandle", "user_handle", "userName", "username", "slug"):
        for handle_value in handle_variants:
            payload = _api_get_json(
                session,
                source_url=source_url,
                raw_cookie=raw_cookie,
                service_name="user-service",
                path="/user/uid",
                params={param_name: handle_value},
            )
            uid = _extract_uid(payload)
            if uid:
                _append_discovery_debug(f"{event_prefix}_resolved", handle=normalized_handle, uid=uid, mode=param_name)
                return uid

    return ""


def _resolve_author_uid_from_sample_models(
    session: requests.Session,
    source_url: str,
    raw_cookie: str,
    handle: str,
) -> str:
    html_text = _fetch_listing_html(session, source_url, raw_cookie)
    page_links = _extract_page_links(html_text, source_url)
    if not page_links:
        _append_discovery_debug("author_uid_sample_no_links", handle=handle)
        return ""

    expected_handle = str(handle or "").strip().lstrip("@").lower()
    votes: dict[str, int] = {}

    for model_url in page_links[:8]:
        design = fetch_design_from_api(
            session,
            raw_cookie,
            model_url,
            api_host_hint=_origin_from_url(source_url),
        )
        if not isinstance(design, dict):
            _append_discovery_debug(
                "author_uid_sample_design_missing",
                handle=handle,
                model_url=model_url,
            )
            continue

        uid = _extract_uid(design)
        handles = sorted(_extract_author_handles_from_design(design))
        _append_discovery_debug(
            "author_uid_sample_model",
            handle=handle,
            model_url=model_url,
            uid=uid,
            author_handles=handles,
        )
        if not uid:
            continue
        votes[uid] = votes.get(uid, 0) + 1
        if expected_handle and expected_handle in handles:
            _append_discovery_debug("author_uid_resolved", handle=handle, uid=uid, mode="sample_model")
            return uid

    if votes:
        uid = max(votes.items(), key=lambda item: item[1])[0]
        _append_discovery_debug(
            "author_uid_resolved",
            handle=handle,
            uid=uid,
            mode="sample_model_vote",
            votes=votes,
        )
        return uid

    return ""


def _resolve_author_uid(session: requests.Session, source_url: str, raw_cookie: str, handle: str) -> str:
    uid = _resolve_uid_by_handle_api(
        session,
        source_url=source_url,
        raw_cookie=raw_cookie,
        handle=handle,
        event_prefix="author_uid",
    )
    if uid:
        return uid

    uid = _resolve_uid_from_listing_html(
        session,
        source_url=source_url,
        raw_cookie=raw_cookie,
        handle=handle,
        event_prefix="author_uid",
    )
    if uid:
        return uid

    uid = _resolve_author_uid_from_sample_models(session, source_url, raw_cookie, handle)
    if uid:
        return uid

    _append_discovery_debug("author_uid_missing", handle=handle)
    return ""


def _resolve_collection_owner_uid(
    session: requests.Session,
    source_url: str,
    raw_cookie: str,
    handle: str,
) -> str:
    uid = _resolve_uid_by_handle_api(
        session,
        source_url=source_url,
        raw_cookie=raw_cookie,
        handle=handle,
        event_prefix="collection_owner_uid",
    )
    if uid:
        return uid

    uid = _resolve_uid_from_listing_html(
        session,
        source_url=source_url,
        raw_cookie=raw_cookie,
        handle=handle,
        event_prefix="collection_owner_uid",
    )
    if uid:
        return uid

    _append_discovery_debug("collection_owner_uid_missing", handle=handle, source_url=source_url)
    return ""


def _discover_author_upload_api(
    session: requests.Session,
    source_url: str,
    raw_cookie: str,
    max_pages: int,
    limit: int = AUTHOR_BATCH_PAGE_LIMIT,
) -> Optional[dict]:
    handle = _extract_author_handle(source_url)
    if not handle:
        return None

    uid = _resolve_author_uid(session, source_url, raw_cookie, handle)
    if not uid:
        return None

    _append_discovery_debug("author_discovery_start", source_url=source_url, handle=handle, uid=uid)

    discovered: list[str] = []
    seen: set[str] = set()
    pages_scanned = 0
    offset = 0
    expected_total: Optional[int] = None

    for _ in range(max_pages):
        payload, hits_payload, page_links = _fetch_author_hits_payload(
            session,
            source_url=source_url,
            raw_cookie=raw_cookie,
            uid=uid,
            offset=offset,
            limit=limit,
            seen_links=seen,
        )
        if payload is None:
            return None if pages_scanned == 0 else {
                "source_url": source_url,
                "pages_scanned": pages_scanned,
                "items": discovered,
                "mode": "author_upload_api_partial",
                "expected_total": expected_total,
            }

        if hits_payload is None:
            return None if pages_scanned == 0 else {
                "source_url": source_url,
                "pages_scanned": pages_scanned,
                "items": discovered,
                "mode": "author_upload_api_partial",
                "expected_total": expected_total,
            }

        hits = hits_payload.get("hits") or []
        pages_scanned += 1
        expected_total = _extract_total_count(hits_payload, len(hits))

        new_link_count = 0
        for link in page_links:
            if link in seen:
                continue
            seen.add(link)
            discovered.append(link)
            new_link_count += 1

        if not hits:
            break

        has_next = _extract_has_next(hits_payload)
        if has_next is False:
            break
        if expected_total is not None and len(discovered) >= expected_total:
            break
        if new_link_count == 0 and offset > 0:
            break
        if len(hits) < limit:
            break

        offset += max(len(hits), 1)

    return {
        "source_url": source_url,
        "pages_scanned": pages_scanned,
        "items": discovered,
        "mode": "author_upload_api",
        "expected_total": expected_total,
    }


def _collection_route_query_params(
    source_url: str,
    handle: str,
    *,
    include_handle: bool = False,
    extra_params: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    base_params: dict[str, Any] = {}
    normalized_handle = str(handle or "").strip().lstrip("@")
    if include_handle and normalized_handle:
        base_params["handle"] = normalized_handle

    query_params = dict(parse_qsl(urlparse(source_url).query, keep_blank_values=True))
    for key in ("query", "q", "keyword", "order", "status"):
        value = str(query_params.get(key) or "").strip()
        if value:
            base_params[key] = value
    for key, value in (extra_params or {}).items():
        if value in (None, ""):
            continue
        base_params[key] = value
    return base_params


def _collection_designs_param_candidates(
    offset: int,
    limit: int,
    source_url: str,
    handle: str,
    *,
    search_session_id: str = "",
) -> list[dict[str, Any]]:
    page_index = max((offset // max(limit, 1)) + 1, 1)
    extra_params = {"searchSessionId": search_session_id} if search_session_id else None
    base_params = _collection_route_query_params(
        source_url,
        handle,
        include_handle=False,
        extra_params=extra_params,
    )
    candidates = [
        {**base_params, "offset": offset, "limit": limit},
        {**base_params, "page": page_index, "limit": limit},
        {**base_params, "pageNum": page_index, "limit": limit},
        {**base_params, "pageNo": page_index, "limit": limit},
        {**base_params, "current": page_index, "size": limit},
        {**base_params, "offset": offset, "page": page_index, "limit": limit},
    ]
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, Any], ...]] = set()
    for candidate in candidates:
        key = tuple(sorted(candidate.items()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _collection_designs_path_candidates(uid: str) -> list[str]:
    candidates = [
        f"/favorites/designs/{uid}",
        f"/favorites/{uid}/designs",
    ]
    seen: set[str] = set()
    result: list[str] = []
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        result.append(path)
    return result


def _collection_list_path_candidates(uid: str) -> list[str]:
    candidates = [
        f"/favorites/v2/list/{uid}",
        f"/favoriteslist/{uid}",
    ]
    seen: set[str] = set()
    result: list[str] = []
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        result.append(path)
    return result


def _collection_list_param_candidates(source_url: str, handle: str) -> list[dict[str, Any]]:
    base_params = _collection_route_query_params(source_url, handle, include_handle=False)
    candidates = [
        base_params,
        {**base_params, "offset": 0, "limit": 50},
        {**base_params, "page": 1, "limit": 50},
        {**base_params, "pageNum": 1, "limit": 50},
        {**base_params, "pageNo": 1, "limit": 50},
        {**base_params, "current": 1, "size": 50},
    ]
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, Any], ...]] = set()
    for candidate in candidates:
        key = tuple(sorted(candidate.items()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _extract_collection_entry_id(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    for key in ("collectionId", "favoriteId", "favoritesId", "listId", "id"):
        candidate = _coerce_numeric_string(node.get(key))
        if candidate:
            return candidate
    return ""


def _extract_collection_entries(payload: Any, owner_uid: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    for node in _iter_dicts(payload):
        collection_id = _extract_collection_entry_id(node)
        if not collection_id or collection_id == owner_uid or collection_id in seen:
            continue
        if any(
            key in node
            for key in ("downloadCount", "printCount", "commentCount", "likeCount", "coverUrl", "designCreator", "user")
        ):
            continue
        if not any(
            key in node
            for key in (
                "designCount",
                "designCnt",
                "modelCount",
                "modelsCount",
                "collectionType",
                "privacy",
                "isDefault",
                "isPublic",
                "name",
                "title",
            )
        ):
            continue

        seen.add(collection_id)
        entries.append(
            {
                "id": collection_id,
                "name": str(node.get("name") or node.get("title") or "").strip(),
                "count": _extract_total_count(node, -1),
            }
        )
    return entries


def _fetch_collection_list_entries(
    session: requests.Session,
    source_url: str,
    raw_cookie: str,
    handle: str,
    owner_uid: str,
) -> list[dict[str, Any]]:
    for path in _collection_list_path_candidates(owner_uid):
        for params in _collection_list_param_candidates(source_url, handle):
            payload = _api_get_json(
                session,
                source_url=source_url,
                raw_cookie=raw_cookie,
                service_name="design-service",
                path=path,
                params=params or None,
            )
            if payload is None:
                continue

            entries = _extract_collection_entries(payload, owner_uid)
            _append_discovery_debug(
                "collection_list_probe",
                path=path,
                params=params,
                entry_count=len(entries),
                entries=entries[:10],
            )
            if entries:
                return entries
    return []


def _discover_collection_models_by_lists(
    session: requests.Session,
    source_url: str,
    raw_cookie: str,
    handle: str,
    owner_uid: str,
    max_pages: int,
    limit: int = COLLECTION_BATCH_PAGE_LIMIT,
) -> Optional[dict]:
    entries = _fetch_collection_list_entries(
        session,
        source_url=source_url,
        raw_cookie=raw_cookie,
        handle=handle,
        owner_uid=owner_uid,
    )
    if not entries:
        return None

    discovered: list[str] = []
    seen_links: set[str] = set()
    pages_scanned = 0
    collections_scanned = 0
    expected_total = sum(max(int(entry.get("count") or 0), 0) for entry in entries)
    total_page_budget = max(max_pages, 1)
    if expected_total > 0:
        projected_pages = sum(
            max((max(int(entry.get("count") or 0), 0) + max(limit, 1) - 1) // max(limit, 1), 1)
            for entry in entries
        )
        total_page_budget = min(max(total_page_budget, projected_pages + 2), 240)

    _append_discovery_debug(
        "collection_list_discovery_start",
        source_url=source_url,
        handle=handle,
        owner_uid=owner_uid,
        entry_count=len(entries),
        expected_total=expected_total,
        total_page_budget=total_page_budget,
        entries=entries[:10],
    )

    for entry in entries:
        collection_id = str(entry.get("id") or "").strip()
        if not collection_id:
            continue

        collections_scanned += 1
        offset = 0
        collection_expected = max(int(entry.get("count") or 0), 0)
        collection_page_budget = max(1, max_pages)
        if collection_expected > 0:
            collection_page_budget = min(
                max(collection_page_budget, (collection_expected + max(limit, 1) - 1) // max(limit, 1) + 1),
                total_page_budget,
            )
        collection_pages_scanned = 0
        collection_discovered_before = len(discovered)

        while pages_scanned < total_page_budget and collection_pages_scanned < collection_page_budget:
            payload = None
            hits_payload = None
            page_links: list[str] = []

            for params in _collection_designs_param_candidates(offset, limit, source_url, handle):
                payload = _api_get_json(
                    session,
                    source_url=source_url,
                    raw_cookie=raw_cookie,
                    service_name="design-service",
                    path=f"/favorites/{collection_id}/designs",
                    params=params,
                )
                if payload is None:
                    continue
                hits_payload = _extract_hits_payload(payload)
                if hits_payload is None:
                    continue
                page_links = _extract_model_urls_from_hits(hits_payload, source_url)
                _append_discovery_debug(
                    "collection_list_page_probe",
                    collection_id=collection_id,
                    offset=offset,
                    limit=limit,
                    params=params,
                    page_links=len(page_links),
                    hits=len(hits_payload.get("hits") or []),
                    total=hits_payload.get("total"),
                )
                break

            if payload is None or hits_payload is None:
                break

            hits = hits_payload.get("hits") or []
            pages_scanned += 1
            collection_pages_scanned += 1
            new_links = 0
            for link in page_links:
                if link in seen_links:
                    continue
                seen_links.add(link)
                discovered.append(link)
                new_links += 1

            total = _extract_total_count(hits_payload, len(hits))
            has_next = _extract_has_next(hits_payload)
            if not hits:
                break
            if has_next is False:
                break
            if collection_expected > 0 and (len(discovered) - collection_discovered_before) >= collection_expected:
                break
            if total is not None and total >= 0 and new_links == 0 and offset > 0:
                break
            if new_links == 0 and offset > 0:
                break
            if len(hits) < limit and has_next is not True:
                break

            offset += max(len(hits), 1)

    if not discovered:
        return None

    return {
        "source_url": source_url,
        "pages_scanned": pages_scanned,
        "collections_scanned": collections_scanned,
        "items": discovered,
        "mode": "collection_models_lists",
        "expected_total": expected_total if expected_total > 0 else len(discovered),
    }


def _fetch_collection_hits_payload(
    session: requests.Session,
    source_url: str,
    raw_cookie: str,
    handle: str,
    uid: str,
    offset: int,
    limit: int,
    seen_links: set[str],
    search_session_id: str,
) -> tuple[Optional[dict], Optional[dict], list[str], str]:
    first_payload: Optional[dict] = None
    first_hits_payload: Optional[dict] = None
    first_page_links: list[str] = []
    first_path = ""

    for path in _collection_designs_path_candidates(uid):
        for params in _collection_designs_param_candidates(
            offset,
            limit,
            source_url,
            handle,
            search_session_id=search_session_id,
        ):
            payload = _api_get_json(
                session,
                source_url=source_url,
                raw_cookie=raw_cookie,
                service_name="design-service",
                path=path,
                params=params,
            )
            if payload is None:
                continue

            hits_payload = _extract_hits_payload(payload)
            if hits_payload is None:
                continue

            page_links = _extract_model_urls_from_hits(hits_payload, source_url)
            if first_payload is None:
                first_payload = payload
                first_hits_payload = hits_payload
                first_page_links = page_links
                first_path = path

            if offset <= 0 and page_links:
                _append_discovery_debug(
                    "collection_page_selected",
                    path=path,
                    offset=offset,
                    limit=limit,
                    params=params,
                    page_links=len(page_links),
                    new_links=len(page_links),
                )
                return payload, hits_payload, page_links, path

            new_links = [link for link in page_links if link not in seen_links]
            if new_links:
                _append_discovery_debug(
                    "collection_page_selected",
                    path=path,
                    offset=offset,
                    limit=limit,
                    params=params,
                    page_links=len(page_links),
                    new_links=len(new_links),
                )
                return payload, hits_payload, page_links, path

            _append_discovery_debug(
                "collection_page_duplicate",
                path=path,
                offset=offset,
                limit=limit,
                params=params,
                page_links=len(page_links),
            )

    return first_payload, first_hits_payload, first_page_links, first_path


def _discover_collection_models_api(
    session: requests.Session,
    source_url: str,
    raw_cookie: str,
    max_pages: int,
    limit: int = COLLECTION_BATCH_PAGE_LIMIT,
) -> Optional[dict]:
    handle = _extract_collection_handle(source_url)
    if not handle:
        return None

    uid = _resolve_collection_owner_uid(session, source_url, raw_cookie, handle)
    if not uid:
        return None

    _append_discovery_debug("collection_discovery_start", source_url=source_url, handle=handle, uid=uid)

    discovered: list[str] = []
    seen: set[str] = set()
    pages_scanned = 0
    offset = 0
    expected_total: Optional[int] = None
    page_budget = max(max_pages, 1)
    selected_path = ""
    search_session_id = ""

    while pages_scanned < page_budget:
        payload, hits_payload, page_links, selected_path = _fetch_collection_hits_payload(
            session,
            source_url=source_url,
            raw_cookie=raw_cookie,
            handle=handle,
            uid=uid,
            offset=offset,
            limit=limit,
            seen_links=seen,
            search_session_id=search_session_id,
        )
        if payload is None:
            return None if pages_scanned == 0 else {
                "source_url": source_url,
                "pages_scanned": pages_scanned,
                "items": discovered,
                "mode": "collection_models_api_partial",
                "expected_total": expected_total,
            }

        if hits_payload is None:
            return None if pages_scanned == 0 else {
                "source_url": source_url,
                "pages_scanned": pages_scanned,
                "items": discovered,
                "mode": "collection_models_api_partial",
                "expected_total": expected_total,
            }

        hits = hits_payload.get("hits") or []
        pages_scanned += 1
        discovered_session_id = _extract_search_session_id(hits_payload) or _extract_search_session_id(payload)
        if discovered_session_id:
            search_session_id = discovered_session_id
        expected_total = _extract_total_count(hits_payload, len(hits))
        page_size = max(len(hits), 1)
        if expected_total:
            estimated_pages = max((expected_total + page_size - 1) // page_size, 1) + 1
            page_budget = min(max(page_budget, estimated_pages), 120)

        new_link_count = 0
        for link in page_links:
            if link in seen:
                continue
            seen.add(link)
            discovered.append(link)
            new_link_count += 1

        if not hits:
            break

        has_next = _extract_has_next(hits_payload)
        if has_next is False:
            break
        if expected_total is not None and len(discovered) >= expected_total:
            break
        if new_link_count == 0 and offset > 0:
            break
        if len(hits) < limit and has_next is not True:
            break

        offset += page_size

    if not discovered:
        _append_discovery_debug(
            "collection_api_empty_fallback",
            source_url=source_url,
            handle=handle,
            uid=uid,
            path=selected_path,
            pages_scanned=pages_scanned,
            expected_total=expected_total,
        )
        return None

    return {
        "source_url": source_url,
        "pages_scanned": pages_scanned,
        "items": discovered,
        "mode": "collection_models_api",
        "path": selected_path,
        "expected_total": expected_total,
    }


def _fetch_collection_detail_hits_payload(
    session: requests.Session,
    source_url: str,
    raw_cookie: str,
    collection_id: str,
    offset: int,
    limit: int,
    seen_links: set[str],
    search_session_id: str,
) -> tuple[Optional[dict], Optional[dict], list[str], str]:
    first_payload: Optional[dict] = None
    first_hits_payload: Optional[dict] = None
    first_page_links: list[str] = []
    path = f"/favorites/{collection_id}/designs"

    for params in _collection_designs_param_candidates(
        offset,
        limit,
        source_url,
        "",
        search_session_id=search_session_id,
    ):
        payload = _api_get_json(
            session,
            source_url=source_url,
            raw_cookie=raw_cookie,
            service_name="design-service",
            path=path,
            params=params,
        )
        if payload is None:
            continue

        hits_payload = _extract_hits_payload(payload)
        if hits_payload is None:
            continue

        page_links = _extract_model_urls_from_hits(hits_payload, source_url)
        if first_payload is None:
            first_payload = payload
            first_hits_payload = hits_payload
            first_page_links = page_links

        if offset <= 0 and page_links:
            _append_discovery_debug(
                "collection_detail_page_selected",
                collection_id=collection_id,
                offset=offset,
                limit=limit,
                params=params,
                page_links=len(page_links),
                new_links=len(page_links),
            )
            return payload, hits_payload, page_links, path

        new_links = [link for link in page_links if link not in seen_links]
        if new_links:
            _append_discovery_debug(
                "collection_detail_page_selected",
                collection_id=collection_id,
                offset=offset,
                limit=limit,
                params=params,
                page_links=len(page_links),
                new_links=len(new_links),
            )
            return payload, hits_payload, page_links, path

        _append_discovery_debug(
            "collection_detail_page_duplicate",
            collection_id=collection_id,
            offset=offset,
            limit=limit,
            params=params,
            page_links=len(page_links),
        )

    return first_payload, first_hits_payload, first_page_links, path


def _discover_collection_detail_models_api(
    session: requests.Session,
    source_url: str,
    raw_cookie: str,
    max_pages: int,
    limit: int = COLLECTION_BATCH_PAGE_LIMIT,
) -> Optional[dict]:
    collection_id = _extract_collection_id(source_url)
    if not collection_id:
        return None

    _append_discovery_debug("collection_detail_discovery_start", source_url=source_url, collection_id=collection_id)

    discovered: list[str] = []
    seen: set[str] = set()
    pages_scanned = 0
    offset = 0
    expected_total: Optional[int] = None
    page_budget = max(max_pages, 1)
    search_session_id = ""
    selected_path = ""

    while pages_scanned < page_budget:
        payload, hits_payload, page_links, selected_path = _fetch_collection_detail_hits_payload(
            session,
            source_url=source_url,
            raw_cookie=raw_cookie,
            collection_id=collection_id,
            offset=offset,
            limit=limit,
            seen_links=seen,
            search_session_id=search_session_id,
        )
        if payload is None:
            return None if pages_scanned == 0 else {
                "source_url": source_url,
                "pages_scanned": pages_scanned,
                "items": discovered,
                "mode": "collection_detail_api_partial",
                "expected_total": expected_total,
                "collection_id": collection_id,
            }

        if hits_payload is None:
            return None if pages_scanned == 0 else {
                "source_url": source_url,
                "pages_scanned": pages_scanned,
                "items": discovered,
                "mode": "collection_detail_api_partial",
                "expected_total": expected_total,
                "collection_id": collection_id,
            }

        hits = hits_payload.get("hits") or []
        pages_scanned += 1
        discovered_session_id = _extract_search_session_id(hits_payload) or _extract_search_session_id(payload)
        if discovered_session_id:
            search_session_id = discovered_session_id
        expected_total = _extract_total_count(hits_payload, len(hits))
        page_size = max(len(hits), 1)
        if expected_total:
            estimated_pages = max((expected_total + page_size - 1) // page_size, 1) + 1
            page_budget = min(max(page_budget, estimated_pages), 120)

        new_link_count = 0
        for link in page_links:
            if link in seen:
                continue
            seen.add(link)
            discovered.append(link)
            new_link_count += 1

        if not hits:
            break

        has_next = _extract_has_next(hits_payload)
        if has_next is False:
            break
        if expected_total is not None and len(discovered) >= expected_total:
            break
        if new_link_count == 0 and offset > 0:
            break
        if len(hits) < limit and has_next is not True:
            break

        offset += page_size

    if not discovered:
        _append_discovery_debug(
            "collection_detail_api_empty",
            source_url=source_url,
            collection_id=collection_id,
            path=selected_path,
            pages_scanned=pages_scanned,
            expected_total=expected_total,
        )
        return None

    return {
        "source_url": source_url,
        "pages_scanned": pages_scanned,
        "items": discovered,
        "mode": "collection_detail_api",
        "path": selected_path,
        "expected_total": expected_total,
        "collection_id": collection_id,
    }


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


def _extract_page_links(html_text: str, base_url: str) -> list[str]:
    found: set[str] = set()
    expanded = str(html_text or "").replace("\\/", "/").replace("\\u002F", "/")

    for raw in MODEL_PATH_RE.findall(expanded):
        found.add(normalize_model_url(f"/zh/models/{raw}", fallback_base=base_url))

    soup = BeautifulSoup(expanded, "html.parser")
    for link in soup.find_all("a", href=True):
        normalized = normalize_model_url(link.get("href") or "", fallback_base=base_url)
        if normalized:
            found.add(normalized)

    try:
        next_data = extract_next_data(expanded)
    except Exception:
        next_data = {}
    _collect_model_urls_from_node(next_data, found, base_url)

    return sorted(url for url in found if url)


def _normalize_source_title(title: str, source_url: str) -> str:
    text = re.sub(r"\s+", " ", str(title or "")).strip()
    if not text:
        return ""

    for separator in (" - ", " | ", " — ", " – "):
        marker = f"{separator}MakerWorld"
        if text.endswith(marker):
            text = text[: -len(marker)].strip()
            break

    path = (urlparse(source_url).path or "").lower()
    if "/upload" in path:
        text = re.sub(r"\s*(的)?(上传|作品|模型)(页面|页)?\s*$", "", text, flags=re.I).strip()
        text = re.sub(r"\s*(uploads?|models?)\s*$", "", text, flags=re.I).strip()

    return text.strip(" -|—–")


def resolve_batch_source_name(url: str, raw_cookie: str) -> str:
    source_url = normalize_source_url(url)
    session = requests.Session()
    session.headers.update({"User-Agent": BROWSER_USER_AGENT})
    session.cookies.update(parse_cookies(raw_cookie))

    try:
        html_text = _fetch_listing_html(session, source_url, raw_cookie)
    except Exception:
        html_text = ""

    if html_text:
        soup = BeautifulSoup(str(html_text), "html.parser")
        candidates: list[str] = []

        for node in soup.select("meta[property='og:title'], meta[name='og:title'], meta[name='twitter:title']"):
            content = str(node.get("content") or "").strip()
            if content:
                candidates.append(content)

        for node in soup.select("main h1, h1, [data-testid='page-title'], [class*='title']"):
            text = node.get_text(" ", strip=True)
            if text:
                candidates.append(text)

        title_node = soup.find("title")
        if title_node:
            title_text = title_node.get_text(" ", strip=True)
            if title_text:
                candidates.append(title_text)

        for candidate in candidates:
            normalized = _normalize_source_title(candidate, source_url)
            if normalized and normalized.lower() != "makerworld":
                return normalized

    if _is_collection_models_url(source_url):
        handle = _extract_collection_handle(source_url)
        return f"{handle or 'MakerWorld'} 收藏夹".strip()

    handle_match = AUTHOR_UPLOAD_RE.search(urlparse(source_url).path or "")
    if handle_match:
        return handle_match.group(1).strip()

    return ""


def _page_variants(base_url: str, page: int, limit: int = HTML_BATCH_PAGE_LIMIT) -> list[str]:
    if page <= 1:
        return [base_url]
    parsed = urlparse(base_url)
    existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
    offset = max(page - 1, 0) * max(limit, 1)
    candidates = [
        {"page": page},
        {"page": page, "limit": limit},
        {"pageNum": page},
        {"pageNum": page, "limit": limit},
        {"pageNo": page},
        {"pageNo": page, "limit": limit},
        {"p": page},
        {"p": page, "limit": limit},
        {"offset": offset, "limit": limit},
        {"offset": offset, "page": page, "limit": limit},
        {"current": page, "size": limit},
        {"currentPage": page, "pageSize": limit},
    ]
    variants: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        payload = dict(existing)
        payload.update({key: str(value) for key, value in candidate.items()})
        variant = urlunparse(parsed._replace(query=urlencode(payload)))
        if variant in seen:
            continue
        seen.add(variant)
        variants.append(variant)
    return variants


def _discover_by_html(session: requests.Session, source_url: str, raw_cookie: str, max_pages: int) -> dict:
    discovered: list[str] = []
    seen: set[str] = set()
    pages_scanned = 0

    for page in range(1, max_pages + 1):
        best_links: list[str] = []
        best_total_count = -1
        best_new_count = -1
        best_variant = ""
        for variant in _page_variants(source_url, page):
            html_text = _fetch_listing_html(session, variant, raw_cookie)
            page_links = _extract_page_links(html_text, variant)
            new_link_count = len([link for link in page_links if link not in seen])
            _append_discovery_debug(
                "html_page_variant",
                page=page,
                variant=variant,
                total_links=len(page_links),
                new_links=new_link_count,
            )

            should_replace = False
            if page <= 1:
                should_replace = len(page_links) > best_total_count
            else:
                should_replace = (
                    new_link_count > best_new_count
                    or (new_link_count == best_new_count and len(page_links) > best_total_count)
                )

            if should_replace:
                best_links = page_links
                best_total_count = len(page_links)
                best_new_count = new_link_count
                best_variant = variant

        if page == 1 and not best_links:
            raise RuntimeError("未能在页面中识别模型链接，请确认链接与 Cookie 是否有效。")

        new_links = [link for link in best_links if link not in seen]
        _append_discovery_debug(
            "html_page_selected",
            page=page,
            variant=best_variant,
            total_links=len(best_links),
            new_links=len(new_links),
        )
        pages_scanned = page
        if not new_links and page > 1:
            break

        for link in new_links:
            seen.add(link)
            discovered.append(link)

    return {
        "source_url": source_url,
        "pages_scanned": pages_scanned,
        "items": discovered,
        "mode": "html_fallback",
    }


def discover_batch_model_urls(url: str, raw_cookie: str, max_pages: int = 12) -> dict:
    source_url = normalize_source_url(url)
    session = requests.Session()
    session.headers.update({"User-Agent": BROWSER_USER_AGENT})
    session.cookies.update(parse_cookies(raw_cookie))

    if _is_collection_models_url(source_url):
        if _is_collection_detail_url(source_url):
            detail_result = _discover_collection_detail_models_api(
                session=session,
                source_url=source_url,
                raw_cookie=raw_cookie,
                max_pages=max_pages,
            )
            if detail_result is not None:
                return detail_result

        api_result = _discover_collection_models_api(
            session=session,
            source_url=source_url,
            raw_cookie=raw_cookie,
            max_pages=max_pages,
        )
        if api_result is not None:
            return api_result

        handle = _extract_collection_handle(source_url)
        owner_uid = _resolve_collection_owner_uid(session, source_url, raw_cookie, handle) if handle else ""
        if handle and owner_uid:
            list_result = _discover_collection_models_by_lists(
                session=session,
                source_url=source_url,
                raw_cookie=raw_cookie,
                handle=handle,
                owner_uid=owner_uid,
                max_pages=max_pages,
            )
            if list_result is not None:
                return list_result

    if AUTHOR_UPLOAD_RE.search(urlparse(source_url).path or ""):
        api_result = _discover_author_upload_api(
            session=session,
            source_url=source_url,
            raw_cookie=raw_cookie,
            max_pages=max_pages,
        )
        if api_result is not None:
            return api_result

    return _discover_by_html(
        session=session,
        source_url=source_url,
        raw_cookie=raw_cookie,
        max_pages=max_pages,
    )
