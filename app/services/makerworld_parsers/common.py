import re
from typing import Literal
from urllib.parse import quote, urljoin, urlparse, urlunparse


MODEL_PATH_RE = re.compile(r"/(?:[a-z]{2}/)?models/(\d+)(?:[^\"'\\s<>]*)?", re.I)
AUTHOR_UPLOAD_RE = re.compile(r"/(?:[a-z]{2}/)?@([^/?#]+)/upload(?:[/?#]|$)", re.I)
AUTHOR_ROOT_RE = re.compile(r"^/(?:[a-z]{2}/)?@[^/?#]+/?$", re.I)
COLLECTION_DETAIL_RE = re.compile(r"/(?:[a-z]{2}/)?collections/(\d+)(?:-[^/?#]+)?(?:[/?#]|$)", re.I)

_CN_HOSTS = frozenset({"makerworld.com.cn", "api.bambulab.cn"})
_GLOBAL_HOSTS = frozenset({"makerworld.com", "api.bambulab.com"})


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
    author_match = AUTHOR_UPLOAD_RE.search(path)
    if author_match:
        handle = author_match.group(1)
        normalized_path = f"/zh/@{quote(handle, safe='@._-')}/upload"
        return urlunparse(parsed._replace(path=normalized_path, query="", fragment=""))
    if AUTHOR_ROOT_RE.fullmatch(path):
        handle = path.rstrip("/").split("@", 1)[-1]
        normalized_path = f"/zh/@{quote(handle, safe='@._-')}/upload"
        return urlunparse(parsed._replace(path=normalized_path, query="", fragment=""))

    return absolute


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


def platform_from_url(url: str) -> Literal["cn", "global", ""]:
    host = (urlparse(str(url or "").strip()).hostname or "").lower()
    if host in _CN_HOSTS:
        return "cn"
    if host in _GLOBAL_HOSTS:
        return "global"
    return ""
