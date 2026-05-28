import unittest
from unittest.mock import patch

import requests

from app.services import batch_discovery


class BatchDiscoveryTest(unittest.TestCase):
    def test_normalize_source_url_canonicalizes_author_upload_links_to_zh(self):
        cases = {
            "https://makerworld.com/en/@Oierre/upload": "https://makerworld.com/zh/@Oierre/upload",
            "https://makerworld.com/@Oierre/upload?appSharePlatform=copy": "https://makerworld.com/zh/@Oierre/upload",
            "https://makerworld.com.cn/@Xy.Shy/upload?appSharePlatform=copy": "https://makerworld.com.cn/zh/@Xy.Shy/upload",
            "https://makerworld.com.cn/zh/@LC.Figure": "https://makerworld.com.cn/zh/@LC.Figure/upload",
        }

        for raw_url, expected in cases.items():
            with self.subTest(raw_url=raw_url):
                self.assertEqual(batch_discovery.normalize_source_url(raw_url), expected)

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

    def test_collection_list_discovery_prefers_design_endpoint_total_for_expected_total(self):
        def fake_fetch_entries(*_args, **_kwargs):
            return [
                {"id": "518732", "name": "默认收藏夹", "count": 340},
                {"id": "989423", "name": "已打印", "count": 14},
            ]

        def fake_api_get_json(_session, **kwargs):
            path = kwargs["path"]
            offset = int((kwargs.get("params") or {}).get("offset") or 0)
            if path == "/favorites/518732/designs":
                if offset == 0:
                    return {
                        "hits": [
                            {"id": 1001, "title": "A", "coverUrl": "https://example.test/a.jpg"},
                            {"id": 1002, "title": "B", "coverUrl": "https://example.test/b.jpg"},
                        ],
                        "total": 2,
                    }
                return {"hits": [], "total": 2}
            if path == "/favorites/989423/designs":
                return {
                    "hits": [
                        {"id": 2001, "title": "C", "coverUrl": "https://example.test/c.jpg"},
                    ],
                    "total": 1,
                }
            return None

        with patch.object(batch_discovery, "_fetch_collection_list_entries", side_effect=fake_fetch_entries), \
                patch.object(batch_discovery, "_api_get_json", side_effect=fake_api_get_json), \
                patch.object(batch_discovery, "_append_discovery_debug"):
            result = batch_discovery._discover_collection_models_by_lists(
                requests.Session(),
                "https://makerworld.com.cn/zh/@s450586793/collections/models",
                "token=ok",
                "s450586793",
                "2024907479",
                max_pages=2,
                limit=20,
            )

        self.assertEqual(len(result["items"]), 3)
        self.assertEqual(result["expected_total"], 3)
        self.assertNotIn("reported_expected_total", result)
        self.assertEqual(result["expected_total_source"], "collection_designs_total")
        self.assertTrue(result["strict_expected_total"])

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

    def test_api_get_json_can_skip_empty_hits_and_continue_endpoint_probe(self):
        calls = []

        class FakeScraplingResult:
            status_code = 0
            engine = "test"
            error = ""

        class FakeResponse:
            status_code = 200
            headers = {"content-type": "application/json"}
            text = "{}"

            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        class FakeSession:
            headers = {"User-Agent": "test-agent"}

            def get(self, url, **_kwargs):
                calls.append(url)
                if len(calls) == 1:
                    return FakeResponse({"hits": [], "total": 0})
                return FakeResponse({"hits": [{"id": 1001, "title": "A"}], "total": 1})

        with patch.object(
            batch_discovery,
            "_service_endpoint_candidates",
            return_value=["https://api.example.test/empty", "https://api.example.test/full"],
        ), patch.object(
            batch_discovery,
            "fetch_json_with_scrapling",
            return_value=(None, FakeScraplingResult()),
        ), patch.object(batch_discovery, "_append_discovery_debug"):
            payload = batch_discovery._api_get_json(
                FakeSession(),
                "https://makerworld.com/zh/@ace/collections/models",
                "token=ok",
                "design-service",
                "/favorites/designs/123",
                {"offset": 0, "limit": 20},
                skip_empty_hits=True,
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual(payload["total"], 1)

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
        self.assertEqual(authors[0]["avatar_url"], "https://example.test/a.jpg")
        self.assertEqual(authors[0]["url"], "https://makerworld.com.cn/zh/@AcePrint/upload")

    def test_extract_followed_authors_skips_uid_only_frontend_follow_hits(self):
        payload = {
            "hits": [
                {
                    "uid": 2024907479,
                    "handle": "",
                    "name": "艾斯",
                    "avatar": "https://example.test/avatar.jpg",
                    "downloadCount": 12,
                    "publicInstanceUploadCount": 3,
                    "MWCount": {"designCount": 9},
                }
            ],
            "total": 1,
        }

        authors = batch_discovery._extract_followed_authors(payload, "cn")

        self.assertEqual(authors, [])

    def test_extract_followed_authors_accepts_next_data_followers(self):
        payload = {
            "props": {
                "pageProps": {
                    "followers": [
                        {
                            "uid": 954312513,
                            "name": "3D Girl",
                            "handle": "GLB_ThreeeDee",
                            "avatar": "https://example.test/a.jpg",
                            "publicInstanceUploadCount": 348,
                        }
                    ],
                    "total": 27,
                }
            }
        }

        authors = batch_discovery._extract_followed_authors(payload, "cn")

        self.assertEqual(len(authors), 1)
        self.assertEqual(authors[0]["handle"], "GLB_ThreeeDee")

    def test_followed_author_paths_prioritize_makerworld_frontend_follows_api(self):
        paths = batch_discovery._followed_author_path_candidates("2024907479")

        self.assertEqual(paths[0], "/user/2024907479/follows")
        self.assertEqual(len(paths), len(set(paths)))

    def test_discover_cookie_followed_authors_paginates_frontend_follows_api(self):
        calls = []

        def fake_api_get_json(_session, **kwargs):
            calls.append(kwargs)
            self.assertEqual(kwargs["service_name"], "user-service")
            if kwargs["path"] != "/user/2024907479/follows":
                return None
            offset = int((kwargs.get("params") or {}).get("offset") or 0)
            if offset == 0:
                return {
                    "hits": [
                        {"uid": 1, "handle": "AcePrint", "name": "Ace Print", "avatar": "https://example.test/a.jpg"},
                        {"uid": 2, "handle": "BeePrint", "name": "Bee Print", "avatar": "https://example.test/b.jpg"},
                    ],
                    "total": 3,
                }
            if offset == 2:
                return {
                    "hits": [
                        {"uid": 3, "handle": "CatPrint", "name": "Cat Print", "avatar": "https://example.test/c.jpg"},
                    ],
                    "total": 3,
                }
            return {"hits": [], "total": 3}

        with patch.object(batch_discovery, "_api_get_json", side_effect=fake_api_get_json), \
                patch.object(batch_discovery, "_append_discovery_debug"):
            result = batch_discovery.discover_cookie_followed_authors(
                "cn",
                "token=ok",
                uid="2024907479",
                max_pages=4,
                limit=2,
            )

        self.assertEqual(result["path"], "/user/2024907479/follows")
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["total"], 3)
        self.assertEqual([item["handle"] for item in result["items"]], ["AcePrint", "BeePrint", "CatPrint"])
        self.assertEqual([call["params"]["offset"] for call in calls], [0, 2])

    def test_discover_cookie_followed_authors_continues_when_api_caps_page_size(self):
        calls = []

        def fake_api_get_json(_session, **kwargs):
            calls.append(kwargs)
            if kwargs["path"] != "/user/2024907479/follows":
                return None
            offset = int((kwargs.get("params") or {}).get("offset") or 0)
            if offset == 0:
                return {
                    "hits": [
                        {
                            "uid": index,
                            "handle": f"Author{index}",
                            "name": f"Author {index}",
                            "publicInstanceUploadCount": index,
                        }
                        for index in range(1, 21)
                    ],
                    "total": 27,
                }
            if offset == 20:
                return {
                    "hits": [
                        {
                            "uid": index,
                            "handle": f"Author{index}",
                            "name": f"Author {index}",
                            "publicInstanceUploadCount": index,
                        }
                        for index in range(21, 28)
                    ],
                    "total": 27,
                }
            return {"hits": [], "total": 27}

        with patch.object(batch_discovery, "_api_get_json", side_effect=fake_api_get_json), \
                patch.object(batch_discovery, "_append_discovery_debug"):
            result = batch_discovery.discover_cookie_followed_authors(
                "cn",
                "token=ok",
                uid="2024907479",
                max_pages=4,
                limit=100,
            )

        self.assertEqual(result["count"], 27)
        self.assertEqual(result["total"], 27)
        self.assertEqual([call["params"]["offset"] for call in calls], [0, 20])

    def test_resolve_author_uid_accepts_user_uid_handle(self):
        with patch.object(batch_discovery, "_api_get_json") as api_get_json, \
                patch.object(batch_discovery, "_append_discovery_debug") as debug_log:
            uid = batch_discovery._resolve_author_uid(
                requests.Session(),
                "https://makerworld.com.cn/zh/@user_2024907479/upload",
                "token=ok",
                "user_2024907479",
            )

        self.assertEqual(uid, "")
        api_get_json.assert_not_called()
        debug_log.assert_called_with("author_uid_missing", handle="user_2024907479", reason="synthetic_uid_handle")

    def test_default_favorites_subscription_source_uses_account_handle(self):
        source = batch_discovery.default_favorites_subscription_source(
            "global",
            {"handle": "s450586793", "name": "艾斯", "avatar_url": "https://example.test/avatar.jpg"},
        )

        self.assertEqual(source["title"], "艾斯的收藏夹")
        self.assertEqual(source["avatar_url"], "https://example.test/avatar.jpg")
        self.assertEqual(source["url"], "https://makerworld.com/zh/@s450586793/collections/models")

    def test_extract_user_info_from_next_data_reads_profile_and_counts(self):
        summary = batch_discovery._extract_user_info_from_next_data(
            {
                "props": {
                    "pageProps": {
                        "userInfo": {
                            "uid": 2024907479,
                            "name": "艾斯",
                            "handle": "s450586793",
                            "avatar": "https://example.test/avatar.jpg",
                            "followCount": 27,
                            "likeCount": 1,
                            "collectionCount": 6,
                            "favoritesCount": {"publicCount": 2, "privateCount": 3, "likedCount": 1},
                        }
                    }
                }
            }
        )

        self.assertEqual(summary["uid"], "2024907479")
        self.assertEqual(summary["name"], "艾斯")
        self.assertEqual(summary["handle"], "s450586793")
        self.assertEqual(summary["avatar_url"], "https://example.test/avatar.jpg")
        self.assertEqual(summary["follow_count"], 27)
        self.assertEqual(summary["liked_collection_count"], 1)

    def test_extract_account_profile_reads_profile_counts(self):
        profile = batch_discovery._extract_account_profile(
            {
                "uid": 2024907479,
                "name": "艾斯",
                "handle": "s450586793",
                "avatar": "https://example.test/avatar.jpg",
                "followCount": 27,
                "likeCount": 1,
                "collectionCount": 6,
            }
        )

        self.assertEqual(profile["uid"], "2024907479")
        self.assertEqual(profile["name"], "艾斯")
        self.assertEqual(profile["handle"], "s450586793")
        self.assertEqual(profile["avatar_url"], "https://example.test/avatar.jpg")
        self.assertEqual(profile["follow_count"], 27)
        self.assertEqual(profile["liked_collection_count"], 1)

    def test_default_favorites_subscription_source_requires_real_handle(self):
        source = batch_discovery.default_favorites_subscription_source(
            "global",
            {"uid": "2073587493", "handle": "", "name": "艾斯"},
        )

        self.assertEqual(source, {})

    def test_extract_account_profile_ignores_plain_counter_id(self):
        profile = batch_discovery._extract_account_profile(
            {"data": {"id": 123, "unreadCount": 4}}
        )

        self.assertEqual(profile, {})

    def test_discover_cookie_account_profile_continues_until_handle_found(self):
        payloads = [
            {
                "uid": 2073587493,
                "name": "艾斯",
                "avatar": "https://example.test/avatar.jpg",
            },
            {
                "uid": 2073587493,
                "handle": "s450586793",
                "name": "艾斯",
            },
        ]

        with patch.object(batch_discovery, "_api_get_json", side_effect=payloads), \
                patch.object(batch_discovery, "_append_discovery_debug"):
            profile = batch_discovery.discover_cookie_account_profile("global", "token=ok")

        self.assertEqual(profile["uid"], "2073587493")
        self.assertEqual(profile["handle"], "s450586793")

    def test_api_headers_include_bearer_token_from_cookie(self):
        session = requests.Session()
        session.headers.update({"User-Agent": "MakerHub-Test"})

        headers = batch_discovery._build_api_headers(
            session,
            "token=access-token; refreshToken=refresh-token",
            "https://makerworld.com.cn/zh",
        )

        self.assertEqual(headers["Authorization"], "Bearer access-token")
        self.assertEqual(headers["token"], "access-token")
        self.assertEqual(headers["X-Token"], "access-token")
        self.assertEqual(headers["X-Access-Token"], "access-token")

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

    def test_followed_collection_discovery_stops_after_probe_budget(self):
        calls = []

        def fake_api_get_json(_session, **kwargs):
            calls.append(kwargs)
            return None

        with patch.object(batch_discovery, "_api_get_json", side_effect=fake_api_get_json), \
                patch.object(batch_discovery, "_append_discovery_debug") as debug_log:
            result = batch_discovery.discover_cookie_followed_collections(
                "cn",
                "token=ok",
                uid="2024907479",
                max_probe_count=3,
            )

        self.assertEqual(result["items"], [])
        self.assertEqual(len(calls), 3)
        self.assertEqual(debug_log.call_args.args[0], "cookie_followed_collections_probe_budget_exhausted")

    def test_collection_design_params_include_handle(self):
        params = batch_discovery._collection_designs_param_candidates(
            0,
            20,
            "https://makerworld.com/zh/@s450586793/collections/models",
            "s450586793",
        )

        self.assertTrue(params)
        self.assertTrue(all(item.get("handle") == "s450586793" for item in params))

    def test_bambulab_service_candidates_use_v1_without_legacy_api_prefix(self):
        candidates = batch_discovery._service_endpoint_candidates(
            "https://makerworld.com/zh/@s450586793/collections/models",
            "design-service",
            "/favorites/518732/designs",
        )

        self.assertEqual(
            candidates[0],
            "https://api.bambulab.com/v1/design-service/favorites/518732/designs",
        )
        self.assertNotIn(
            "https://api.bambulab.com/api/v1/design-service/favorites/518732/designs",
            candidates,
        )

    def test_collection_fallback_continues_when_candidate_misses_expected_total(self):
        partial_items = [
            {"url": f"https://makerworld.com/zh/models/{index}", "source_order": index}
            for index in range(6)
        ]
        complete_items = [
            {"url": f"https://makerworld.com/zh/models/{index}", "source_order": index}
            for index in range(17)
        ]

        with patch.object(
            batch_discovery,
            "_discover_collection_models_api",
            return_value={
                "source_url": "https://makerworld.com/zh/@s450586793/collections/models",
                "items": partial_items,
                "mode": "collection_models_api",
                "expected_total": 8,
            },
        ), patch.object(
            batch_discovery,
            "_resolve_collection_owner_uid",
            return_value="2024907479",
        ), patch.object(
            batch_discovery,
            "_discover_collection_models_by_lists",
            return_value={
                "source_url": "https://makerworld.com/zh/@s450586793/collections/models",
                "items": partial_items,
                "mode": "collection_models_lists",
                "expected_total": 8,
            },
        ), patch.object(
            batch_discovery,
            "_discover_by_html",
            return_value={
                "source_url": "https://makerworld.com/zh/@s450586793/collections/models",
                "items": complete_items,
                "mode": "html_fallback",
            },
        ):
            result = batch_discovery._discover_collection_with_fallbacks(
                requests.Session(),
                "https://makerworld.com/zh/@s450586793/collections/models",
                "token=ok",
                max_pages=2,
                page_expected_total=17,
            )

        self.assertEqual(len(result["items"]), 17)
        self.assertEqual(result["mode"], "html_fallback")
        self.assertEqual(result["expected_total"], 17)
        self.assertEqual(result["expected_total_source"], "collection_page_all_models")
        self.assertTrue(result["strict_expected_total"])

    def test_collection_fallback_prefers_result_closest_to_page_total(self):
        complete_items = [
            {"url": f"https://makerworld.com/zh/models/{index}", "source_order": index}
            for index in range(18)
        ]
        overbroad_items = [
            {"url": f"https://makerworld.com/zh/models/{index}", "source_order": index}
            for index in range(25)
        ]

        with patch.object(
            batch_discovery,
            "_discover_collection_models_api",
            return_value={
                "source_url": "https://makerworld.com/zh/@s450586793/collections/models",
                "items": complete_items,
                "mode": "collection_models_api",
                "expected_total": 18,
            },
        ), patch.object(
            batch_discovery,
            "_resolve_collection_owner_uid",
            return_value="2024907479",
        ), patch.object(
            batch_discovery,
            "_discover_collection_models_by_lists",
            return_value={
                "source_url": "https://makerworld.com/zh/@s450586793/collections/models",
                "items": overbroad_items,
                "mode": "collection_models_lists",
                "expected_total": 25,
            },
        ), patch.object(batch_discovery, "_discover_by_html") as html_fallback:
            result = batch_discovery._discover_collection_with_fallbacks(
                requests.Session(),
                "https://makerworld.com/zh/@s450586793/collections/models",
                "token=ok",
                max_pages=2,
                page_expected_total=18,
            )

        self.assertEqual(len(result["items"]), 18)
        self.assertEqual(result["mode"], "collection_models_api")
        self.assertEqual(result["expected_total"], 18)
        html_fallback.assert_not_called()

    def test_collection_fallback_retries_api_when_first_result_misses_page_total(self):
        partial_items = [
            {"url": f"https://makerworld.com/zh/models/{index}", "source_order": index}
            for index in range(6)
        ]
        complete_items = [
            {"url": f"https://makerworld.com/zh/models/{index}", "source_order": index}
            for index in range(17)
        ]
        api_results = [
            {
                "source_url": "https://makerworld.com/zh/@s450586793/collections/models",
                "items": partial_items,
                "mode": "collection_models_api",
                "expected_total": 6,
            },
            {
                "source_url": "https://makerworld.com/zh/@s450586793/collections/models",
                "items": complete_items,
                "mode": "collection_models_api",
                "expected_total": 17,
            },
        ]

        with patch.object(
            batch_discovery,
            "_discover_collection_models_api",
            side_effect=api_results,
        ) as api_mock, patch.object(
            batch_discovery,
            "_resolve_collection_owner_uid",
            return_value="2024907479",
        ), patch.object(batch_discovery, "_discover_collection_models_by_lists") as list_fallback, \
                patch.object(batch_discovery, "_discover_by_html") as html_fallback:
            result = batch_discovery._discover_collection_with_fallbacks(
                requests.Session(),
                "https://makerworld.com/zh/@s450586793/collections/models",
                "token=ok",
                max_pages=2,
                page_expected_total=17,
            )

        self.assertEqual(api_mock.call_count, 2)
        self.assertEqual(len(result["items"]), 17)
        self.assertEqual(result["mode"], "collection_models_api")
        list_fallback.assert_not_called()
        html_fallback.assert_not_called()

    def test_collection_fallback_uses_candidate_expected_total_when_page_total_missing(self):
        partial_items = [
            {"url": f"https://makerworld.com/zh/models/{index}", "source_order": index}
            for index in range(6)
        ]
        complete_items = [
            {"url": f"https://makerworld.com/zh/models/{index}", "source_order": index}
            for index in range(8)
        ]

        with patch.object(
            batch_discovery,
            "_discover_collection_models_api",
            return_value={
                "source_url": "https://makerworld.com/zh/@s450586793/collections/models",
                "items": partial_items,
                "mode": "collection_models_api",
                "expected_total": 8,
            },
        ), patch.object(
            batch_discovery,
            "_resolve_collection_owner_uid",
            return_value="2024907479",
        ), patch.object(
            batch_discovery,
            "_discover_collection_models_by_lists",
            return_value={
                "source_url": "https://makerworld.com/zh/@s450586793/collections/models",
                "items": complete_items,
                "mode": "collection_models_lists",
                "expected_total": 8,
            },
        ), patch.object(batch_discovery, "_discover_by_html") as html_fallback:
            result = batch_discovery._discover_collection_with_fallbacks(
                requests.Session(),
                "https://makerworld.com/zh/@s450586793/collections/models",
                "token=ok",
                max_pages=2,
                page_expected_total=None,
            )

        self.assertEqual(len(result["items"]), 8)
        self.assertEqual(result["mode"], "collection_models_lists")
        html_fallback.assert_not_called()

    def test_extract_page_links_preserves_page_order(self):
        html = """
        <a href="/zh/models/300">third</a>
        <script>{"url":"/zh/models/100"}</script>
        <a href="/zh/models/200">second</a>
        """

        links = batch_discovery._extract_page_links(html, "https://makerworld.com/zh/@ace/collections/models")

        self.assertEqual(
            links[:3],
            [
                "https://makerworld.com/zh/models/300",
                "https://makerworld.com/zh/models/100",
                "https://makerworld.com/zh/models/200",
            ],
        )


if __name__ == "__main__":
    unittest.main()
