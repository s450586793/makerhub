import tempfile
import unittest
from pathlib import Path

import app.services.task_state as task_state_module
from app.services.remote_refresh import _build_missing_3mf_items
from app.services.task_state import METADATA_ONLY_MISSING_3MF_MESSAGE, _normalize_missing_3mf


class Missing3mfTest(unittest.TestCase):
    def test_normalize_filters_metadata_only_placeholders(self):
        payload = {
            "items": [
                {
                    "model_id": "1686848",
                    "title": "仪羽毛球（球头无挖孔）",
                    "status": "missing",
                    "message": METADATA_ONLY_MISSING_3MF_MESSAGE,
                },
                {
                    "model_id": "2",
                    "title": "Real missing profile",
                    "status": "missing",
                    "message": "未获取到 3MF 下载地址。",
                },
            ]
        }

        normalized = _normalize_missing_3mf(payload)

        self.assertEqual(len(normalized["items"]), 1)
        self.assertEqual(normalized["items"][0]["model_id"], "2")

    def test_remote_refresh_missing_builder_skips_metadata_only_placeholders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            meta_path = Path(temp_dir) / "model" / "meta.json"
            meta_path.parent.mkdir(parents=True)
            meta = {
                "id": "1686848",
                "url": "https://makerworld.com.cn/zh/models/1686848",
                "title": "仪羽毛球（球头无挖孔）",
                "instances": [
                    {
                        "id": "profile-1",
                        "title": "信息补全占位配置",
                        "downloadState": "missing",
                        "downloadMessage": METADATA_ONLY_MISSING_3MF_MESSAGE,
                    },
                    {
                        "id": "profile-2",
                        "title": "真实缺失配置",
                        "downloadState": "missing",
                        "downloadMessage": "未获取到 3MF 下载地址。",
                    },
                ],
            }

            items = _build_missing_3mf_items(meta_path, meta, resolved_files={"matches": {}})

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["instance_id"], "profile-2")

    def test_normalize_infers_verification_status_from_message(self):
        payload = {
            "items": [
                {
                    "model_id": "1759345",
                    "title": "0.2mm 层高, 2 层墙, 15% 填充",
                    "status": "missing",
                    "model_url": "https://makerworld.com.cn/zh/models/1759345",
                    "message": "MakerWorld 需要验证，前往官网任意下载一个模型。",
                }
            ]
        }

        normalized = _normalize_missing_3mf(payload)

        self.assertEqual(len(normalized["items"]), 1)
        self.assertEqual(normalized["items"][0]["status"], "verification_required")
        self.assertEqual(normalized["items"][0]["message"], "MakerWorld 需要验证，前往官网任意下载一个模型。")

    def test_retry_state_is_not_reclassified_by_old_verification_message(self):
        payload = {
            "items": [
                {
                    "model_id": "1759345",
                    "title": "0.2mm 层高, 2 层墙, 15% 填充",
                    "status": "queued",
                    "model_url": "https://makerworld.com.cn/zh/models/1759345",
                    "message": "MakerWorld 需要验证，前往官网任意下载一个模型。",
                }
            ]
        }

        normalized = _normalize_missing_3mf(payload)

        self.assertEqual(len(normalized["items"]), 1)
        self.assertEqual(normalized["items"][0]["status"], "queued")

    def test_download_limited_message_uses_current_limit_guard_date(self):
        original_guard_path = task_state_module.THREE_MF_LIMIT_GUARD_PATH
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                task_state_module.THREE_MF_LIMIT_GUARD_PATH = Path(temp_dir) / "three_mf_limit_guard.json"
                task_state_module.THREE_MF_LIMIT_GUARD_PATH.write_text(
                    """
{
  "active": true,
  "limited_until": "2099-01-02T00:00:00+08:00",
  "last_hit_at": "2099-01-01T01:22:00+08:00",
  "message": "国区返回了每日下载上限，今日暂停自动重试，自动重试暂停至 2026-04-26 00:00。",
  "reason": "download_limited",
  "model_url": "https://makerworld.com.cn/zh/models/2193050"
}
""".strip(),
                    encoding="utf-8",
                )
                payload = {
                    "items": [
                        {
                            "model_id": "2193050",
                            "title": "0.2mm 层高, 2 层墙, 15% 填充",
                            "status": "download_limited",
                            "model_url": "https://makerworld.com.cn/zh/models/2193050",
                            "message": "国区返回了每日下载上限，今日暂停自动重试，自动重试暂停至 2026-04-26 00:00。",
                        }
                    ]
                }

                normalized = _normalize_missing_3mf(payload)
        finally:
            task_state_module.THREE_MF_LIMIT_GUARD_PATH = original_guard_path

        self.assertEqual(len(normalized["items"]), 1)
        self.assertEqual(normalized["items"][0]["status"], "download_limited")
        self.assertEqual(
            normalized["items"][0]["message"],
            "国区返回了每日下载上限，今日暂停自动重试，自动重试暂停至 2099-01-02 00:00。",
        )


if __name__ == "__main__":
    unittest.main()
