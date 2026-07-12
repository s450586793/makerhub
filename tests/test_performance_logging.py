from types import SimpleNamespace
from unittest.mock import patch

from app.services import performance


def test_slow_get_request_is_logged_without_query_values():
    request = SimpleNamespace(
        method="GET",
        url=SimpleNamespace(path="/api/models"),
        query_params={"q": "secret search", "page": "2"},
    )
    response = SimpleNamespace(status_code=200, headers={"content-length": "1234"})

    with patch.object(performance, "append_business_log") as log:
        performance.log_api_request_if_needed(request, response, duration_ms=901)

    log.assert_called_once()
    args, kwargs = log.call_args
    assert args[:3] == ("performance", "slow_api_request", "API 请求耗时较高。")
    assert kwargs["method"] == "GET"
    assert kwargs["path"] == "/api/models"
    assert kwargs["status_code"] == 200
    assert kwargs["duration_ms"] == 901
    assert kwargs["query_keys"] == ["page", "q"]
    assert "secret search" not in str(kwargs)


def test_fast_successful_request_is_not_logged():
    request = SimpleNamespace(
        method="GET",
        url=SimpleNamespace(path="/api/models"),
        query_params={"page": "1"},
    )
    response = SimpleNamespace(status_code=200, headers={})

    with patch.object(performance, "append_business_log") as log:
        performance.log_api_request_if_needed(request, response, duration_ms=120)

    log.assert_not_called()


def test_failed_request_is_logged_even_when_fast():
    request = SimpleNamespace(
        method="POST",
        url=SimpleNamespace(path="/api/archive"),
        query_params={},
    )
    response = SimpleNamespace(status_code=500, headers={})

    with patch.object(performance, "append_business_log") as log:
        performance.log_api_request_if_needed(request, response, duration_ms=50)

    log.assert_called_once()
    args, kwargs = log.call_args
    assert args[:3] == ("performance", "api_error_request", "API 请求失败。")
    assert kwargs["method"] == "POST"
    assert kwargs["path"] == "/api/archive"
    assert kwargs["status_code"] == 500


def test_frontend_slow_page_event_is_sanitized_and_logged():
    payload = {
        "page": "settings",
        "route": "/settings?token=secret",
        "duration_ms": 1500.6,
        "api_count": 5,
        "slow_api_count": 2,
        "max_api_duration_ms": 900.2,
        "extra": "ignored",
    }

    with patch.object(performance, "append_business_log") as log:
        result = performance.log_frontend_page_event(payload)

    assert result == {"success": True, "recorded": True}
    log.assert_called_once()
    args, kwargs = log.call_args
    assert args[:3] == ("performance", "slow_page_load", "页面首屏加载较慢。")
    assert kwargs["page"] == "settings"
    assert kwargs["route"] == "/settings"
    assert kwargs["duration_ms"] == 1500.6
    assert "secret" not in str(kwargs)


def test_frontend_page_milestones_are_whitelisted_clamped_and_keep_duration_compatibility():
    payload = {
        "page": "models",
        "route": "/models?token=secret",
        "duration_ms": 1500.6,
        "data_ready_ms": -1,
        "enrichment_ready_ms": 999999,
        "max_ttfb_ms": 999999,
        "max_parse_ms": "42.34",
        "max_total_ms": 1000.06,
        "request_body": "secret",
        "query": "token=secret",
    }

    with patch.object(performance, "append_business_log") as log:
        result = performance.log_frontend_page_event(payload)

    assert result == {"success": True, "recorded": True}
    _args, kwargs = log.call_args
    assert kwargs["duration_ms"] == 1500.6
    assert kwargs["data_ready_ms"] == 0.0
    assert kwargs["enrichment_ready_ms"] == 600000.0
    assert kwargs["max_ttfb_ms"] == 600000.0
    assert kwargs["max_parse_ms"] == 42.3
    assert kwargs["max_total_ms"] == 1000.1
    assert "secret" not in str(kwargs)


def test_frontend_enrichment_event_is_logged_separately_from_slow_page_load():
    payload = {
        "page": "tasks",
        "route": "/tasks?token=secret",
        "event_kind": "enrichment",
        "duration_ms": 1600,
        "data_ready_ms": 0,
        "enrichment_ready_ms": 1600,
    }

    with patch.object(performance, "append_business_log") as log:
        result = performance.log_frontend_page_event(payload)

    assert result == {"success": True, "recorded": True}
    args, kwargs = log.call_args
    assert args[:3] == ("performance", "slow_page_enrichment", "页面补全数据加载较慢。")
    assert kwargs["route"] == "/tasks"
    assert "secret" not in str(kwargs)
