import json
from pathlib import Path
from unittest.mock import patch

import requests

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
from app.services.makerworld_pipeline.archive import fetch_instance_3mf


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


def test_archive_pipeline_keeps_result_contract(tmp_path):
    design = {"id": 123, "title": "Demo", "instances": []}
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"props": {"pageProps": {"design": design}}})
        + "</script>"
    )
    with patch(
        "app.services.makerworld_pipeline.archive.fetch_html_with_browser",
        return_value=html,
    ), patch(
        "app.services.makerworld_pipeline.archive.reserve_three_mf_download_slot",
        side_effect=AssertionError("no instance"),
    ):
        result = archive_model(
            url="https://makerworld.com.cn/zh/models/123",
            cookie="",
            download_dir=tmp_path / "archive",
            logs_dir=tmp_path / "logs",
            download_assets=False,
            collect_comments_data=False,
            rebuild_archive=False,
        )

    assert set(result) == {
        "base_name",
        "work_dir",
        "missing_3mf",
        "action",
        "model_id",
        "instances",
        "stats",
    }


def test_archive_pipeline_calls_browser_authorizer_at_most_once_per_instance():
    browser_result = {
        "status_code": 200,
        "text": "",
        "payload": {"name": "demo.3mf", "url": "https://cdn.example.test/demo.3mf?signature=ok"},
        "verification": {},
    }
    with patch(
        "app.services.makerworld_pipeline.archive.browser_authorize_3mf_download",
        return_value=browser_result,
    ) as authorize:
        name, signed_url, _api_url, failure = fetch_instance_3mf(
            requests.Session(),
            456,
            "",
            api_url="https://api.bambulab.cn/v1/i/456/3mf",
            origin="https://makerworld.com.cn",
            browser_authorization=True,
            browser_profile_id="profile-cn",
            model_page_url="https://makerworld.com.cn/zh/models/123",
        )

    authorize.assert_called_once()
    assert name == "demo.3mf"
    assert signed_url.startswith("https://cdn.example.test/demo.3mf")
    assert failure["state"] == "available"


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
