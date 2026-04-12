import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from app.services.legacy_archiver import extract_next_data, fetch_html_with_curl, fetch_html_with_requests, parse_cookies


MODEL_PATH_RE = re.compile(r"/(?:[a-z]{2}/)?models/(\d+)(?:[^\"'\\s<>]*)?", re.I)


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


def discover_batch_model_urls(url: str, raw_cookie: str, max_pages: int = 12) -> dict:
    source_url = normalize_source_url(url)
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Makerhub Batch Scanner)"})
    session.cookies.update(parse_cookies(raw_cookie))

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
    }
