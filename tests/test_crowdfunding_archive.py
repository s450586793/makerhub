import json
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.services import archive_worker, legacy_archiver, three_mf
from app.services.makerworld_pipeline import archive
from app.services.remote_refresh import _build_missing_3mf_items, _merge_instance_record


@pytest.mark.parametrize("signal", [
    {"paidSetting": {"crowdfunding": 1}},
    {"paidSetting": {"crowdfunding": "1"}},
    {"crowdfundingInfo": {"projectId": 272}},
    {"threeMfSkipReason": "crowdfunding"},
])
def test_crowdfunding_design_is_excluded_from_missing_rebuild(signal, tmp_path):
    meta = {"id": 2888275, "url": "https://makerworld.com/en/models/2888275",
            "instances": [{"id": 3226767, "downloadState": "http_error"}], **signal}
    assert three_mf.is_three_mf_download_prohibited(meta)
    assert _build_missing_3mf_items(tmp_path / "meta.json", meta, {"matches": {}}) == []


@pytest.mark.parametrize("signal", [
    {}, {"paidSetting": {"crowdfunding": 0}}, {"paidSetting": {"crowdfunding": "false"}},
    {"crowdfundingInfo": {}}, {"crowdfundingInfo": {"projectId": 0}},
    {"summary": "See my crowdfunding project"},
])
def test_ordinary_design_is_not_excluded_by_unrelated_crowdfunding_text(signal):
    assert not three_mf.is_three_mf_download_prohibited(signal)


@pytest.mark.parametrize("deferred", [False, True])
def test_crowdfunding_archive_preserves_metadata_and_assets_without_quota(deferred, tmp_path):
    design = {
        "id": 2888275, "title": "Crowdfunding demo", "paidSetting": {"crowdfunding": 1},
        "summary": "<p>Assembly instructions</p>", "coverUrl": "https://cdn.example.test/cover.jpg",
        "instances": [
            {"id": 3226767, "title": "Main profile", "cover": "https://cdn.example.test/profile.jpg",
             "downloadUrl": "https://cdn.example.test/forbidden.3mf"},
            {"id": 3226768, "title": "Alternate profile"},
        ],
    }
    html = '<script id="__NEXT_DATA__">' + json.dumps({"props": {"pageProps": {"design": design}}}) + '</script>'
    downloads = []

    def download(_session, url, dest, **_kwargs):
        assert not str(url).endswith(".3mf")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"image fixture")
        downloads.append(url)

    with patch.object(legacy_archiver, "fetch_html_with_browser", return_value=html), \
            patch.object(legacy_archiver, "reserve_three_mf_download_slot", side_effect=AssertionError("quota reserved")), \
            patch.object(legacy_archiver, "fetch_instance_3mf", side_effect=AssertionError("3MF authorized")), \
            patch.object(archive, "download_file", side_effect=download), \
            patch.object(legacy_archiver, "download_file", side_effect=download):
        result = legacy_archiver.archive_model(
            "https://makerworld.com/en/models/2888275", "", tmp_path / "archive", tmp_path / "logs",
            collect_comments_data=False, skip_three_mf_fetch=deferred, rebuild_archive=True,
        )
    root = Path(result["work_dir"])
    meta = json.loads((root / "meta.json").read_text())
    assert meta["title"] == "Crowdfunding demo"
    assert "Assembly instructions" in meta["summary"]["text"]
    assert [i["title"] for i in meta["instances"]] == ["Main profile", "Alternate profile"]
    assert len(downloads) >= 2
    assert list((root / "images").iterdir())
    assert meta["threeMfDownloadAllowed"] is False
    assert meta["threeMfSkipReason"] == "crowdfunding"
    assert all(i["downloadState"] == "not_downloadable" and not i["downloadUrl"] for i in meta["instances"])
    assert all("众筹" in i["downloadMessage"] for i in meta["instances"])
    assert result["missing_3mf"] == []
    assert result["three_mf_skip_reason"] == "crowdfunding"
    assert result["stats"]["instances"]["api_fetch_attempts"] == 0
    assert result["stats"]["instances"]["three_mf_downloaded"] == 0
    assert _build_missing_3mf_items(root / "meta.json", meta, {"matches": {}}) == []


def test_browser_crowdfunding_result_is_not_a_transient_error():
    with patch.object(legacy_archiver, "browser_authorize_3mf_download", return_value={
        "status_code": 200, "payload": {"code": "MAKERHUB_CROWDFUNDING"},
    }):
        _name, url, _api, failure = legacy_archiver.fetch_instance_3mf(
            SimpleNamespace(cookies={}), 3226767, "", origin="https://makerworld.com",
            browser_authorization=True, browser_profile_id="profile-global",
            model_page_url="https://makerworld.com/en/models/2888275",
        )
    assert not url
    assert failure["state"] == "not_downloadable"
    assert failure["reason"] == "crowdfunding"


@pytest.mark.parametrize("retry", [False, True])
def test_worker_finishes_crowdfunding_without_child_or_account_gate_change(retry):
    manager = archive_worker.ArchiveTaskManager(background_enabled=False)
    manager.store = SimpleNamespace(load=lambda: SimpleNamespace(cookies=[], proxy=None, three_mf_limits=None))
    completed, pending, logs = [], [], []
    manager.task_store = SimpleNamespace(
        update_missing_3mf_status=lambda **kwargs: None,
        replace_missing_3mf_for_model=lambda model_id, items: pending.append(items),
        remove_recent_failures_for_model=lambda *args, **kwargs: None,
        update_active_task=lambda *args, **kwargs: None,
        complete_archive_task=lambda task_id, **kwargs: completed.append(kwargs),
    )
    result = {"model_id": "2888275", "base_name": "Crowdfunding demo", "work_dir": "",
              "instances": [{"id": 3226767, "downloadState": "not_downloadable"}],
              "missing_3mf": [], "three_mf_skip_reason": "crowdfunding"}
    with ExitStack() as stack:
        for name, value in {
            "_select_cookie": "cookie", "_read_three_mf_limit_guard": {"active": False},
            "_is_three_mf_limit_guard_active_for_url": False, "run_archive_model_job": result,
            "invalidate_model_detail_cache": None, "upsert_archive_snapshot_model": True,
            "invalidate_archive_snapshot": None,
        }.items():
            stack.enter_context(patch.object(archive_worker, name, return_value=value))
        stack.enter_context(patch.object(archive_worker, "_temporary_proxy_env", side_effect=lambda *a, **k: nullcontext()))
        gate = stack.enter_context(patch.object(archive_worker, "mark_account_ok"))
        child = stack.enter_context(patch.object(manager, "_enqueue_three_mf_stage_task_from_result", return_value="unexpected"))
        stack.enter_context(patch.object(archive_worker, "_log_archive", side_effect=lambda *a, **k: logs.append((a, k))))
        manager._run_single_task("task", "https://makerworld.com/en/models/2888275", {"missing_3mf_retry": retry})
    assert pending == [[]]
    assert completed[0]["progress"] == 100
    assert "众筹" in completed[0]["message"]
    assert "跳过" in completed[0]["message"]
    assert "已入队" not in completed[0]["message"]
    child.assert_not_called()
    gate.assert_not_called()


def test_three_mf_stage_filters_prohibited_instances():
    manager = archive_worker.ArchiveTaskManager(background_enabled=False)
    manager._enqueue_single_task = Mock(return_value="child")
    with patch.object(archive_worker, "_log_archive"):
        result = manager._enqueue_three_mf_stage_task_from_result("https://makerworld.com/en/models/1", {
            "instances": [{"id": 10, "downloadState": "not_downloadable"}],
        }, {})
    assert result == ""
    manager._enqueue_single_task.assert_not_called()


def test_source_refresh_keeps_crowdfunding_skip_instead_of_restoring_old_error():
    old = {"id": 1, "fileName": "existing.3mf", "downloadState": "http_error",
           "downloadMessage": "timeout", "downloadUrl": "https://cdn.example.test/stale.3mf"}
    fresh = {"id": 1, "downloadState": "not_downloadable", "downloadMessage": "众筹模型已跳过 3MF 下载。",
             "threeMfSkipReason": "crowdfunding", "downloadUrl": ""}
    merged = _merge_instance_record(old, fresh)
    assert merged["fileName"] == "existing.3mf"
    assert merged["downloadState"] == "not_downloadable"
    assert not merged["downloadUrl"]
    assert "众筹" in merged["downloadMessage"]


def test_rebuild_does_not_download_old_crowdfunding_urls_or_remove_existing_files(tmp_path):
    root = tmp_path / "MW_2888275_Demo"
    (root / "instances").mkdir(parents=True)
    saved = root / "instances" / "existing.3mf"
    saved.write_bytes(b"previous download")
    meta = {"id": 2888275, "baseName": root.name, "threeMfSkipReason": "crowdfunding", "instances": [
        {"id": 1, "fileName": "existing.3mf", "downloadUrl": "https://cdn.example.test/existing.3mf"},
        {"id": 2, "fileName": "missing.3mf", "downloadUrl": "https://cdn.example.test/missing.3mf"},
    ]}
    meta_path = root / "meta.json"
    meta_path.write_text(json.dumps(meta))
    with patch.object(legacy_archiver, "_download_three_mf_file", side_effect=AssertionError("3MF download attempted")) as download:
        legacy_archiver.rebuild_once(meta_path)
    download.assert_not_called()
    assert saved.read_bytes() == b"previous download"
    assert not (root / "instances" / "missing.3mf").exists()


def test_crowdfunding_browser_fallback_skips_remaining_profiles(tmp_path):
    design = {"id": 2888275, "title": "Demo", "instances": [{"id": 1}, {"id": 2}]}
    html = '<script id="__NEXT_DATA__">' + json.dumps({"props": {"pageProps": {"design": design}}}) + '</script>'
    with patch.object(legacy_archiver, "fetch_html_with_browser", return_value=html), \
            patch.object(legacy_archiver, "reserve_three_mf_download_slot", return_value={"allowed": True}), \
            patch.object(legacy_archiver, "fetch_instance_3mf", return_value=(
                "", "", "", {"state": "not_downloadable", "reason": "crowdfunding", "message": "众筹模型"},
            )) as authorize:
        result = legacy_archiver.archive_model(
            "https://makerworld.com/en/models/2888275", "", tmp_path / "archive", tmp_path / "logs",
            download_assets=False, collect_comments_data=False, rebuild_archive=False,
        )
    assert authorize.call_count == 1
    assert result["missing_3mf"] == []
    assert result["three_mf_skip_reason"] == "crowdfunding"
    assert len(result["instances"]) == 2
    assert all(i["downloadState"] == "not_downloadable" for i in result["instances"])
