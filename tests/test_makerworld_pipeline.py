from pathlib import Path
from unittest.mock import patch

from app.services.makerworld_browser_client import (
    MakerWorldBrowserError,
    MakerWorldBrowserResponse,
)
from app.services.makerworld_parsers import common as parser_common
from app.services.makerworld_pipeline import (
    archive_model,
    discover_source,
    source_is_deleted,
)
from app.services import batch_discovery


def test_batch_discovery_facade_preserves_url_helper_identity():
    assert getattr(batch_discovery, "normalize_source_url", None) is parser_common.normalize_source_url
    assert getattr(batch_discovery, "normalize_model_url", None) is parser_common.normalize_model_url
    assert getattr(batch_discovery, "extract_model_id", None) is parser_common.extract_model_id


def test_source_is_deleted_uses_browser_response():
    response = MakerWorldBrowserResponse(
        url="https://makerworld.com.cn/404",
        status_code=404,
        content_type="text/html",
        text="not found",
        profile_id="cn-profile",
    )

    with patch(
        "app.services.makerworld_pipeline.source_status.makerworld_browser_get",
        return_value=response,
    ) as browser_get:
        assert source_is_deleted("https://makerworld.com.cn/zh/models/1", "token=ok") is True

    browser_get.assert_called_once()


def test_source_is_deleted_treats_browser_outage_as_unknown_not_deleted():
    with patch(
        "app.services.makerworld_pipeline.source_status.makerworld_browser_get",
        side_effect=MakerWorldBrowserError("暂不可用"),
    ):
        assert source_is_deleted("https://makerworld.com.cn/zh/models/1", "token=ok") is False


def test_archive_model_delegates_to_legacy_archiver():
    kwargs = {
        "url": "https://makerworld.com.cn/zh/models/1",
        "cookie": "token=ok",
        "download_dir": Path("/tmp/archive"),
        "logs_dir": Path("/tmp/logs"),
    }
    expected = {"ok": True}

    with patch(
        "app.services.makerworld_pipeline.legacy_archive_model",
        return_value=expected,
    ) as legacy_archive_model:
        assert archive_model(**kwargs) == expected

    legacy_archive_model.assert_called_once_with(**kwargs)


def test_discover_source_fetches_each_candidate_once_and_keeps_result_shape():
    payload = {"hits": [{"id": 1001, "title": "A"}], "total": 1}

    with patch(
        "app.services.makerworld_pipeline.discovery.makerworld_browser_get_json",
        return_value=payload,
    ) as fetch, patch(
        "app.services.makerworld_pipeline.discovery._resolve_author_uid",
        return_value="100",
    ):
        result = discover_source(
            "https://makerworld.com.cn/zh/@ace/upload",
            "token=ok",
            max_pages=1,
        )

    assert set(("items", "expected_total", "mode")).issubset(result)
    assert result["expected_total"] == 1
    assert fetch.call_count >= 1

    with patch(
        "app.services.makerworld_pipeline.discovery.makerworld_browser_get_json",
        return_value=payload,
    ), patch(
        "app.services.makerworld_pipeline.discovery._resolve_author_uid",
        return_value="100",
    ):
        assert batch_discovery.discover_batch_model_urls(
            "https://makerworld.com.cn/zh/@ace/upload",
            "token=ok",
            max_pages=1,
        ) == result
