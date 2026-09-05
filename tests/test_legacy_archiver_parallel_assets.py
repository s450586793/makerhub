import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests

from app.services import asset_downloader
from app.services import legacy_archiver


class LegacyArchiverParallelAssetsTest(unittest.TestCase):
    def test_download_image_assets_uses_limited_parallel_workers(self):
        submitted = []

        class FakeFuture:
            def result(self):
                return None

        class FakeExecutor:
            def __init__(self, max_workers):
                self.max_workers = max_workers

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def submit(self, fn):
                submitted.append((self.max_workers, fn))
                fn()
                return FakeFuture()

        tasks = [
            {"url": f"https://example.test/{index}.jpg", "download": lambda: None, "apply": []}
            for index in range(8)
        ]

        with patch.object(asset_downloader, "ThreadPoolExecutor", FakeExecutor), \
                patch.object(asset_downloader, "as_completed", side_effect=lambda futures: list(futures)):
            stats = legacy_archiver._download_image_assets(tasks, None, 40, 45, "正在下载图片")

        self.assertEqual(stats, {"completed": 8, "failed": 0})
        self.assertEqual({max_workers for max_workers, _fn in submitted}, {legacy_archiver.IMAGE_ASSET_DOWNLOAD_WORKERS})

    def test_download_file_is_reexported_for_legacy_callers(self):
        self.assertIs(legacy_archiver.download_file, asset_downloader.download_file)

    def test_static_asset_failure_log_redacts_credentials_query_and_fragment(self):
        signed = (
            "https://user:password@cdn.example.test:8443/cloudflare/image.jpg"
            "?token=secret#verification"
        )

        def fail_download():
            raise asset_downloader.AssetDownloadError("static download failed")

        output = StringIO()
        with redirect_stdout(output):
            stats = legacy_archiver._download_image_assets(
                [
                    {
                        "url": signed,
                        "download": fail_download,
                        "apply": [],
                    }
                ],
                None,
                40,
                45,
                "正在下载图片",
            )

        logged = output.getvalue()
        self.assertEqual(stats, {"completed": 0, "failed": 1})
        self.assertIn("https://cdn.example.test:8443/cloudflare/image.jpg", logged)
        self.assertNotIn("user", logged)
        self.assertNotIn("password", logged)
        self.assertNotIn("token", logged)
        self.assertNotIn("secret", logged)
        self.assertNotIn("verification", logged)

    def test_parse_summary_keeps_image_order_after_parallel_downloads(self):
        html = '<p><img src="https://example.test/2.jpg"><img src="https://example.test/1.jpg"></p>'

        with TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir)

            def fake_download(_session, _url, dest, **_kwargs):
                dest.write_text("ok", encoding="utf-8")

            with patch.object(legacy_archiver, "download_file", side_effect=fake_download) as fake_downloader:
                summary = legacy_archiver.parse_summary(
                    {"summary": html},
                    "Demo",
                    object(),
                    out_dir,
                )

        self.assertEqual(fake_downloader.call_count, 2)
        self.assertEqual(
            [item["fileName"] for item in summary["summaryImages"]],
            ["summary_img_01.jpg", "summary_img_02.jpg"],
        )

    def test_collect_design_images_keeps_image_order_after_parallel_downloads(self):
        design = {
            "designPictures": [
                {"url": "https://example.test/2.jpg"},
                {"url": "https://example.test/1.jpg"},
            ]
        }

        with TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir)

            def fake_download(_session, _url, dest, **_kwargs):
                dest.write_text("ok", encoding="utf-8")

            with patch.object(legacy_archiver, "download_file", side_effect=fake_download) as fake_downloader:
                images, cover = legacy_archiver.collect_design_images(
                    design,
                    object(),
                    out_dir,
                    "Demo",
                )

        self.assertEqual(fake_downloader.call_count, 2)
        self.assertEqual(
            [item["fileName"] for item in images],
            ["design_01.jpg", "design_02.jpg"],
        )
        self.assertEqual(cover["fileName"], "design_01.jpg")

    def test_fresh_asset_session_inherits_base_session_cookies(self):
        captured = []
        base_session = requests.Session()
        base_session.cookies.set("maker_session", "secret", domain="example.test", path="/")

        with TemporaryDirectory() as temp_dir:
            dest = Path(temp_dir) / "asset.jpg"

            def fake_download(session, _url, _dest, **_kwargs):
                captured.append(session.cookies.get("maker_session", domain="example.test", path="/"))
                _dest.write_text("ok", encoding="utf-8")

            with patch.object(
                requests.Session,
                "get",
                side_effect=AssertionError("asset test must not use the network"),
            ), patch.object(
                legacy_archiver,
                "download_file",
                side_effect=fake_download,
            ) as fake_downloader:
                legacy_archiver._download_asset_with_fresh_session(base_session, "https://example.test/asset.jpg", dest)

        fake_downloader.assert_called_once()
        self.assertEqual(captured, ["secret"])


if __name__ == "__main__":
    unittest.main()
