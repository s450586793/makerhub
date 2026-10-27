import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import source_library_routes
from app.services import source_library
from tests.test_helpers import InMemoryDatabaseState


def model(number, *, title=None, imported=None, tags=None):
    return {
        "model_dir": f"LOCAL_{number}",
        "title": title or f"Fortress profile {number}",
        "source": "local",
        "author": {"name": "Maker"},
        "tags": tags or [],
        "stats": {"likes": number, "downloads": number, "prints": number},
        "collect_ts": number,
        "publish_ts": number,
        "local_flags": {"deleted": False, "favorite": False, "printed": False},
        "local_import": imported or {},
    }


class LocalMergeIdentityTests(unittest.TestCase):
    def suggestions(self, titles, imported=None):
        return source_library._build_local_merge_suggestions([
            model(index, title=title, imported=imported) for index, title in enumerate(titles)
        ])

    def test_arbitrary_short_suffixes_do_not_equate_different_works(self):
        for titles in (["Desert - Oasis", "Desert - Caravan"],
                       ["Dragon Statue (Mini)", "Dragon Statue (Large)"],
                       ["Garden_Castle", "Garden_Bridge"],
                       ["Database", "Datahead"],
                       ["Group1", "Group2"],
                       ["Fortress - Party", "Fortress - Partner"]):
            with self.subTest(titles=titles):
                self.assertEqual(self.suggestions(titles), [])

    def test_explicit_parts_and_chinese_components_still_group(self):
        for titles, expected in [
            (["Desert Fortress - Part 1", "Desert Fortress (Part 2)"], "Desert Fortress"),
            (["宇宙战士_头部", "宇宙战士_身体"], "宇宙战士"),
            (["火焰战士头部", "火焰战士身体"], "火焰战士"),
            (["Download Station - part 1", "Download Station - part 2"], "Download Station"),
            (["Hash Browns - part 1", "Hash Browns - part 2"], "Hash Browns"),
        ]:
            with self.subTest(titles=titles):
                suggestions = self.suggestions(titles)
                self.assertEqual(len(suggestions), 1)
                self.assertEqual(suggestions[0]["title"], expected)
                self.assertEqual(suggestions[0]["count"], 2)

    def test_technical_names_do_not_become_title_groups(self):
        for name in ("download-dirs-20261023", ".local-import-download-dirs-20261023",
                     ".local-imports-20261023", "download-dirs-v5", "import_uploads-old",
                     "import_uploads", "导入暂存", "本地模型导入", "hash-abcdef123456",
                     "a" * 64, "a12f1234-1234-4321-9876-123456789abc",
                     "20261023", "2026-10-23", "20261023-123456", "2026-10-23T12:34:56"):
            with self.subTest(name=name):
                self.assertEqual(self.suggestions([f"{name} - Part 1", f"{name} - Part 2"]), [])

    def test_technical_model_keys_cannot_bypass_name_checks(self):
        for key in ("download-dirs-20261023", "hash:" + "a" * 64,
                    "title_designer:download-dirs-20261023|maker",
                    "title_designer:.local-import-20261023|maker",
                    "title_designer:download-dirs-v5|maker", "title_designer:20261023|maker",
                    "title_designer:hash-abcdef123456|maker", "a" * 32,
                    "a12f1234-1234-4321-9876-123456789abc"):
            with self.subTest(key=key):
                self.assertEqual(self.suggestions(
                    ["Import record alpha", "Import record beta"], {"model_key": key}), [])

    def test_real_design_id_takes_priority_over_technical_title_or_key(self):
        suggestions = self.suggestions(["download-dirs-20261023", "hash-abcdef123456"],
                                       {"design_model_id": "1234567", "model_key": "hash:" + "a" * 64})
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["key"], "design_model:1234567")
        self.assertNotIn(suggestions[0]["title"], {"download-dirs", "hash", "download-dirs-20261023"})

    def test_explicit_numeric_design_ids_remain_reliable_even_when_they_resemble_technical_names(self):
        for identity in ("20261023", "12345678901234567890"):
            for field, value in (("design_model_id", identity), ("model_key", f"design_model:{identity}")):
                with self.subTest(field=field, value=value):
                    suggestions = self.suggestions(["Unrelated alpha", "Unrelated beta"], {field: value})
                    self.assertEqual(len(suggestions), 1)

    def test_technical_design_id_is_not_a_reliable_identity(self):
        for value in ("download-dirs-20261023", "hash:" + "a" * 64, "a" * 64,
                      ".local-import-20261023", "0", "unknown"):
            with self.subTest(value=value):
                self.assertEqual(self.suggestions(["Unrelated alpha", "Unrelated beta"],
                                                  {"design_model_id": value}), [])

    def test_meaningful_model_keys_group_without_requiring_parts_in_title(self):
        for key in ("title_designer:desert fortress|ace", "design_model:1234567", "custom:fortress"):
            with self.subTest(key=key):
                suggestions = self.suggestions(["Fortress - Classic", "Fortress - Detailed"],
                                               {"model_key": key})
                self.assertEqual(len(suggestions), 1)
                self.assertEqual(suggestions[0]["key"], "model_key:" + key)

    def test_explicit_original_filename_parts_can_group_generic_profile_titles(self):
        items = [model(number, title=f"Profile {number}", imported={
            "original_filename": f"Desert Fortress - Part {number}.3mf",
        }) for number in (1, 2)]
        self.assertEqual(source_library._build_local_merge_suggestions(items)[0]["title"], "Desert Fortress")

    def test_hidden_and_remote_models_do_not_enter_groups(self):
        first = model(1, imported={"design_model_id": "42"})
        second = model(2, imported={"design_model_id": "42"})
        second["local_flags"]["deleted"] = True
        third = model(3, imported={"design_model_id": "42"})
        third["source"] = "cn"
        self.assertEqual(source_library._build_local_merge_suggestions([first, second, third]), [])


class LocalMergeGroupFilteringTests(unittest.TestCase):
    def setUp(self):
        self.items = [model(number, imported={"design_model_id": "42"},
                            tags=["tool" if number % 2 else "other"]) for number in range(1, 14)]
        self.items += [model(number, title=f"Garden profile {number}",
                             imported={"design_model_id": "84"}) for number in (20, 21)]
        self.group = {"key": "local-organizer", "kind": "local", "route_kind": "source",
                      "model_dirs": [row["model_dir"] for row in self.items]}

    def fallback(self, **kwargs):
        with patch.object(source_library, "_indexed_group_payload", return_value=None), \
                patch.object(source_library, "_group_models", return_value=(
                    {self.group["key"]: self.group}, self.items, [])):
            return source_library.build_source_group_models_payload(
                self.group["kind"], self.group["key"], **kwargs)

    def test_fallback_filters_full_group_before_pagination_and_keeps_switches(self):
        result = self.fallback(merge_group="design_model:42", page=2, page_size=5)
        self.assertEqual([row["model_dir"] for row in result["items"]],
                         [f"LOCAL_{number}" for number in range(8, 3, -1)])
        self.assertEqual(result["filtered_total"], 13)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["filters"]["merge_group"], "design_model:42")
        self.assertEqual(result["active_merge_group"],
                         {"key": "design_model:42", "title": "Fortress profile 1", "count": 13})
        self.assertEqual({row["key"] for row in result["merge_suggestions"]},
                         {"design_model:42", "design_model:84"})

    def test_fallback_intersects_search_tag_source_and_sort_before_pagination(self):
        result = self.fallback(merge_group="design_model:42", q="Fortress", tag="tool",
                               source="local", sort_key="likes", page=2, page_size=3)
        self.assertEqual([row["model_dir"] for row in result["items"]], ["LOCAL_7", "LOCAL_5", "LOCAL_3"])
        self.assertEqual(result["filtered_total"], 7)
        self.assertEqual(result["active_merge_group"]["count"], 13)
        self.assertEqual(self.fallback(merge_group="design_model:42", source="cn")["items"], [])

    def test_unknown_or_stale_group_is_empty_and_never_falls_back_to_full_list(self):
        for key in ("design_model:missing", "title:download-dirs", "design_model:42 "):
            with self.subTest(key=key):
                result = self.fallback(merge_group=key)
                self.assertEqual(result["items"], [])
                self.assertEqual(result["filtered_total"], 0)
                self.assertIsNone(result["active_merge_group"])
                self.assertEqual(result["filters"]["merge_group"], key)
        self.items = self.items[-3:]
        self.group["model_dirs"] = [row["model_dir"] for row in self.items]
        self.assertEqual(self.fallback(merge_group="design_model:42")["items"], [])

    def test_filter_does_not_affect_other_source_scopes(self):
        self.group.update(key="author-one", kind="author")
        result = self.fallback(merge_group="design_model:42", page_size=120)
        self.assertEqual(len(result["items"]), 15)
        self.assertIsNone(result["active_merge_group"])
        self.assertEqual(result["filters"]["merge_group"], "")

    def test_nonlisted_group_can_be_filtered_without_truncating_membership(self):
        self.items = [model(group * 10 + number, imported={"design_model_id": str(group)})
                      for group in range(10) for number in (1, 2)]
        self.group["model_dirs"] = [row["model_dir"] for row in self.items]
        result = self.fallback(merge_group="design_model:9", page_size=120)
        self.assertEqual({row["model_dir"] for row in result["items"]}, {"LOCAL_91", "LOCAL_92"})
        self.assertEqual(len(result["merge_suggestions"]), 8)

    def test_indexed_filter_uses_server_candidates_and_sql_pagination_without_full_load(self):
        summary = {"total": 15, "author_count": 1, "local_count": 15, "preview_models": []}
        first = {"items": [], "total": 15, "count": 0, "summary": summary, "revision": 7}
        page = {"items": [self.items[6]], "total": 15, "count": 1, "summary": summary,
                "revision": 7, "filtered_total": 7, "filters": {"q": "Fortress", "tag": "tool"}}
        with InMemoryDatabaseState(), \
                patch.object(source_library, "_SOURCE_GROUP_MERGE_CACHE", {}), \
                patch.object(source_library, "query_group_models", side_effect=[first, page]) as query, \
                patch.object(source_library, "query_group_merge_candidates", return_value=self.items) as candidates, \
                patch.object(source_library, "_group_models", side_effect=AssertionError("Full library load")):
            result = source_library.build_source_group_models_payload(
                "local", "local-organizer", merge_group="design_model:42", q="Fortress",
                tag="tool", source="local", sort_key="likes", page=2, page_size=3)
        self.assertEqual(query.call_count, 2)
        self.assertEqual(query.call_args_list[0].kwargs["page_size"], 0)
        self.assertEqual(set(query.call_args.kwargs["merge_model_dirs"]),
                         {f"LOCAL_{number}" for number in range(1, 14)})
        self.assertEqual(query.call_args.kwargs["page"], 2)
        self.assertEqual(query.call_args.kwargs["page_size"], 3)
        self.assertEqual(query.call_args.kwargs["q"], "Fortress")
        self.assertEqual(query.call_args.kwargs["tag"], "tool")
        self.assertEqual(query.call_args.kwargs["source"], "local")
        self.assertEqual(query.call_args.kwargs["sort_key"], "likes")
        self.assertEqual(candidates.call_count, 1)
        self.assertEqual(result["active_merge_group"]["count"], 13)

    def test_indexed_unknown_group_passes_an_empty_server_allowlist(self):
        summary = {"total": 15, "author_count": 1, "local_count": 15, "preview_models": []}
        rows = [{"items": [], "total": 15, "count": 0, "summary": summary, "revision": 7}
                for _ in range(2)]
        with InMemoryDatabaseState(), \
                patch.object(source_library, "_SOURCE_GROUP_MERGE_CACHE", {}), \
                patch.object(source_library, "query_group_models", side_effect=rows) as query, \
                patch.object(source_library, "query_group_merge_candidates", return_value=self.items), \
                patch.object(source_library, "_group_models", side_effect=AssertionError("Full library load")):
            result = source_library.build_source_group_models_payload(
                "local", "local-organizer", merge_group="design_model:gone")
        self.assertEqual(query.call_args.kwargs["merge_model_dirs"], [])
        self.assertIsNone(result["active_merge_group"])
        self.assertEqual(result["filters"]["merge_group"], "design_model:gone")

    def test_indexed_nonlisted_group_reuses_cached_candidates_when_changing_pages(self):
        candidates = [model(group * 10 + number, imported={"design_model_id": str(group)})
                      for group in range(1, 11) for number in (1, 2)]
        summary = {"total": 20, "author_count": 1, "local_count": 20, "preview_models": []}
        rows = [{"items": [], "total": 20, "count": 0, "summary": summary, "revision": 7}
                for _ in range(4)]
        with InMemoryDatabaseState(), \
                patch.object(source_library, "_SOURCE_GROUP_MERGE_CACHE", {}), \
                patch.object(source_library, "query_group_models", side_effect=rows) as query, \
                patch.object(source_library, "query_group_merge_candidates", return_value=candidates) as read, \
                patch.object(source_library, "_group_models", side_effect=AssertionError("Full library load")):
            for page in (1, 2):
                result = source_library.build_source_group_models_payload(
                    "local", "local-organizer", merge_group="design_model:9", page=page, page_size=1)
                self.assertEqual(set(query.call_args.kwargs["merge_model_dirs"]), {"LOCAL_91", "LOCAL_92"})
                self.assertEqual(query.call_args.kwargs["page"], page)
                self.assertEqual(result["active_merge_group"]["count"], 2)
                self.assertNotIn("design_model:9", {row["key"] for row in result["merge_suggestions"]})
        self.assertEqual(read.call_count, 1)


class LocalMergeGroupRouteTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(source_library_routes.router)
        self.client = TestClient(app)
        self.url = "/api/source-library/sources/local/local-organizer"

    def test_route_forwards_exact_group_and_preserves_pagination_parameters(self):
        with patch.object(source_library_routes, "run_web_io", return_value={"items": []}) as run:
            result = self.client.get(self.url, params={
                "merge_group": "design_model:42 ", "q": "Fortress", "source": "local",
                "tag": "tool", "sort": "likes", "page": 2, "page_size": 3,
                "model_dirs": "LOCAL_untrusted",
            })
        self.assertEqual(result.status_code, 200)
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["merge_group"], "design_model:42 ")
        self.assertEqual(kwargs["q"], "Fortress")
        self.assertEqual(kwargs["source"], "local")
        self.assertEqual(kwargs["tag"], "tool")
        self.assertEqual(kwargs["sort_key"], "likes")
        self.assertEqual((kwargs["page"], kwargs["page_size"]), (2, 3))
        self.assertNotIn("model_dirs", kwargs)
        self.assertNotIn("merge_model_dirs", kwargs)

    def test_route_limit_keeps_legacy_response_pagination(self):
        with patch.object(source_library_routes, "run_web_io", return_value={"items": []}) as run:
            result = self.client.get(self.url, params={
                "merge_group": "design_model:42", "page": 4, "page_size": 8, "limit": 24,
            })
        self.assertEqual(result.status_code, 200)
        self.assertEqual((run.call_args.kwargs["page"], run.call_args.kwargs["page_size"]), (1, 24))
        self.assertEqual((result.json()["page"], result.json()["page_size"]), (4, 8))
        self.assertEqual(run.call_args.kwargs["merge_group"], "design_model:42")

    def test_route_rejects_oversized_group_key_without_querying(self):
        with patch.object(source_library_routes, "run_web_io") as run:
            result = self.client.get(self.url, params={"merge_group": "x" * 1001})
        self.assertEqual(result.status_code, 422)
        run.assert_not_called()

    def test_state_route_does_not_forward_merge_group(self):
        with patch.object(source_library_routes, "run_web_io", return_value={"items": []}) as run:
            result = self.client.get("/api/source-library/states/local_favorite",
                                     params={"merge_group": "design_model:42"})
        self.assertEqual(result.status_code, 200)
        self.assertNotIn("merge_group", run.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
