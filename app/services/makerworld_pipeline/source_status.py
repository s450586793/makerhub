from urllib.parse import urlparse

from app.services.makerworld_browser_client import MakerWorldBrowserError, makerworld_browser_get
from app.services.makerworld_parsers.common import normalize_source_url


def source_is_deleted(url: str, raw_cookie: str) -> bool:
    try:
        response = makerworld_browser_get(
            normalize_source_url(url),
            raw_cookie=raw_cookie,
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
    except MakerWorldBrowserError:
        return False

    final_path = urlparse(response.url).path.lower()
    return (
        response.status_code == 404
        or "/404" in final_path
        or "not found" in response.text[:400].lower()
    )
