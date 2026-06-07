from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.services.business_logs import append_business_log


GET_SLOW_THRESHOLD_MS = 800
WRITE_SLOW_THRESHOLD_MS = 1500
HIGH_FREQUENCY_GET_SLOW_THRESHOLD_MS = 2500
FRONTEND_SLOW_PAGE_THRESHOLD_MS = 1200
HIGH_FREQUENCY_PATHS = {
    "/api/logs",
    "/api/state-events",
}
SKIP_API_TIMING_PATHS = {
    "/api/performance/events",
}


def _safe_path(value: Any) -> str:
    path = str(value or "").strip()
    if not path.startswith("/"):
        return "/"
    return path[:240]


def _query_keys(request: Any) -> list[str]:
    params = getattr(request, "query_params", {}) or {}
    try:
        keys = params.keys()
    except AttributeError:
        keys = []
    return sorted({str(key)[:80] for key in keys if str(key or "").strip()})


def _response_size(response: Any) -> int:
    headers = getattr(response, "headers", {}) or {}
    try:
        raw_value = headers.get("content-length") or headers.get("Content-Length") or ""
    except AttributeError:
        raw_value = ""
    try:
        return max(int(raw_value), 0)
    except (TypeError, ValueError):
        return 0


def _threshold_for(method: str, path: str) -> int:
    clean_method = str(method or "").upper()
    clean_path = _safe_path(path)
    if clean_method == "GET" and clean_path in HIGH_FREQUENCY_PATHS:
        return HIGH_FREQUENCY_GET_SLOW_THRESHOLD_MS
    if clean_method == "GET":
        return GET_SLOW_THRESHOLD_MS
    return WRITE_SLOW_THRESHOLD_MS


def log_api_request_if_needed(request: Any, response: Any, *, duration_ms: float) -> None:
    method = str(getattr(request, "method", "") or "").upper()
    path = _safe_path(getattr(getattr(request, "url", None), "path", ""))
    if path in SKIP_API_TIMING_PATHS:
        return

    status_code = int(getattr(response, "status_code", 0) or 0)
    rounded_duration = round(float(duration_ms or 0), 1)
    failed = status_code >= 400
    slow = rounded_duration >= _threshold_for(method, path)
    if not failed and not slow:
        return

    event = "api_error_request" if failed else "slow_api_request"
    message = "API 请求失败。" if failed else "API 请求耗时较高。"
    try:
        append_business_log(
            "performance",
            event,
            message,
            level="warning" if failed else "info",
            method=method,
            path=path,
            status_code=status_code,
            duration_ms=rounded_duration,
            slow=slow,
            query_keys=_query_keys(request),
            response_size=_response_size(response),
        )
    except Exception:
        return


def _safe_route(value: Any) -> str:
    raw_route = str(value or "").strip()[:240]
    if not raw_route:
        return ""
    parts = urlsplit(raw_route)
    path = parts.path or raw_route.split("?", 1)[0]
    return urlunsplit(("", "", path[:200], "", "")) or "/"


def _safe_int(value: Any, minimum: int = 0, maximum: int = 10000) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return minimum
    return max(minimum, min(number, maximum))


def _safe_float(value: Any, minimum: float = 0.0, maximum: float = 600000.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return minimum
    return round(max(minimum, min(number, maximum)), 1)


def log_frontend_page_event(payload: dict[str, Any]) -> dict[str, bool]:
    if not isinstance(payload, dict):
        return {"success": True, "recorded": False}

    duration_ms = _safe_float(payload.get("duration_ms"))
    if duration_ms < FRONTEND_SLOW_PAGE_THRESHOLD_MS:
        return {"success": True, "recorded": False}

    page = str(payload.get("page") or "").strip()[:80] or "unknown"
    try:
        append_business_log(
            "performance",
            "slow_page_load",
            "页面首屏加载较慢。",
            page=page,
            route=_safe_route(payload.get("route")),
            duration_ms=duration_ms,
            api_count=_safe_int(payload.get("api_count"), maximum=500),
            slow_api_count=_safe_int(payload.get("slow_api_count"), maximum=500),
            max_api_duration_ms=_safe_float(payload.get("max_api_duration_ms")),
        )
        return {"success": True, "recorded": True}
    except Exception:
        return {"success": True, "recorded": False}
