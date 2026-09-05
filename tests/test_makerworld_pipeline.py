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
from app.services import batch_discovery, legacy_archiver
from app.services.makerworld_pipeline import archive as pipeline_archive
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


def test_archive_pipeline_default_comments_path_has_no_missing_globals(tmp_path):
    design = {"id": 124, "title": "Comments", "instances": []}
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"props": {"pageProps": {"design": design}}})
        + "</script>"
    )
    with patch(
        "app.services.makerworld_pipeline.archive.fetch_html_with_browser",
        return_value=html,
    ), patch(
        "app.services.makerworld_pipeline.archive.makerworld_browser_get_json",
        return_value={"total": 0, "hits": []},
    ) as browser_json:
        result = archive_model(
            url="https://makerworld.com.cn/zh/models/124",
            cookie="",
            download_dir=tmp_path / "archive",
            logs_dir=tmp_path / "logs",
            download_assets=False,
            rebuild_archive=False,
        )

    assert browser_json.call_count >= 1
    assert result["stats"]["comments"]["download_tasks"] == 0


def test_archive_pipeline_downloads_comment_assets_without_missing_globals(tmp_path):
    avatar_url = "https://cdn.example.test/avatar.png?width=96&signature=secret"
    design = {"id": 125, "title": "Comment assets", "instances": []}
    next_data = {
        "comments": [
            {
                "commentId": "comment-1",
                "commentContent": "hello",
                "user": {"nickname": "A", "avatarUrl": avatar_url},
            }
        ],
        "props": {"pageProps": {"design": design}},
    }
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(next_data)
        + "</script>"
    )

    def write_asset(_session, _url, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"avatar")

    with patch(
        "app.services.makerworld_pipeline.archive.fetch_html_with_browser",
        return_value=html,
    ), patch(
        "app.services.makerworld_pipeline.archive.makerworld_browser_get_json",
        return_value={"total": 0, "hits": []},
    ), patch(
        "app.services.makerworld_pipeline.archive._download_asset_with_fresh_session",
        side_effect=write_asset,
    ) as download_asset:
        result = archive_model(
            url="https://makerworld.com.cn/zh/models/125",
            cookie="",
            download_dir=tmp_path / "archive",
            logs_dir=tmp_path / "logs",
            download_assets=False,
            download_comment_assets=True,
            rebuild_archive=False,
        )

    download_asset.assert_called_once()
    assert result["stats"]["comments"]["download_completed"] == 1
    assert list((tmp_path / "archive" / "_shared" / "avatars").iterdir())


def test_archive_pipeline_reuses_existing_design_media(tmp_path):
    archive_root = tmp_path / "archive"
    model_dir = archive_root / "MW_126_Existing"
    images_dir = model_dir / "images"
    images_dir.mkdir(parents=True)
    existing_image = images_dir / "design_01.jpg"
    existing_image.write_bytes(b"existing")
    image_url = "https://cdn.example.test/design.jpg?width=1600&signature=secret"
    (model_dir / "meta.json").write_text(
        json.dumps(
            {
                "author": {},
                "designImages": [
                    {
                        "index": 1,
                        "originalUrl": image_url,
                        "relPath": "images/design_01.jpg",
                        "fileName": "design_01.jpg",
                    }
                ],
                "instances": [],
                "stats": {},
            }
        ),
        encoding="utf-8",
    )
    design = {"id": 126, "title": "Renamed", "images": [{"url": image_url}], "instances": []}
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"props": {"pageProps": {"design": design}}})
        + "</script>"
    )

    with patch(
        "app.services.makerworld_pipeline.archive.fetch_html_with_browser",
        return_value=html,
    ), patch(
        "app.services.makerworld_pipeline.archive.download_file",
        side_effect=AssertionError("existing media must be reused"),
    ), patch.object(
        legacy_archiver,
        "download_file",
        side_effect=AssertionError("existing media must be reused"),
    ):
        result = archive_model(
            url="https://makerworld.com.cn/zh/models/126",
            cookie="",
            download_dir=archive_root,
            logs_dir=tmp_path / "logs",
            existing_root=archive_root,
            existing_model_dir=model_dir.name,
            download_assets=True,
            collect_comments_data=False,
            rebuild_archive=False,
        )

    meta = json.loads((Path(result["work_dir"]) / "meta.json").read_text(encoding="utf-8"))
    assert result["action"] == "updated"
    assert existing_image.read_bytes() == b"existing"
    assert meta["designImages"][0]["relPath"] == "images/design_01.jpg"


def test_archive_pipeline_fetches_3mf_without_browser_authorization(tmp_path):
    design = {"id": 127, "title": "Direct 3MF", "instances": [{"id": 456, "title": "Profile"}]}
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
        return_value={"allowed": True},
    ), patch(
        "app.services.makerworld_pipeline.archive._wait_before_three_mf_download",
        return_value=0,
    ), patch(
        "app.services.makerworld_pipeline.archive.makerworld_browser_get_json",
        return_value={"name": "profile.3mf", "url": "https://cdn.example.test/profile.3mf?signature=ok"},
    ) as browser_json, patch(
        "app.services.makerworld_pipeline.archive.browser_authorize_3mf_download",
    ) as browser_authorize:
        result = archive_model(
            url="https://makerworld.com.cn/zh/models/127",
            cookie="",
            download_dir=tmp_path / "archive",
            logs_dir=tmp_path / "logs",
            download_assets=False,
            collect_comments_data=False,
            rebuild_archive=False,
            browser_three_mf_authorization=False,
        )

    browser_json.assert_called_once()
    browser_authorize.assert_not_called()
    assert result["instances"][0]["downloadUrl"].startswith("https://cdn.example.test/profile.3mf")


def test_legacy_archive_facade_injects_comment_and_3mf_patch_points(tmp_path):
    design = {"id": 128, "title": "Legacy patches", "instances": [{"id": 789, "title": "Profile"}]}
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"props": {"pageProps": {"design": design}}})
        + "</script>"
    )
    with patch.object(legacy_archiver, "fetch_html_with_browser", return_value=html) as fetch_html, patch.object(
        legacy_archiver,
        "collect_comments",
        return_value={"count": 0, "items": [], "assetStats": {}},
    ) as comments, patch.object(
        legacy_archiver,
        "reserve_three_mf_download_slot",
        return_value={"allowed": True},
    ) as reserve_slot, patch.object(
        legacy_archiver,
        "fetch_instance_3mf",
        return_value=("legacy.3mf", "https://cdn.example.test/legacy.3mf", "https://api.example.test", {"state": "available", "message": ""}),
    ) as fetch_3mf:
        result = legacy_archiver.archive_model(
            url="https://makerworld.com.cn/zh/models/128",
            cookie="",
            download_dir=tmp_path / "archive",
            logs_dir=tmp_path / "logs",
            download_assets=False,
            rebuild_archive=False,
        )

    fetch_html.assert_called_once()
    comments.assert_called_once()
    reserve_slot.assert_called_once()
    fetch_3mf.assert_called_once()
    assert result["instances"][0]["downloadUrl"] == "https://cdn.example.test/legacy.3mf"


def test_archive_dependency_overrides_are_call_scoped():
    first_fetch = lambda *_args, **_kwargs: "first"
    second_fetch = lambda *_args, **_kwargs: "second"

    first = pipeline_archive.archive_dependencies_with_overrides(fetch_html_with_browser=first_fetch)
    second = pipeline_archive.archive_dependencies_with_overrides(fetch_html_with_browser=second_fetch)

    assert first.fetch_html_with_browser is first_fetch
    assert second.fetch_html_with_browser is second_fetch
    assert pipeline_archive.DEFAULT_ARCHIVE_DEPENDENCIES.fetch_html_with_browser is not first_fetch
    assert pipeline_archive.DEFAULT_ARCHIVE_DEPENDENCIES.fetch_html_with_browser is not second_fetch


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
