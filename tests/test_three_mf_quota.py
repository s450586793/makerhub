import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.schemas.models import AppConfig, ThreeMfDownloadLimitsConfig
from app.services.archive_worker import _three_mf_daily_limits
from app.services.three_mf_quota import reserve_three_mf_download_slot


class ThreeMfQuotaTest(unittest.TestCase):
    def test_app_config_defaults_daily_limits_to_100_per_site(self):
        config = AppConfig()

        self.assertEqual(config.three_mf_limits.cn_daily_limit, 100)
        self.assertEqual(config.three_mf_limits.global_daily_limit, 100)

    def test_app_config_allows_zero_as_unlimited_limit(self):
        limits = ThreeMfDownloadLimitsConfig(cn_daily_limit=0, global_daily_limit=0)

        self.assertEqual(limits.cn_daily_limit, 0)
        self.assertEqual(limits.global_daily_limit, 0)

    def test_archive_worker_preserves_zero_daily_limits(self):
        config = SimpleNamespace(
            three_mf_limits=SimpleNamespace(cn_daily_limit=0, global_daily_limit=0)
        )

        self.assertEqual(_three_mf_daily_limits(config), (0, 0))

    def test_reserve_blocks_after_daily_limit_per_site(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            quota_path = Path(temp_dir) / "quota.json"
            lock_path = Path(temp_dir) / "quota.lock"

            first = reserve_three_mf_download_slot(
                url="https://makerworld.com.cn/zh/models/1",
                limit=2,
                quota_path=quota_path,
                lock_path=lock_path,
            )
            second = reserve_three_mf_download_slot(
                url="https://makerworld.com.cn/zh/models/2",
                limit=2,
                quota_path=quota_path,
                lock_path=lock_path,
            )
            blocked = reserve_three_mf_download_slot(
                url="https://makerworld.com.cn/zh/models/3",
                limit=2,
                quota_path=quota_path,
                lock_path=lock_path,
            )
            global_first = reserve_three_mf_download_slot(
                url="https://makerworld.com/zh/models/4",
                limit=2,
                quota_path=quota_path,
                lock_path=lock_path,
            )

        self.assertTrue(first["allowed"])
        self.assertEqual(first["used"], 1)
        self.assertTrue(second["allowed"])
        self.assertEqual(second["used"], 2)
        self.assertFalse(blocked["allowed"])
        self.assertEqual(blocked["used"], 2)
        self.assertIn("国区每日 3MF 下载上限（2）", blocked["message"])
        self.assertTrue(global_first["allowed"])
        self.assertEqual(global_first["used"], 1)

    def test_zero_daily_limit_is_unlimited_and_does_not_write_quota(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            quota_path = Path(temp_dir) / "quota.json"
            lock_path = Path(temp_dir) / "quota.lock"

            first = reserve_three_mf_download_slot(
                url="https://makerworld.com.cn/zh/models/1",
                limit=0,
                quota_path=quota_path,
                lock_path=lock_path,
            )
            second = reserve_three_mf_download_slot(
                url="https://makerworld.com.cn/zh/models/2",
                limit=0,
                quota_path=quota_path,
                lock_path=lock_path,
            )

        self.assertTrue(first["allowed"])
        self.assertTrue(first["unlimited"])
        self.assertEqual(first["limit"], 0)
        self.assertIsNone(first["remaining"])
        self.assertTrue(second["allowed"])
        self.assertFalse(quota_path.exists())


if __name__ == "__main__":
    unittest.main()
