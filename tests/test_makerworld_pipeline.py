from pathlib import Path
from unittest.mock import patch

from app.services.makerworld_browser_client import (
    MakerWorldBrowserError,
    MakerWorldBrowserResponse,
)
from app.services.makerworld_pipeline import (
    archive_model,
    discover_source,
    source_is_deleted,
)


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


def test_discover_source_delegates_to_legacy_discovery():
    expected = {"items": []}

    with patch(
        "app.services.makerworld_pipeline.discover_batch_model_urls",
        return_value=expected,
    ) as discover_batch_model_urls:
        assert discover_source("https://makerworld.com.cn/zh/@ace/upload", "token=ok", 3) == expected

    discover_batch_model_urls.assert_called_once_with(
        "https://makerworld.com.cn/zh/@ace/upload",
        "token=ok",
        max_pages=3,
    )
