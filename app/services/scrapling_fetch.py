from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlencode

import requests

from app.services.cookie_utils import sanitize_cookie_header
from app.services.proxy_policy import proxy_url


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


def _proxy_url(proxy_config: Any = None, target_url: str = "", *, allow_domestic_proxy: bool = False) -> str:
    proxy = proxy_config
    if proxy is None:
        try:
            from app.core.store import JsonStore

            proxy = JsonStore().load().proxy
        except Exception:
            proxy = None
    return proxy_url(proxy, target_url, allow_domestic_proxy=allow_domestic_proxy)


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


def _request_get(url: str, **kwargs: Any) -> requests.Response:
    return requests.get(url, **kwargs)


def fetch_text(
    url: str,
    *,
    raw_cookie: str = "",
    headers: Optional[dict[str, str]] = None,
    params: Optional[dict[str, Any]] = None,
    proxy_config: Any = None,
    timeout: float = 30,
    logger: Optional[Callable[..., None]] = None,
    advanced_config: Any = None,
    allow_domestic_proxy: bool = False,
) -> ScraplingFetchResult:
    if not scrapling_enabled(advanced_config):
        return ScraplingFetchResult(ok=False, url=url, engine="disabled", error="Static fetch disabled")

    request_headers = _headers_with_cookie(headers, raw_cookie)
    target_url = _with_params(url, params)
    proxy = _proxy_url(proxy_config, target_url, allow_domestic_proxy=allow_domestic_proxy)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    static_result = ScraplingFetchResult(ok=False, url=target_url, engine="scrapling-static")
    last_error = ""
    for _ in range(3):
        try:
            response = _request_get(
                target_url,
                timeout=timeout,
                proxies=proxies,
                headers=request_headers,
            )
            return _result_from_response(response, "scrapling-static")
        except requests.RequestException as exc:
            last_error = str(exc)[:240]
        except Exception as exc:
            last_error = str(exc)[:240]
            break
    static_result.error = last_error
    if last_error:
        _log(logger, "静态抓取失败:", last_error)
    return static_result


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
    allow_domestic_proxy: bool = False,
) -> ScraplingFetchResult:
    result = fetch_text(
        url,
        raw_cookie=raw_cookie,
        headers=headers,
        params=params,
        proxy_config=proxy_config,
        timeout=timeout,
        logger=logger,
        advanced_config=advanced_config,
        allow_domestic_proxy=allow_domestic_proxy,
    )
    if not result.ok:
        return result
    try:
        result.payload = json.loads(result.text or "")
    except Exception as exc:
        result.ok = False
        result.error = f"non-json response: {str(exc)[:180]}"
    return result
