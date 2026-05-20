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


if __name__ == "__main__":
    unittest.main()
