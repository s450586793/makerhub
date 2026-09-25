import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests

from app.services.asset_downloader import AssetDownloadError
from app.services.makerworld_pipeline import archive_model
from app.services.makerworld_pipeline.archive import fetch_instance_3mf
from app.services.task_state import _normalize_archive_queue
from app.services import archive_worker
from app.services import process_jobs
from app.services.task_state import _normalize_missing_3mf


@pytest.fixture
def saved_model(tmp_path):
    root = tmp_path / "archive"
    model = root / "MW_123_saved"
    (model / "instances").mkdir(parents=True)
    (model / "images").mkdir()
    (model / "images" / "cover.jpg").write_bytes(b"saved-image")
    meta = {
        "id": 123, "title": "Saved title", "baseName": model.name,
        "url": "https://makerworld.com.cn/zh/models/123", "collectDate": 100,
        "summary": {"html": "saved summary"}, "comments": [{"id": "comment-1"}],
        "attachments": [{"url": "https://cdn.example.test/manual.pdf", "relPath": "file/manual.pdf"}],
        "instances": [
            {"id": 456, "title": "Missing", "fileName": "missing.3mf", "name": "missing.3mf",
             "downloadUrl": "https://cdn.example.test/old.3mf?signature=secret",
             "downloadState": "http_error", "downloadMessage": "transfer timed out"},
            {"id": 789, "title": "Saved", "fileName": "saved.3mf", "downloadUrl": ""},
        ],
    }
    (model / "instances" / "saved.3mf").write_bytes(b"saved-3mf")
    (model / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return model, meta


def run_retry(model, **kwargs):
    with (
        patch("app.services.makerworld_pipeline.archive.fetch_html_with_browser", side_effect=AssertionError("must not refetch metadata")),
        patch("app.services.makerworld_pipeline.archive.rebuild_once", side_effect=AssertionError("must not rebuild unrelated assets")),
    ):
        return archive_model(
            url="https://makerworld.com.cn/zh/models/123", cookie="token=ok",
            download_dir=model.parent, existing_root=model.parent, logs_dir=model.parent / "logs",
            three_mf_only=True, record_missing_3mf_log=False, **kwargs,
        )


def test_retry_reuses_failed_transfer_url_and_preserves_saved_resources(saved_model):
    model, before = saved_model
    urls = []

    def transfer(url, dest, **kwargs):
        urls.append(url)
        dest.write_bytes(b"downloaded-3mf")

    with (
        patch("app.services.makerworld_pipeline.archive._download_three_mf_file", side_effect=transfer, create=True),
        patch("app.services.makerworld_pipeline.archive.fetch_instance_3mf", side_effect=AssertionError("must reuse signed URL")),
        patch("app.services.makerworld_pipeline.archive.reserve_three_mf_download_slot", side_effect=AssertionError("no new authorization")),
    ):
        result = run_retry(model)

    after = json.loads((model / "meta.json").read_text())
    assert {k: v for k, v in after.items() if k != "instances"} == {k: v for k, v in before.items() if k != "instances"}
    assert (model / "instances" / "saved.3mf").read_bytes() == b"saved-3mf"
    assert (model / "images" / "cover.jpg").read_bytes() == b"saved-image"
    assert urls == [before["instances"][0]["downloadUrl"]]
    assert result["missing_3mf"] == []
    assert result["stats"]["instances"]["api_fetch_attempts"] == 0


@pytest.mark.parametrize("status", [401, 403, 404, 410])
def test_retry_refreshes_rejected_direct_link_once(saved_model, status):
    model, before = saved_model
    urls = []
    fresh = "https://cdn.example.test/fresh.3mf?signature=new"

    def transfer(url, dest, **kwargs):
        urls.append(url)
        if len(urls) == 1:
            raise AssetDownloadError("HTTP failure", status_code=status)
        dest.write_bytes(b"new-3mf")

    with (
        patch("app.services.makerworld_pipeline.archive._download_three_mf_file", side_effect=transfer, create=True),
        patch("app.services.makerworld_pipeline.archive.reserve_three_mf_download_slot", return_value={"allowed": True}),
        patch("app.services.makerworld_pipeline.archive.fetch_instance_3mf", return_value=("missing.3mf", fresh, "", {"state": "available"})) as authorize,
    ):
        result = run_retry(model)
    assert urls == [before["instances"][0]["downloadUrl"], fresh]
    assert result["missing_3mf"] == []
    assert authorize.call_count == 1


@pytest.mark.parametrize("status", [0, 429, 500])
def test_retry_keeps_url_on_transient_failure_without_reauthorizing(saved_model, status):
    model, before = saved_model
    with (
        patch("app.services.makerworld_pipeline.archive._download_three_mf_file", side_effect=AssetDownloadError("temporary failure", status_code=status), create=True),
        patch("app.services.makerworld_pipeline.archive.fetch_instance_3mf", side_effect=AssertionError("no new authorization")),
    ):
        result = run_retry(model)
    assert len(result["missing_3mf"]) == 1
    assert result["missing_3mf"][0]["downloadUrl"] == before["instances"][0]["downloadUrl"]
    assert result["missing_3mf"][0]["downloadState"] == "http_error"


def test_retry_only_selected_instance_keeps_other_records(saved_model):
    model, before = saved_model
    with patch("app.services.makerworld_pipeline.archive.fetch_instance_3mf", side_effect=AssertionError("already on disk")):
        result = run_retry(model, instance_ids=["789"])
    after = json.loads((model / "meta.json").read_text())
    assert after["instances"][0] == before["instances"][0]
    assert len(after["instances"]) == 2
    assert [item["id"] for item in result["missing_3mf"]] == [456]


def test_retry_missing_local_metadata_fails_without_crawling(saved_model):
    model, _ = saved_model
    (model / "meta.json").unlink()
    with pytest.raises(RuntimeError, match="本地.*重新归档"):
        run_retry(model)


def test_retry_obeys_verification_gate_without_downloading(saved_model):
    model, _ = saved_model
    with patch("app.services.makerworld_pipeline.archive._download_three_mf_file", side_effect=AssertionError("gate closed"), create=True):
        result = run_retry(model, skip_three_mf_fetch=True, three_mf_skip_state="cloudflare", three_mf_skip_message="需要验证")
    assert result["missing_3mf"][0]["downloadState"] == "cloudflare"


@pytest.mark.parametrize("flag", ["missing_3mf_retry", "three_mf_download"])
def test_retry_queue_only_shows_download_and_finalize_stages(flag):
    queue = _normalize_archive_queue({"queued": [{
        "id": "retry", "url": "https://makerworld.com.cn/zh/models/123",
        "mode": "single_model", "status": "queued", "meta": {flag: True},
    }]})
    assert [item["type"] for item in queue["queued"][0]["subtasks"]] == ["three_mf", "finalize"]


def test_cloudflare_page_result_is_verification_not_generic_http_error():
    with patch("app.services.makerworld_pipeline.archive.browser_authorize_3mf_download", return_value={
        "status_code": 403, "payload": {"code": "MAKERHUB_CLOUDFLARE"},
    }):
        _, url, _, failure = fetch_instance_3mf(
            requests.Session(), 456, "", origin="https://makerworld.com.cn",
            browser_authorization=True, browser_profile_id="cn",
            model_page_url="https://makerworld.com.cn/zh/models/123",
        )
    assert url == ""
    assert failure["state"] == "cloudflare"
    assert "验证" in failure["message"]


@pytest.mark.parametrize("earlier_success", [False, True])
def test_cloudflare_interstitial_closes_gate_even_for_first_model(earlier_success):
    with (
        patch.object(archive_worker, "get_account_health", return_value={"three_mf_gate": "open"}),
        patch.object(archive_worker, "mark_account_ok") as mark_ok,
        patch.object(archive_worker, "update_three_mf_gate") as gate,
    ):
        failure = archive_worker._sync_account_health_for_archive_result(
            platform="cn", model_url="https://makerworld.com.cn/zh/models/123",
            model_id="123", instance_id="456", missing_3mf_retry=True,
            three_mf_authorization_succeeded=earlier_success,
            missing_items=[{"status": "cloudflare", "message": "需要浏览器验证", "instance_id": "456"}],
        )
    assert failure and failure["status"] == "cloudflare"
    assert gate.call_args.kwargs["gate"] == "cloudflare"
    mark_ok.assert_not_called()


def test_worker_does_not_sync_browser_before_direct_file_retry():
    manager = archive_worker.ArchiveTaskManager(background_enabled=False)
    manager.store = SimpleNamespace(load=lambda: SimpleNamespace(cookies=[SimpleNamespace(
        platform="cn", cookie="", browser_profile_id="profile-cn",
    )], proxy=None, three_mf_limits=None))
    manager.task_store = Mock()
    with (
        patch.object(manager, "_refresh_browser_session_for_task", side_effect=AssertionError("static file needs no browser")),
        patch.object(archive_worker, "_select_cookie", return_value=""),
        patch.object(archive_worker, "_read_three_mf_limit_guard", return_value={}),
        patch.object(archive_worker, "three_mf_gate_for_url", return_value={"open": True}),
        patch.object(archive_worker, "_temporary_proxy_env", return_value=nullcontext()),
        patch.object(archive_worker, "run_archive_model_job", return_value={
            "model_id": "123", "instances": [],
            "missing_3mf": [{"id": "789", "downloadState": "cloudflare", "downloadMessage": "old failure"}],
            "stats": {"instances": {"processed_instance_ids": ["456"]}},
        }) as job,
        patch.object(archive_worker, "_sync_account_health_for_archive_result") as sync_health,
        patch.object(archive_worker, "invalidate_archive_snapshot"),
        patch.object(archive_worker, "_log_archive"),
    ):
        manager._run_single_task("task", "https://makerworld.com.cn/zh/models/123", {
            "missing_3mf_retry": True, "instance_id": "456",
        })
    assert job.call_args.kwargs["three_mf_only"] is True
    assert job.call_args.kwargs["instance_ids"] == ["456"]
    assert sync_health.call_args.kwargs["missing_items"] == []


def test_retry_persists_new_authorization_before_transfer_can_be_interrupted(saved_model):
    model, before = saved_model
    before["instances"][0]["downloadUrl"] = ""
    (model / "meta.json").write_text(json.dumps(before))
    fresh = "https://cdn.example.test/fresh.3mf?signature=new"
    with (
        patch("app.services.makerworld_pipeline.archive._download_three_mf_file", side_effect=KeyboardInterrupt),
        patch("app.services.makerworld_pipeline.archive.reserve_three_mf_download_slot", return_value={"allowed": True}),
        patch("app.services.makerworld_pipeline.archive.fetch_instance_3mf", return_value=("missing.3mf", fresh, "", {"state": "available"})),
        pytest.raises(KeyboardInterrupt),
    ):
        run_retry(model)
    assert json.loads((model / "meta.json").read_text())["instances"][0]["downloadUrl"] == fresh


def test_retry_stops_authorizations_after_cloudflare(saved_model):
    model, before = saved_model
    before["instances"][0]["downloadUrl"] = ""
    (model / "instances" / "saved.3mf").unlink()
    (model / "meta.json").write_text(json.dumps(before))
    with (
        patch("app.services.makerworld_pipeline.archive.reserve_three_mf_download_slot", return_value={"allowed": True}),
        patch("app.services.makerworld_pipeline.archive.fetch_instance_3mf", return_value=("", "", "", {"state": "cloudflare", "message": "需要验证"})) as authorize,
    ):
        result = run_retry(model)
    assert authorize.call_count == 1
    assert [item["downloadState"] for item in result["missing_3mf"]] == ["cloudflare", "cloudflare"]


def test_missing_list_keeps_cloudflare_guidance_without_requesting_a_download():
    with patch("app.services.task_state.load_database_json_state", return_value={}):
        state = _normalize_missing_3mf([{"model_id": "123", "status": "cloudflare", "message": "403"}])
    message = state["items"][0]["message"]
    assert "Cloudflare" in message
    assert "已验证" in message
    assert "任意下载" not in message


@pytest.mark.parametrize("subprocess", [False, True])
def test_process_job_keeps_three_mf_only_mode(saved_model, monkeypatch, subprocess):
    model, _ = saved_model
    monkeypatch.delenv("MAKERHUB_CLOAKBROWSER_URL", raising=False)
    monkeypatch.delenv("MAKERHUB_CLOAKBROWSER_AUTH_TOKEN", raising=False)
    with patch.object(process_jobs, "_use_subprocess", return_value=subprocess):
        result = process_jobs.run_archive_model_job(
            url="https://makerworld.com.cn/zh/models/123", cookie="",
            download_dir=str(model.parent), existing_root=str(model.parent), logs_dir=str(model.parent / "logs"),
            instance_ids=["789"], three_mf_only=True, record_missing_3mf_log=False,
        )
    assert len(result["instances"]) == 2
    assert [item["id"] for item in result["missing_3mf"]] == [456]
