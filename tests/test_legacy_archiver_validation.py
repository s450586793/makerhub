import unittest

from app.services import legacy_archiver


class _JsonResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _ApiSession:
    def __init__(self, payload_by_host=None):
        self.headers = {"User-Agent": "test"}
        self.calls = []
        self.payload_by_host = payload_by_host or {}

    def get(self, url, timeout=None, headers=None):
        self.calls.append(url)
        for host, payload in self.payload_by_host.items():
            if host in url:
                return _JsonResponse(200, payload)
        return _JsonResponse(404, {})


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
        session = _ApiSession(
            {
                "api.bambulab.com": {
                    "id": 2416065,
                    "title": "Global model with same numeric id",
                    "coverUrl": "https://cdn.example.com/global.jpg",
                    "instances": [],
                }
            }
        )

        design = legacy_archiver.fetch_design_from_api(
            session,
            "",
            "https://makerworld.com.cn/zh/models/2416065",
            api_host_hint="https://api.bambulab.com",
        )

        self.assertIsNone(design)
        self.assertFalse(any("api.bambulab.com" in call for call in session.calls))

    def test_fetch_design_from_api_accepts_matching_cn_payload(self):
        session = _ApiSession(
            {
                "api.bambulab.cn": {
                    "id": "2416065",
                    "title": "十二生肖-兔女孩",
                    "coverUrl": "https://cdn.example.com/cn.jpg",
                    "instances": [],
                }
            }
        )

        design = legacy_archiver.fetch_design_from_api(
            session,
            "",
            "https://makerworld.com.cn/zh/models/2416065",
        )

        self.assertIsInstance(design, dict)
        self.assertEqual(design["id"], 2416065)
        self.assertEqual(design["title"], "十二生肖-兔女孩")

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


if __name__ == "__main__":
    unittest.main()
