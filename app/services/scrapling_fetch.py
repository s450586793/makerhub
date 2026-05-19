from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlencode

from app.services.cookie_utils import sanitize_cookie_header


SCRAPING_ENGINE_LEGACY = "legacy"
SCRAPING_ENGINE_SCRAPLING_FIRST = "scrapling_first"
SCRAPING_ENGINE_SCRAPLING_ONLY = "scrapling_only"
SCRAPING_ENGINE_CHOICES = {
    SCRAPING_ENGINE_LEGACY,
    SCRAPING_ENGINE_SCRAPLING_FIRST,
    SCRAPING_ENGINE_SCRAPLING_ONLY,
}


@dataclass
class ScraplingFetchResult:
    ok: bool = False
    status_code: int = 0
    text: str = ""
    payload: Any = None
    headers: dict[str, Any] | None = None
    url: str = ""
    engine: str = ""
    error: str = ""


def _log(logger: Optional[Callable[..., None]], *args: Any) -> None:
    if not callable(logger):
        return
    try:
        logger(" ".join(str(arg) for arg in args))
    except Exception:
        pass


def _load_fetchers():
    try:
        from scrapling.fetchers import Fetcher, StealthyFetcher  # type: ignore

        return Fetcher, StealthyFetcher, ""
    except Exception as exc:
        try:
            from scrapling import Fetcher, StealthyFetcher  # type: ignore

            return Fetcher, StealthyFetcher, ""
        except Exception:
            return None, None, str(exc)


def scrapling_available() -> bool:
    fetcher, _, _ = _load_fetchers()
    return fetcher is not None


def _coerce_engine(value: Any) -> str:
    engine = str(value or SCRAPING_ENGINE_SCRAPLING_FIRST).strip().lower()
    return engine if engine in SCRAPING_ENGINE_CHOICES else SCRAPING_ENGINE_SCRAPLING_FIRST


def _load_advanced_config() -> dict[str, Any]:
    try:
        from app.core.store import JsonStore

        advanced = JsonStore().load().advanced
        return advanced.model_dump() if hasattr(advanced, "model_dump") else dict(advanced or {})
    except Exception:
        return {}


def scrapling_enabled(config: Any = None) -> bool:
    raw = config.model_dump() if hasattr(config, "model_dump") else config
    if not isinstance(raw, dict):
        raw = _load_advanced_config()
    return _coerce_engine(raw.get("scraping_engine")) != SCRAPING_ENGINE_LEGACY


def scrapling_only(config: Any = None) -> bool:
    raw = config.model_dump() if hasattr(config, "model_dump") else config
    if not isinstance(raw, dict):
        raw = _load_advanced_config()
    return _coerce_engine(raw.get("scraping_engine")) == SCRAPING_ENGINE_SCRAPLING_ONLY


def _use_browser_fallback(config: Any = None) -> bool:
    raw = config.model_dump() if hasattr(config, "model_dump") else config
    if not isinstance(raw, dict):
        raw = _load_advanced_config()
    return bool(raw.get("scrapling_browser_fallback", True))


def _proxy_url(proxy_config: Any = None) -> str:
    proxy = proxy_config
    if proxy is None:
        try:
            from app.core.store import JsonStore

            proxy = JsonStore().load().proxy
        except Exception:
            proxy = None
    if not proxy or not bool(getattr(proxy, "enabled", False)):
        return ""
    https_proxy = str(getattr(proxy, "https_proxy", "") or "").strip()
    http_proxy = str(getattr(proxy, "http_proxy", "") or "").strip()
    return https_proxy or http_proxy


def _headers_with_cookie(headers: Optional[dict[str, str]], raw_cookie: str) -> dict[str, str]:
    clean_headers: dict[str, str] = {}
    for key, value in (headers or {}).items():
        if value is None:
            continue
        clean_headers[str(key)] = str(value)
    cookie_header = sanitize_cookie_header(raw_cookie)
    if cookie_header and not any(key.lower() == "cookie" for key in clean_headers):
        clean_headers["Cookie"] = cookie_header
    return clean_headers


def _response_text(response: Any) -> str:
    text = getattr(response, "text", "")
    if callable(text):
        try:
            text = text()
        except Exception:
            text = ""
    if isinstance(text, bytes):
        return text.decode("utf-8", errors="replace")
    if isinstance(text, str):
        return text
    try:
        return str(response)
    except Exception:
        return ""


def _result_from_response(response: Any, engine: str) -> ScraplingFetchResult:
    status = int(getattr(response, "status", 0) or getattr(response, "status_code", 0) or 0)
    text = _response_text(response)
    return ScraplingFetchResult(
        ok=bool(status and status < 400),
        status_code=status,
        text=text,
        headers=dict(getattr(response, "headers", {}) or {}),
        url=str(getattr(response, "url", "") or ""),
        engine=engine,
    )


def _with_params(url: str, params: Optional[dict[str, Any]]) -> str:
    if not params:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urlencode(params, doseq=True)}"


def _looks_like_verification_or_html(text: str) -> bool:
    lowered = str(text or "").lstrip().lower()
    if not lowered:
        return False
    return (
        lowered.startswith("<!doctype html")
        or lowered.startswith("<html")
        or "cf-browser-verification" in lowered
        or "cf-challenge" in lowered
        or "challenge-platform" in lowered
        or "verify you are human" in lowered
    )


def _looks_like_verification(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(
        marker in lowered
        for marker in (
            "cf-browser-verification",
            "cf-challenge",
            "challenge-platform",
            "cf_clearance",
            "verify you are human",
            "checking your browser",
            "security check",
        )
    )


def _should_try_browser(result: ScraplingFetchResult, *, expect_json: bool) -> bool:
    if not result.status_code:
        return True
    if _looks_like_verification(result.text):
        return True
    if expect_json and result.ok and _looks_like_verification_or_html(result.text):
        return True
    if result.status_code in {403, 429, 503} and _looks_like_verification_or_html(result.text):
        return True
    return False


def fetch_text(
    url: str,
    *,
    raw_cookie: str = "",
    headers: Optional[dict[str, str]] = None,
    params: Optional[dict[str, Any]] = None,
    proxy_config: Any = None,
    timeout: float = 30,
    expect_json: bool = False,
    logger: Optional[Callable[..., None]] = None,
    advanced_config: Any = None,
) -> ScraplingFetchResult:
    if not scrapling_enabled(advanced_config):
        return ScraplingFetchResult(ok=False, url=url, engine="disabled", error="Scrapling disabled")

    Fetcher, StealthyFetcher, import_error = _load_fetchers()
    if Fetcher is None:
        return ScraplingFetchResult(ok=False, url=url, engine="unavailable", error=import_error or "Scrapling unavailable")

    request_headers = _headers_with_cookie(headers, raw_cookie)
    proxy = _proxy_url(proxy_config)
    target_url = _with_params(url, params)
    common_kwargs: dict[str, Any] = {"headers": request_headers}
    static_result = ScraplingFetchResult(ok=False, url=target_url, engine="scrapling-static")
    try:
        response = Fetcher.get(
            target_url,
            timeout=timeout,
            retries=2,
            proxy=proxy or None,
            stealthy_headers=True,
            **common_kwargs,
        )
        static_result = _result_from_response(response, "scrapling-static")
        if static_result.ok and not _should_try_browser(static_result, expect_json=expect_json):
            return static_result
    except Exception as exc:
        static_result.error = str(exc)[:240]
        _log(logger, "Scrapling 静态抓取失败:", type(exc).__name__, static_result.error)

    if (
        not _use_browser_fallback(advanced_config)
        or StealthyFetcher is None
        or not _should_try_browser(static_result, expect_json=expect_json)
    ):
        return static_result

    try:
        response = StealthyFetcher.fetch(
            target_url,
            headless=True,
            disable_resources=False,
            block_images=True,
            network_idle=False,
            timeout=max(float(timeout), 10.0) * 1000,
            extra_headers=request_headers,
            proxy=proxy or None,
            wait=0,
        )
        return _result_from_response(response, "scrapling-browser")
    except Exception as exc:
        error = str(exc)[:240]
        _log(logger, "Scrapling 浏览器抓取失败:", type(exc).__name__, error)
        if static_result.error:
            error = f"{static_result.error}; {error}"[:240]
        return ScraplingFetchResult(
            ok=False,
            status_code=static_result.status_code,
            text=static_result.text,
            headers=static_result.headers,
            url=target_url,
            engine="scrapling-browser",
            error=error,
        )


def fetch_json(
    url: str,
    *,
    raw_cookie: str = "",
    headers: Optional[dict[str, str]] = None,
    params: Optional[dict[str, Any]] = None,
    proxy_config: Any = None,
    timeout: float = 30,
    logger: Optional[Callable[..., None]] = None,
    advanced_config: Any = None,
) -> ScraplingFetchResult:
    result = fetch_text(
        url,
        raw_cookie=raw_cookie,
        headers=headers,
        params=params,
        proxy_config=proxy_config,
        timeout=timeout,
        expect_json=True,
        logger=logger,
        advanced_config=advanced_config,
    )
    if not result.ok:
        return result
    try:
        result.payload = json.loads(result.text or "")
    except Exception as exc:
        result.ok = False
        result.error = f"non-json response: {str(exc)[:180]}"
    return result
