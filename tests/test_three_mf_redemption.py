import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.timezone import now
from app.services import archive_worker, legacy_archiver, remote_refresh, three_mf
from app.services.makerworld_pipeline import archive
from app.services.task_state import _normalize_missing_3mf


MESSAGE = "该模型需要先在 MakerWorld 兑换，完成兑换后可手动重新下载。"


@pytest.mark.parametrize("module", [archive, legacy_archiver])
def test_browser_redemption_result_is_model_specific_and_not_an_account_failure(module):
    with patch.object(module, "browser_authorize_3mf_download", return_value={
        "status_code": 402, "payload": {"code": "MAKERHUB_REDEMPTION_REQUIRED"},
    }):
        name, url, _api, failure = module.fetch_instance_3mf(
            SimpleNamespace(cookies={}), 29275, "", origin="https://makerworld.com.cn",
            browser_authorization=True, browser_profile_id="profile-cn",
            model_page_url="https://makerworld.com.cn/zh/models/132602",
        )
    assert not name and not url
    assert failure == {"state": "redemption_required", "message": MESSAGE}


def test_redemption_status_survives_task_normalization_and_has_a_clear_default_message():
    with patch("app.services.task_state.load_database_json_state", return_value={}):
        payload = _normalize_missing_3mf({"items": [{
            "model_id": "132602", "instance_id": "29275",
            "status": "redemption_required", "message": MESSAGE,
        }]})
    assert payload["items"][0]["status"] == "redemption_required"
    assert payload["items"][0]["message"] == MESSAGE
    assert three_mf.describe_three_mf_failure("redemption_required") == MESSAGE
    assert three_mf.merge_three_mf_failure(
        {"state": "http_error"}, {"state": "redemption_required", "message": MESSAGE},
    )["state"] == "redemption_required"


def test_idle_retries_skip_redemption_models_and_still_schedule_ordinary_failures():
    buckets, cooldown = archive_worker._auto_missing_3mf_retry_buckets([
        {"model_id": "132602", "model_url": "https://makerworld.com.cn/zh/models/132602",
         "status": "redemption_required", "updated_at": "2026-01-01T00:00:00+08:00"},
        {"model_id": "123", "model_url": "https://makerworld.com.cn/zh/models/123",
         "status": "http_error", "updated_at": "2026-01-01T00:00:00+08:00"},
    ], now=now())
    assert [item["model_id"] for item in buckets["cn"]] == ["123"]
    assert cooldown == 0


def test_source_refresh_preserves_redemption_status_in_rebuilt_missing_tasks(tmp_path):
    instance = remote_refresh._merge_instance_record(
        {"id": 29275, "downloadState": "redemption_required", "downloadMessage": MESSAGE},
        {"id": 29275, "title": "Castle profile"},
    )
    meta = {"id": 132602, "url": "https://makerworld.com.cn/zh/models/132602", "instances": [instance]}
    items = remote_refresh._build_missing_3mf_items(tmp_path / "meta.json", meta, {"matches": {}})
    with patch("app.services.task_state.load_database_json_state", return_value={}):
        normalized = _normalize_missing_3mf({"items": items})["items"]

    assert len(normalized) == 1
    assert normalized[0]["status"] == "redemption_required"
    assert normalized[0]["message"] == MESSAGE
    buckets, _cooldown = archive_worker._auto_missing_3mf_retry_buckets(normalized, now=now())
    assert not any(buckets.values())


def test_redemption_pauses_sibling_profiles_but_a_manual_retry_can_download_after_redemption(tmp_path):
    model = tmp_path / "MW_132602_castle"
    model.mkdir()
    meta = {"id": 132602, "title": "Castle", "baseName": model.name,
            "url": "https://makerworld.com.cn/zh/models/132602",
            "instances": [{"id": 29275}, {"id": 3626706}]}
    (model / "meta.json").write_text(json.dumps(meta))
    with patch.object(archive, "fetch_instance_3mf", return_value=(
        "", "", "", {"state": "redemption_required", "message": MESSAGE},
    )) as authorize, patch.object(archive, "reserve_three_mf_download_slot", return_value={"allowed": True}):
        result = archive.archive_model(
            meta["url"], "token=fixture", tmp_path, tmp_path / "logs",
            three_mf_only=True, record_missing_3mf_log=False,
        )
    assert authorize.call_count == 1
    assert [item["downloadState"] for item in result["missing_3mf"]] == [
        "redemption_required", "redemption_required",
    ]
    assert not three_mf.is_three_mf_download_prohibited(result["instances"][0])

    def transfer(_url, dest, **_kwargs):
        dest.write_bytes(b"download fixture")

    with patch.object(archive, "fetch_instance_3mf", return_value=(
        "castle.3mf", "https://cdn.example.test/castle.3mf", "", {"state": "available"},
    )), patch.object(archive, "reserve_three_mf_download_slot", return_value={"allowed": True}), \
            patch.object(archive, "_download_three_mf_file", side_effect=transfer):
        result = archive.archive_model(
            meta["url"], "token=fixture", tmp_path, tmp_path / "logs",
            three_mf_only=True, record_missing_3mf_log=False,
        )
    assert result["missing_3mf"] == []
    assert result["stats"]["instances"]["three_mf_downloaded"] == 2
