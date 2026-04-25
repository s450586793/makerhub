import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
