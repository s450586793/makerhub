import re
from typing import Any, Optional
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from app.services.legacy_archiver import extract_next_data, fetch_html_with_curl, fetch_html_with_requests, parse_cookies


MODEL_PATH_RE = re.compile(r"/(?:[a-z]{2}/)?models/(\d+)(?:[^\"'\\s<>]*)?", re.I)
AUTHOR_UPLOAD_RE = re.compile(r"/(?:[a-z]{2}/)?@([^/?#]+)/upload(?:[/?#]|$)", re.I)

API_BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "X-BBL-Client-Type": "web",
    "X-BBL-Client-Version": "00.00.00.01",
    "X-BBL-App-Source": "makerworld",
}


def normalize_source_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    if raw.startswith("/"):
        return urljoin("https://makerworld.com.cn", raw)
    if not raw.startswith("http://") and not raw.startswith("https://"):
        return f"https://{raw.lstrip('/')}"
    return raw


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
    cookies = parse_cookies(raw_cookie or "")
    return (
        cookies.get("token")
        or cookies.get("access_token")
        or cookies.get("accessToken")
        or ""
    )


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
        fallback_site = "https://makerworld.com.cn"
        fallback_api = "https://api.bambulab.cn"
    else:
        preferred_site = "https://makerworld.com.cn"
        preferred_api = "https://api.bambulab.cn"
        fallback_site = "https://makerworld.com"
        fallback_api = "https://api.bambulab.com"

    bases: list[str] = []
    for candidate in (_origin_from_url(source_url), preferred_site, preferred_api, fallback_site, fallback_api):
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
    headers = dict(API_BROWSER_HEADERS)
    headers["Referer"] = referer or "https://makerworld.com.cn/"
    headers["User-Agent"] = session.headers.get("User-Agent", "Mozilla/5.0 (Makerhub Batch Scanner)")
    if raw_cookie:
        headers["Cookie"] = raw_cookie

    auth_token = _extract_auth_token(raw_cookie)
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
        headers["token"] = auth_token
        headers["X-Token"] = auth_token
        headers["X-Access-Token"] = auth_token
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
        try:
            response = session.get(api_url, params=params or None, headers=headers, timeout=30)
        except Exception:
            continue
        if response.status_code >= 400:
            continue
        text = response.text or ""
        if _looks_like_html_response(text):
            continue
        try:
            payload = response.json()
        except Exception:
            continue
        if isinstance(payload, dict):
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


def _extract_hits_payload(payload: Any) -> Optional[dict]:
    for node in _iter_dicts(payload):
        if isinstance(node.get("hits"), list):
            return node
    return None


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


def _extract_author_handle(source_url: str) -> str:
    match = AUTHOR_UPLOAD_RE.search(urlparse(source_url).path or "")
    if not match:
        return ""
    return match.group(1).strip("@").strip()


def _resolve_author_uid(session: requests.Session, source_url: str, raw_cookie: str, handle: str) -> str:
    profile_payload = _api_get_json(
        session,
        source_url=source_url,
        raw_cookie=raw_cookie,
        service_name="design-service",
        path=f"/profile/{quote(handle, safe='@._-')}",
    )
    uid = _extract_uid(profile_payload)
    if uid:
        return uid

    handle_variants = []
    normalized_handle = str(handle or "").strip("@").strip()
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
                return uid
    return ""


def _discover_author_upload_api(
    session: requests.Session,
    source_url: str,
    raw_cookie: str,
    max_pages: int,
    limit: int = 20,
) -> Optional[dict]:
    handle = _extract_author_handle(source_url)
    if not handle:
        return None

    uid = _resolve_author_uid(session, source_url, raw_cookie, handle)
    if not uid:
        return None

    discovered: list[str] = []
    seen: set[str] = set()
    pages_scanned = 0
    offset = 0
    expected_total: Optional[int] = None

    for _ in range(max_pages):
        payload = _api_get_json(
            session,
            source_url=source_url,
            raw_cookie=raw_cookie,
            service_name="design-service",
            path=f"/published/{uid}/design",
            params={"offset": offset, "limit": limit},
        )
        if payload is None:
            return None if pages_scanned == 0 else {
                "source_url": source_url,
                "pages_scanned": pages_scanned,
                "items": discovered,
                "mode": "author_upload_api_partial",
                "expected_total": expected_total,
            }

        hits_payload = _extract_hits_payload(payload)
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
        page_links = _extract_model_urls_from_hits(hits_payload, source_url)

        for link in page_links:
            if link in seen:
                continue
            seen.add(link)
            discovered.append(link)

        if not hits:
            break

        has_next = _extract_has_next(hits_payload)
        if has_next is False:
            break
        if expected_total is not None and len(discovered) >= expected_total:
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


def _page_variants(base_url: str, page: int) -> list[str]:
    if page <= 1:
        return [base_url]
    parsed = urlparse(base_url)
    existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
    variants = []
    for key in ("page", "pageNum", "pageNo", "p"):
        payload = dict(existing)
        payload[key] = str(page)
        variants.append(urlunparse(parsed._replace(query=urlencode(payload))))
    return variants


def _discover_by_html(session: requests.Session, source_url: str, raw_cookie: str, max_pages: int) -> dict:
    discovered: list[str] = []
    seen: set[str] = set()
    pages_scanned = 0

    for page in range(1, max_pages + 1):
        best_links: list[str] = []
        best_count = -1
        for variant in _page_variants(source_url, page):
            html_text = _fetch_listing_html(session, variant, raw_cookie)
            page_links = _extract_page_links(html_text, variant)
            if len(page_links) > best_count:
                best_links = page_links
                best_count = len(page_links)

        if page == 1 and not best_links:
            raise RuntimeError("未能在页面中识别模型链接，请确认链接与 Cookie 是否有效。")

        new_links = [link for link in best_links if link not in seen]
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
    session.headers.update({"User-Agent": "Mozilla/5.0 (Makerhub Batch Scanner)"})
    session.cookies.update(parse_cookies(raw_cookie))

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
