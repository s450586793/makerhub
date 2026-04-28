import unittest
from unittest.mock import patch

from app.schemas.models import AdvancedRuntimeConfig
from app.services import resource_limiter


class ResourceLimiterConfigTest(unittest.TestCase):
    def setUp(self):
        self.original_limits = dict(resource_limiter.RESOURCE_LIMITS)
        self.original_gates = dict(resource_limiter._GATES)

    def tearDown(self):
        resource_limiter.RESOURCE_LIMITS.clear()
        resource_limiter.RESOURCE_LIMITS.update(self.original_limits)
        resource_limiter._GATES.clear()
        resource_limiter._GATES.update(self.original_gates)

    def test_configure_resource_limits_updates_existing_gates(self):
        resource_limiter.configure_resource_limits({
            "makerworld_request_limit": 1,
            "comment_asset_download_limit": 4,
            "three_mf_download_limit": 1,
            "disk_io_limit": 1,
        })
        resource_limiter.resource_snapshot()

        changed = resource_limiter.configure_resource_limits({
            "makerworld_request_limit": 3,
            "comment_asset_download_limit": 5,
            "three_mf_download_limit": 2,
            "disk_io_limit": 2,
        })
        snapshot = resource_limiter.resource_snapshot()

        self.assertEqual(changed["makerworld_page_api"], 3)
        self.assertEqual(snapshot["makerworld_page_api"]["capacity"], 3)
        self.assertEqual(snapshot["comment_assets"]["capacity"], 5)
        self.assertEqual(snapshot["three_mf_download"]["capacity"], 2)
        self.assertEqual(snapshot["disk_io"]["capacity"], 2)

    def test_configure_resource_limits_clamps_raw_values(self):
        changed = resource_limiter.configure_resource_limits({
            "makerworld_request_limit": 99,
            "comment_asset_download_limit": 99,
            "three_mf_download_limit": 99,
            "disk_io_limit": 0,
        })

        self.assertEqual(changed["makerworld_page_api"], 8)
        self.assertEqual(changed["comment_assets"], 16)
        self.assertEqual(changed["three_mf_download"], 4)
        self.assertEqual(changed["disk_io"], 1)

    def test_advanced_runtime_defaults_honor_existing_env_limits(self):
        with patch.dict("os.environ", {
            "MAKERHUB_REMOTE_REFRESH_MODEL_WORKERS": "3",
            "MAKERHUB_LIMIT_MAKERWORLD_REQUESTS": "4",
            "MAKERHUB_LIMIT_COMMENT_ASSETS": "5",
            "MAKERHUB_LIMIT_THREE_MF_DOWNLOADS": "2",
            "MAKERHUB_LIMIT_DISK_IO": "2",
        }):
            config = AdvancedRuntimeConfig()

        self.assertEqual(config.remote_refresh_model_workers, 3)
        self.assertEqual(config.makerworld_request_limit, 4)
        self.assertEqual(config.comment_asset_download_limit, 5)
        self.assertEqual(config.three_mf_download_limit, 2)
        self.assertEqual(config.disk_io_limit, 2)


if __name__ == "__main__":
    unittest.main()
