import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch

from app.services import legacy_archiver


class _ApiSession:
    def __init__(self):
        self.headers = {"User-Agent": "test"}
        self.calls = []

    def get(self, url, timeout=None, headers=None):
        self.calls.append(url)
        raise AssertionError("MakerWorld control requests must use FlareSolverr")


class LegacyArchiverValidationTest(unittest.TestCase):
    def test_design_payload_rejects_empty_api_shell(self):
        error = legacy_archiver._design_payload_error(
            {"id": 0, "title": "", "coverUrl": "", "instances": []},
            "https://makerworld.com.cn/zh/models/2416065",
        )

        self.assertIn("模型 ID", error)

    def test_design_payload_rejects_id_mismatch(self):
        error = legacy_archiver._design_payload_error(
            {"id": 123, "title": "Wrong model", "coverUrl": "https://cdn.example.com/a.jpg"},
            "https://makerworld.com.cn/zh/models/2416065",
        )

        self.assertIn("不匹配", error)

    def test_design_payload_rejects_empty_title(self):
        error = legacy_archiver._design_payload_error(
            {"id": 2416065, "title": "", "coverUrl": "https://cdn.example.com/a.jpg"},
            "https://makerworld.com.cn/zh/models/2416065",
        )

        self.assertIn("标题为空", error)

    def test_fetch_design_from_api_does_not_cross_cn_to_global(self):
        session = _ApiSession()
        calls = []

        def fake_flaresolverr(url, **_kwargs):
            calls.append(url)
            return None

        with patch.object(legacy_archiver, "flaresolverr_get_json", side_effect=fake_flaresolverr):
            design = legacy_archiver.fetch_design_from_api(
                session,
                "",
                "https://makerworld.com.cn/zh/models/2416065",
                api_host_hint="https://api.bambulab.com",
            )

        self.assertIsNone(design)
        self.assertFalse(any("api.bambulab.com" in call for call in calls))
        self.assertEqual(session.calls, [])

    def test_fetch_design_from_api_accepts_matching_cn_payload(self):
        session = _ApiSession()

        def fake_flaresolverr(url, **_kwargs):
            if "api.bambulab.cn" in url:
                return {
                    "id": "2416065",
                    "title": "十二生肖-兔女孩",
                    "coverUrl": "https://cdn.example.com/cn.jpg",
                    "instances": [],
                }
            return None

        with patch.object(legacy_archiver, "flaresolverr_get_json", side_effect=fake_flaresolverr):
            design = legacy_archiver.fetch_design_from_api(
                session,
                "",
                "https://makerworld.com.cn/zh/models/2416065",
            )

        self.assertIsInstance(design, dict)
        self.assertEqual(design["id"], 2416065)
        self.assertEqual(design["title"], "十二生肖-兔女孩")
        self.assertEqual(session.calls, [])

    def test_fetch_design_from_api_prefers_global_bambulab_api(self):
        session = _ApiSession()
        calls = []

        def fake_flaresolverr(url, **_kwargs):
            calls.append(url)
            if "api.bambulab.com" in url:
                return {
                    "id": "2416065",
                    "title": "Global API model",
                    "coverUrl": "https://cdn.example.com/global.jpg",
                    "instances": [],
                }
            if "makerworld.com" in url:
                return {
                    "id": "2416065",
                    "title": "Site domain model",
                    "coverUrl": "https://cdn.example.com/site.jpg",
                    "instances": [],
                }
            return None

        with patch.object(legacy_archiver, "flaresolverr_get_json", side_effect=fake_flaresolverr):
            design = legacy_archiver.fetch_design_from_api(
                session,
                "",
                "https://makerworld.com/zh/models/2416065",
            )

        self.assertIsInstance(design, dict)
        self.assertEqual(design["title"], "Global API model")
        self.assertIn("api.bambulab.com", calls[0])
        self.assertEqual(session.calls, [])

    def test_fetch_design_from_api_uses_flaresolverr_without_requests(self):
        session = _ApiSession()
        with patch(
            "app.services.legacy_archiver.flaresolverr_get_json",
            return_value={
                "id": "2416065",
                "title": "FlareSolverr model",
                "coverUrl": "https://cdn.example.com/cn.jpg",
                "instances": [],
            },
        ):
            design = legacy_archiver.fetch_design_from_api(
                session,
                "token=abc",
                "https://makerworld.com.cn/zh/models/2416065",
            )

        self.assertIsInstance(design, dict)
        self.assertEqual(design["title"], "FlareSolverr model")
        self.assertEqual(session.calls, [])

    def test_fetch_html_with_flaresolverr_uses_flaresolverr_without_old_fallback(self):
        class FailingSession:
            headers = {"User-Agent": "test-agent"}

            def get(self, *_args, **_kwargs):
                raise AssertionError("MakerWorld HTML must use FlareSolverr")

        with patch(
            "app.services.legacy_archiver.flaresolverr_get_text",
            return_value="<html><script id=\"__NEXT_DATA__\"></script></html>",
        ):
            html = legacy_archiver.fetch_html_with_flaresolverr(
                FailingSession(),
                "https://makerworld.com.cn/zh/models/2416065",
                "token=abc",
            )

        self.assertIn("__NEXT_DATA__", html)

    def test_archive_model_reports_makerworld_404_page_as_source_deleted(self):
        makerworld_404_html = """
        <!doctype html>
        <html>
          <head><title>MakerWorld - 404</title></head>
          <body>
            <h1>404</h1>
            <p>该模型可能被改为草稿、下架或者设为私有。</p>
          </body>
        </html>
        """

        with TemporaryDirectory() as temp_dir, patch(
            "app.services.legacy_archiver.fetch_html_with_flaresolverr",
            return_value=makerworld_404_html,
        ), patch(
            "app.services.legacy_archiver.fetch_design_from_api",
            return_value=None,
        ):
            with self.assertRaisesRegex(RuntimeError, "404|下架|私有|草稿"):
                legacy_archiver.archive_model(
                    "https://makerworld.com.cn/zh/models/1590150",
                    "",
                    Path(temp_dir) / "archive",
                    Path(temp_dir) / "logs",
                )

    def test_three_mf_fetch_html_404_is_not_classified_as_cloudflare(self):
        makerworld_404_html = """
        <!doctype html>
        <html>
          <head><title>404</title></head>
          <body>该模型可能被改为草稿、下架或者设为私有。</body>
        </html>
        """

        failure = legacy_archiver._classify_3mf_fetch_failure(
            status_code=404,
            text=makerworld_404_html,
            source="cn",
        )

        self.assertEqual(failure["state"], "not_found")
        self.assertIn("404", failure["message"])
        self.assertIn("私有", failure["message"])
        self.assertNotIn("验证", failure["message"])
        self.assertNotIn("Cloudflare", failure["message"])

    def test_comment_api_base_candidates_prefer_global_bambulab_api(self):
        candidates = legacy_archiver._comment_api_base_candidates(
            "https://makerworld.com/zh/models/2416065",
            api_host_hint="https://makerworld.com",
        )

        self.assertEqual(candidates[0], "https://api.bambulab.com")
        self.assertIn("https://makerworld.com", candidates)

    def test_comment_service_candidates_use_bambulab_v1_without_legacy_api_prefix(self):
        candidates = legacy_archiver._comment_service_endpoint_candidates(
            "https://makerworld.com/zh/models/2416065",
            "/commentandrating",
            api_host_hint="https://makerworld.com",
        )

        self.assertEqual(
            candidates[0],
            "https://api.bambulab.com/v1/comment-service/commentandrating",
        )
        self.assertNotIn(
            "https://api.bambulab.com/api/v1/comment-service/commentandrating",
            candidates,
        )
        self.assertNotIn(
            "https://api.bambulab.com/makerworld/v1/comment-service/commentandrating",
            candidates,
        )

    def test_extract_author_ignores_browsing_history_link(self):
        author = legacy_archiver.extract_author(
            {},
            '<nav><a href="/zh/@s450586793/browsing-history">浏览历史</a></nav>',
        )

        self.assertEqual(author["name"], "")
        self.assertEqual(author["url"], "")

    def test_extract_author_ignores_polluted_author_object(self):
        author = legacy_archiver.extract_author(
            {
                "author": {
                    "name": "浏览历史",
                    "url": "https://makerworld.com.cn/zh/@s450586793/browsing-history",
                }
            },
            "",
        )

        self.assertEqual(author["name"], "")
        self.assertEqual(author["url"], "")

    def test_extract_author_keeps_real_user_link(self):
        author = legacy_archiver.extract_author(
            {},
            '<a class="user_link" href="/zh/@realmaker"><img src="https://cdn.example.com/a.png">真实作者</a>',
        )

        self.assertEqual(author["name"], "真实作者")
        self.assertEqual(author["url"], "https://makerworld.com.cn/zh/@realmaker")
        self.assertEqual(author["avatarUrl"], "https://cdn.example.com/a.png")

    def test_comment_count_prefers_comment_service_total_over_page_hints(self):
        count = legacy_archiver._resolved_comment_count(
            unique_sections=[{"commentCount": 176}],
            next_data={},
            design={"commentCount": 4},
            comment_total=2,
            page_fetch_stats={"total": 3},
        )

        self.assertEqual(count, 3)

    def test_comment_count_prefers_design_count_over_page_hints_without_service_total(self):
        count = legacy_archiver._resolved_comment_count(
            unique_sections=[{"commentCount": 176}],
            next_data={},
            design={"commentCount": 4},
            comment_total=2,
            page_fetch_stats={"total": 0},
        )

        self.assertEqual(count, 4)

    def test_comment_count_uses_explicit_empty_comment_service_total(self):
        count = legacy_archiver._resolved_comment_count(
            unique_sections=[{"commentCount": 176}],
            next_data={},
            design={"commentCount": 4},
            comment_total=0,
            page_fetch_stats={"total": 0, "total_known": True},
        )

        self.assertEqual(count, 0)

    def test_choose_archive_base_name_prefers_existing_model_dir(self):
        class _FakeDir:
            def __init__(self, name, root=None):
                self.name = name
                self._root = root or self

            def resolve(self):
                return self

            def __truediv__(self, child):
                return _FakeDir(str(child), root=self._root if self is not self._root else self)

            def exists(self):
                return self.name == "MW_2475775_旧标题"

            def is_dir(self):
                return self.exists()

            def relative_to(self, _root):
                return self

            def as_posix(self):
                return self.name

            def glob(self, _pattern):
                return []

        base_name, action = legacy_archiver.choose_archive_base_name(
            2475775,
            "新标题",
            existing_root=_FakeDir("archive"),
            existing_model_dir="MW_2475775_旧标题",
        )

        self.assertEqual(base_name, "MW_2475775_旧标题")
        self.assertEqual(action, "updated")

    def test_missing_3mf_summary_does_not_write_legacy_log_file(self):
        captured = []
        with TemporaryDirectory() as temp_dir, patch(
            "app.services.legacy_archiver.append_business_log",
            side_effect=lambda category, event, message="", **payload: captured.append((category, event, message, payload)),
        ):
            logs_dir = Path(temp_dir)
            legacy_archiver._record_missing_3mf_summary(
                logs_dir,
                "MW_1_Demo",
                [
                    {
                        "id": "inst-1",
                        "title": "Plate A",
                        "downloadState": "missing",
                        "downloadMessage": "未获取到 3MF 下载地址",
                    }
                ],
                logger=None,
            )
            legacy_log_exists = (logs_dir / "missing_3mf.log").exists()

        self.assertFalse(legacy_log_exists)
        self.assertEqual(captured[0][0], "archive")
        self.assertEqual(captured[0][1], "missing_3mf_detected")
        self.assertEqual(captured[0][3]["count"], 1)
        self.assertEqual(captured[0][3]["sample"][0]["instance_id"], "inst-1")


if __name__ == "__main__":
    unittest.main()
