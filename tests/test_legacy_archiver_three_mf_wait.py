import unittest
from unittest.mock import patch

from app.services import legacy_archiver


class ThreeMfDownloadWaitTest(unittest.TestCase):
    def test_default_wait_uses_five_to_ten_seconds(self):
        with patch.dict("os.environ", {}, clear=False), patch(
            "app.services.legacy_archiver.random.uniform",
            return_value=7.5,
        ) as random_uniform:
            wait_seconds = legacy_archiver._three_mf_download_wait_seconds()

        self.assertEqual(wait_seconds, 7.5)
        random_uniform.assert_called_once_with(5.0, 10.0)

    def test_wait_can_be_disabled_by_environment(self):
        with patch.dict(
            "os.environ",
            {
                "MAKERHUB_THREE_MF_DOWNLOAD_WAIT_MIN_SECONDS": "0",
                "MAKERHUB_THREE_MF_DOWNLOAD_WAIT_MAX_SECONDS": "0",
            },
        ):
            wait_seconds = legacy_archiver._three_mf_download_wait_seconds()

        self.assertEqual(wait_seconds, 0.0)

    def test_wait_sleep_uses_calculated_duration(self):
        with patch(
            "app.services.legacy_archiver._three_mf_download_wait_seconds",
            return_value=5.25,
        ), patch("app.services.legacy_archiver.time.sleep") as sleep:
            waited = legacy_archiver._wait_before_three_mf_download("test")

        self.assertEqual(waited, 5.25)
        sleep.assert_called_once_with(5.25)


if __name__ == "__main__":
    unittest.main()
