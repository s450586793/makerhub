import html
import json
import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from app.services.cookie_utils import sanitize_cookie_header


DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_CONCURRENCY = 1

_SEMAPHORE_LOCK = threading.Lock()
_SEMAPHORE_SIZE = 0
_SEMAPHORE: Optional[threading.Semaphore] = None


class FlareSolverrError(RuntimeError):
    pass


class FlareSolverrJsonError(FlareSolverrError):
    pass


@dataclass(frozen=True)
class FlareSolverrSolution:
    url: str
    status_code: int
    text: str
    user_agent: str = ""
    cookies: tuple[dict[str, Any], ...] = ()


def _configured_url() -> str:
    raw = str(os.getenv("MAKERHUB_FLARESOLVERR_URL") or "").strip()
    if not raw:
        raise FlareSolverrError("缺少 MAKERHUB_FLARESOLVERR_URL，无法通过 FlareSolverr 请求 MakerWorld。")
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        raise FlareSolverrError("MAKERHUB_FLARESOLVERR_URL 格式无效。")
    if parsed.path in {"", "/"}:
        return urlunparse(parsed._replace(path="/v1", query="", fragment=""))
    return urlunparse(parsed._replace(query="", fragment=""))


def _timeout_seconds() -> int:
    raw = str(os.getenv("MAKERHUB_FLARESOLVERR_TIMEOUT") or "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = int(float(raw))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return max(min(value, 300), 5)


def _max_concurrency() -> int:
    raw = str(os.getenv("MAKERHUB_FLARESOLVERR_MAX_CONCURRENCY") or "").strip()
    if not raw:
        return DEFAULT_MAX_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_CONCURRENCY
    return max(min(value, 8), 1)


def _semaphore() -> threading.Semaphore:
    global _SEMAPHORE, _SEMAPHORE_SIZE
    size = _max_concurrency()
    with _SEMAPHORE_LOCK:
        if _SEMAPHORE is None or _SEMAPHORE_SIZE != size:
            _SEMAPHORE = threading.Semaphore(size)
            _SEMAPHORE_SIZE = size
        return _SEMAPHORE


def _url_with_params(url: str, params: Optional[dict[str, Any]]) -> str:
    if not params:
        return url
    parsed = urlparse(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            pairs.extend((key, str(item)) for item in value if item is not None)
        else:
            pairs.append((key, str(value)))
    return urlunparse(parsed._replace(query=urlencode(pairs, doseq=True)))


def _cookie_domain_for_url(url: str) -> str:
    return urlparse(url).hostname or ""


def _cookies_from_header(raw_cookie: str, url: str) -> list[dict[str, str]]:
    cookie_header = sanitize_cookie_header(raw_cookie)
    if not cookie_header:
        return []
    domain = _cookie_domain_for_url(url)
    cookies: list[dict[str, str]] = []
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        clean_name = name.strip()
        if not clean_name:
            continue
        item = {
            "name": clean_name,
            "value": value.strip(),
            "path": "/",
        }
        if domain:
            item["domain"] = domain
        cookies.append(item)
    return cookies


def _apply_solution_to_session(session: Optional[requests.Session], solution: dict[str, Any], final_url: str) -> None:
    if session is None:
        return
    user_agent = str(solution.get("userAgent") or "").strip()
    if user_agent:
        session.headers.update({"User-Agent": user_agent})
    fallback_domain = _cookie_domain_for_url(final_url)
    for item in solution.get("cookies") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        value = str(item.get("value") or "")
        domain = str(item.get("domain") or fallback_domain or "").strip()
        path = str(item.get("path") or "/").strip() or "/"
        try:
            if domain:
                session.cookies.set(name, value, domain=domain, path=path)
            else:
                session.cookies.set(name, value, path=path)
        except Exception:
            continue


def flaresolverr_get(
    url: str,
    *,
    raw_cookie: str = "",
    headers: Optional[dict[str, str]] = None,
    params: Optional[dict[str, Any]] = None,
    session: Optional[requests.Session] = None,
    session_name: str = "",
) -> FlareSolverrSolution:
    target_url = _url_with_params(url, params)
    solver_url = _configured_url()
    timeout_seconds = _timeout_seconds()
    payload: dict[str, Any] = {
        "cmd": "request.get",
        "url": target_url,
        "maxTimeout": timeout_seconds * 1000,
        "disableMedia": True,
    }
    clean_headers = {str(key): str(value) for key, value in (headers or {}).items() if value is not None}
    if clean_headers:
        payload["headers"] = clean_headers
    cookies = _cookies_from_header(raw_cookie, target_url)
    if cookies:
        payload["cookies"] = cookies
    if session_name:
        payload["session"] = session_name

    try:
        with _semaphore():
            response = requests.post(solver_url, json=payload, timeout=timeout_seconds)
            response.raise_for_status()
            body = response.json()
    except FlareSolverrError:
        raise
    except Exception as exc:
        raise FlareSolverrError(f"FlareSolverr 请求失败：{exc}") from exc

    if not isinstance(body, dict):
        raise FlareSolverrError("FlareSolverr 返回格式无效。")
    if body.get("status") != "ok":
        message = str(body.get("message") or "FlareSolverr 返回失败状态。").strip()
        raise FlareSolverrError(message)
    solution = body.get("solution")
    if not isinstance(solution, dict):
        raise FlareSolverrError("FlareSolverr 返回缺少 solution。")
    _apply_solution_to_session(session, solution, target_url)
    return FlareSolverrSolution(
        url=str(solution.get("url") or target_url),
        status_code=int(solution.get("status") or 0),
        text=str(solution.get("response") or ""),
        user_agent=str(solution.get("userAgent") or ""),
        cookies=tuple(item for item in solution.get("cookies") or [] if isinstance(item, dict)),
    )


def flaresolverr_get_text(url: str, **kwargs) -> str:
    return flaresolverr_get(url, **kwargs).text


def loads_solution_json(text: str) -> Any:
    raw = str(text or "").strip()
    if not raw:
        raise FlareSolverrJsonError("FlareSolverr 响应为空。")
    try:
        return json.loads(raw)
    except Exception:
        pass

    pre_match = re.search(r"<pre[^>]*>(.*?)</pre>", raw, flags=re.I | re.S)
    if pre_match:
        pre_text = html.unescape(pre_match.group(1)).strip()
        try:
            return json.loads(pre_text)
        except Exception as exc:
            raise FlareSolverrJsonError(f"FlareSolverr 响应不是有效 JSON：{exc}") from exc

    stripped = re.sub(r"<[^>]+>", "", raw)
    stripped = html.unescape(stripped).strip()
    if stripped and stripped != raw:
        try:
            return json.loads(stripped)
        except Exception:
            pass
    raise FlareSolverrJsonError("FlareSolverr 响应不是有效 JSON。")


def flaresolverr_get_json(url: str, *, allow_non_json: bool = False, **kwargs) -> Any:
    solution = flaresolverr_get(url, **kwargs)
    try:
        return loads_solution_json(solution.text)
    except FlareSolverrJsonError:
        if allow_non_json:
            return None
        raise
