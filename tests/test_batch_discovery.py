import unittest
from unittest.mock import patch

import requests

from app.services import batch_discovery


class BatchDiscoveryTest(unittest.TestCase):
    def test_resolve_uid_by_handle_prefers_user_search_api(self):
        calls = []

        def fake_api_get_json(_session, **kwargs):
            calls.append(kwargs)
            self.assertEqual(kwargs["service_name"], "search-service")
            self.assertEqual(kwargs["path"], "/search/user")
            return {
                "total": 2,
                "hits": [
                    {"uid": 111, "handle": "other", "name": "Other"},
                    {"uid": 31394486, "handle": "GLB_Whittlabs", "name": "Whitt Labs"},
                ],
            }

        with patch.object(batch_discovery, "_api_get_json", side_effect=fake_api_get_json), \
                patch.object(batch_discovery, "_append_discovery_debug") as debug_log:
            uid = batch_discovery._resolve_uid_by_handle_api(
                requests.Session(),
                "https://makerworld.com.cn/zh/@GLB_Whittlabs/upload",
                "token=ok",
                "GLB_Whittlabs",
                "author_uid",
            )

        self.assertEqual(uid, "31394486")
        self.assertEqual(len(calls), 1)
        debug_log.assert_called_with(
            "author_uid_resolved",
            handle="GLB_Whittlabs",
            uid="31394486",
            mode="search_user",
        )

    def test_resolve_uid_by_handle_accepts_single_user_search_hit(self):
        def fake_api_get_json(_session, **_kwargs):
            return {
                "total": 1,
                "hits": [
                    {"uid": 2595475119, "name": "LCFigure", "avatar": "https://example.test/avatar.jpg"},
                ],
            }

        with patch.object(batch_discovery, "_api_get_json", side_effect=fake_api_get_json), \
                patch.object(batch_discovery, "_append_discovery_debug"):
            uid = batch_discovery._resolve_uid_by_handle_api(
                requests.Session(),
                "https://makerworld.com/zh/@LC.Figure/upload",
                "token=ok",
                "LC.Figure",
                "author_uid",
            )

        self.assertEqual(uid, "2595475119")

    def test_extract_collection_entries_ignores_nested_design_samples(self):
        payload = {
            "total": 2,
            "hits": [
                {
                    "id": 518732,
                    "title": "默认收藏夹",
                    "isDefault": True,
                    "designCnt": 338,
                    "designCover": [
                        "https://example.test/cover.jpg",
                    ],
                    "designs": [
                        {
                            "id": 2356866,
                            "title": "嵌套模型样例",
                            "coverLandscape": "https://example.test/model.jpg",
                            "downloadCount": 12,
                        }
                    ],
                },
                {
                    "id": 989423,
                    "title": "已打印",
                    "isDefault": False,
                    "designCnt": 14,
                },
            ],
        }

        entries = batch_discovery._extract_collection_entries(payload, "2024907479")

        self.assertEqual(
            entries,
            [
                {"id": "518732", "name": "默认收藏夹", "count": 338},
                {"id": "989423", "name": "已打印", "count": 14},
            ],
        )
        self.assertNotIn("2356866", {item["id"] for item in entries})

    def test_extract_collection_page_all_models_count_from_rendered_tab(self):
        html = """
        <html>
          <body>
            <button>所有模型 (308)</button>
            <button>个人收藏夹 (5)</button>
          </body>
        </html>
        """

        self.assertEqual(batch_discovery._extract_collection_page_all_models_count(html), 308)

    def test_apply_collection_page_expected_total_marks_strict_total(self):
        result = {
            "items": ["https://makerworld.com.cn/zh/models/1"],
            "expected_total": 43,
            "mode": "collection_models_api",
        }

        updated = batch_discovery._apply_collection_page_expected_total(result, 308)

        self.assertIs(updated, result)
        self.assertEqual(updated["reported_expected_total"], 43)
        self.assertEqual(updated["expected_total"], 308)
        self.assertEqual(updated["expected_total_source"], "collection_page_all_models")
        self.assertTrue(updated["strict_expected_total"])

    def test_author_upload_api_result_marks_api_total_as_strict(self):
        payload = {
            "total": 2,
            "hits": [
                {"id": 1001, "title": "A", "coverUrl": "https://example.test/a.jpg"},
                {"id": 1002, "title": "B", "coverUrl": "https://example.test/b.jpg"},
            ],
        }

        with patch.object(batch_discovery, "_resolve_author_uid", return_value="2024907479"), \
                patch.object(batch_discovery, "_api_get_json", return_value=payload), \
                patch.object(batch_discovery, "_append_discovery_debug"):
            result = batch_discovery._discover_author_upload_api(
                requests.Session(),
                "https://makerworld.com.cn/zh/@ace/upload",
                "token=ok",
                max_pages=1,
            )

        self.assertEqual(result["expected_total"], 2)
        self.assertEqual(result["expected_total_source"], "author_upload_api_total")
        self.assertTrue(result["strict_expected_total"])

    def test_extract_followed_authors_builds_upload_urls(self):
        payload = {
            "hits": [
                {"uid": 1, "handle": "AcePrint", "name": "Ace Print", "avatarUrl": "https://example.test/a.jpg"},
                {"designId": 1001, "title": "Not an author", "coverUrl": "https://example.test/m.jpg"},
            ]
        }

        authors = batch_discovery._extract_followed_authors(payload, "cn")

        self.assertEqual(len(authors), 1)
        self.assertEqual(authors[0]["title"], "Ace Print")
        self.assertEqual(authors[0]["url"], "https://makerworld.com.cn/zh/@AcePrint/upload")

    def test_default_favorites_subscription_source_uses_account_handle(self):
        source = batch_discovery.default_favorites_subscription_source(
            "global",
            {"handle": "s450586793", "name": "艾斯"},
        )

        self.assertEqual(source["title"], "艾斯 所有模型收藏夹")
        self.assertEqual(source["url"], "https://makerworld.com/en/@s450586793/collections/models")

    def test_extract_account_profile_ignores_plain_counter_id(self):
        profile = batch_discovery._extract_account_profile(
            {"data": {"id": 123, "unreadCount": 4}}
        )

        self.assertEqual(profile, {})

    def test_extract_model_source_items_prefers_hit_design_id_order(self):
        payload = {
            "hits": [
                {
                    "id": 1002,
                    "title": "模型 B",
                    "coverUrl": "https://example.test/b.jpg",
                    "relatedUrl": "https://makerworld.com.cn/zh/models/1001",
                }
            ]
        }

        items = batch_discovery._extract_model_source_items_from_hits(
            payload,
            "https://makerworld.com.cn/zh/@ace/collections/models",
        )

        self.assertEqual(items[0]["model_id"], "1002")
        self.assertEqual(items[0]["source_order"], 0)

    def test_extract_followed_collections_builds_collection_detail_urls(self):
        payload = {
            "hits": [
                {"id": 518732, "title": "关注收藏夹", "designCnt": 12, "isPublic": True},
                {"id": 2356866, "title": "模型样例", "coverLandscape": "https://example.test/model.jpg", "downloadCount": 12},
            ]
        }

        collections = batch_discovery._extract_followed_collections(payload, "cn")

        self.assertEqual(len(collections), 1)
        self.assertEqual(collections[0]["title"], "关注收藏夹")
        self.assertTrue(collections[0]["url"].startswith("https://makerworld.com.cn/zh/collections/518732"))


if __name__ == "__main__":
    unittest.main()
