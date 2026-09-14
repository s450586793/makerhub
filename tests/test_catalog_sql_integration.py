import os
from contextlib import nullcontext
from unittest.mock import patch

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.services import archive_model_index as index
from app.core import database


@pytest.fixture
def sql_catalog():
    database_url = os.environ.get("MAKERHUB_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("MAKERHUB_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as connection:
        connection.execute("""
            CREATE TEMP TABLE archive_model_index (
                model_dir text, model_id text, origin_url text, source text,
                title text, author_name text, cover_url text, collect_ts bigint,
                tags text[], model_json jsonb
            )
        """)
        connection.execute("CREATE TEMP TABLE makerhub_json_state (key text, value jsonb)")
        connection.execute("CREATE TEMP TABLE makerhub_metadata (key text, value jsonb)")
        connection.execute("INSERT INTO makerhub_metadata VALUES (%s, %s)", (index.ARCHIVE_MODEL_INDEX_REVISION_KEY, Jsonb({"revision": 1})))
        for number in range(1, 9):
            connection.execute("INSERT INTO archive_model_index VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (
                f"M{number}", str(number), f"https://makerworld.com/zh/models/{number}",
                "local" if number <= 6 else "global", f"Model {number}", f"Author {number % 2}",
                f"/archive/M{number}/cover.png", number, ["tool", "100%_literal"],
                Jsonb({"remote_sync": {"source_deleted": number == 8}}),
            ))
        states = {
            "model_flags": {"favorites": ["M1", "M2", "M6", "M7"], "printed": ["M3", "M7"], "deleted": ["M6", "M8"]},
            "app_config": {"subscriptions": [{"id": "s1", "mode": "author_upload"}, {"id": "s2", "mode": "collection_models"}]},
            "subscriptions_state": {"items": [
                {"id": "s1", "tracked_items": [{"task_key": "model:7"}, {"task_key": "model:8"}], "current_items": [{"task_key": "model:8"}]},
                {"id": "s2", "tracked_items": [{"task_key": "model:1"}], "current_items": []},
            ]},
        }
        for key, value in states.items():
            connection.execute("INSERT INTO makerhub_json_state VALUES (%s,%s)", (key, Jsonb(value)))
        for field in ("likes", "prints", "downloads", "publish_ts"):
            connection.execute(f"ALTER TABLE archive_model_index ADD {field} bigint DEFAULT 0")
        with patch.object(index, "archive_model_index_configured", return_value=True), \
                patch.object(index, "_database_temporarily_unavailable", return_value=False), \
                patch.object(index, "ensure_archive_model_index_schema", return_value=True), \
                patch.object(index, "archive_model_index_is_bootstrapped", return_value=True), \
                patch.object(index, "database_connection", side_effect=lambda: nullcontext(connection)), \
                patch.object(index, "database_json_state_revision", return_value=("flags", "v1")), \
                patch.object(index, "_ARCHIVE_MODEL_TAGS_CACHE", {}):
            yield connection


def test_organizer_sql_preserves_counts_deleted_sources_and_preview_order(sql_catalog):
    result = index.query_organizer_model_groups()
    assert result is not None
    assert result["visible_model_count"] == 6
    groups = result["groups"]
    assert {key: value["model_count"] for key, value in groups.items()} == {
        "local-organizer": 5, "local_favorite": 3, "printed": 2, "source_deleted": 2, "local_deleted": 2,
    }
    assert groups["local-organizer"]["author_count"] == 2
    assert groups["local_favorite"]["local_count"] == 2
    assert groups["source_deleted"]["source_count"] == 2
    assert [item["model_dir"] for item in groups["local-organizer"]["preview_models"]] == ["M5", "M4", "M3", "M2"]
    assert groups["local-organizer"]["preview_models"][0]["cover_url"] == "/archive/M5/cover.png"
    sql_catalog.execute("DELETE FROM archive_model_index")
    assert index.query_organizer_model_groups() == {"visible_model_count": 0, "groups": {}}


def test_tag_search_and_archive_identity_lookup_execute_real_sql(sql_catalog):
    assert index.query_archive_model_tags("%_") == {"items": ["100%_literal"], "has_more": False}
    assert index.query_archive_model_tags("", limit=1) == {"items": ["100%_literal"], "has_more": True}
    rows = index.lookup_archive_model_keys({"model:1", "model:7", "https://makerworld.com/zh/models/8"})
    assert [row["id"] for row in rows] == ["7", "8"]
    assert [row["id"] for row in index.lookup_archive_model_keys({"model:7", "model:8"}, model_dirs={"M8"})] == ["8"]


def test_source_deletion_summary_does_not_rescan_every_key_for_every_model(sql_catalog):
    sql_catalog.execute("""INSERT INTO archive_model_index
        SELECT 'bulk' || n, n::text, 'https://makerworld.com/zh/models/' || n,
            'global', 'Model ' || n, 'Author', '', n, ARRAY['tool'], '{}'::jsonb
        FROM generate_series(1000, 1999) n
    """)
    state = {"items": [{"id": "s1", "current_items": [], "tracked_items": [{"task_key": f"model:{n}"} for n in range(1000, 1800)]}]}
    sql_catalog.execute("UPDATE makerhub_json_state SET value = %s WHERE key = 'subscriptions_state'", (Jsonb(state),))
    sql_catalog.execute("ANALYZE archive_model_index")
    captured = []

    class RecordingConnection:
        def execute(self, sql, params=None):
            captured.append((sql, params))
            return sql_catalog.execute(sql, params)

    with patch.object(index, "database_connection", side_effect=lambda: nullcontext(RecordingConnection())):
        result = index.query_organizer_model_groups()
    assert result["groups"]["source_deleted"]["model_count"] == 801
    sql, params = captured[-1]
    plan = sql_catalog.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql, params).fetchone()["QUERY PLAN"][0]["Plan"]
    nodes = [plan]
    while nodes:
        node = nodes.pop()
        if node.get("CTE Name") in {"source_deleted_keys", "indexed_models"}:
            assert node["Actual Loops"] <= 2, node
        nodes.extend(node.get("Plans", []))


def test_compact_queue_summary_uses_one_snapshot_and_preserves_full_counts(sql_catalog):
    queue = {
        "active": [{"id": "active"}],
        "queued": [{"id": str(n), "status": "paused", "blocked_reason": "needs_verification",
                    "url": "https://makerworld.com.cn/zh/models/1"} for n in range(12)],
        "recent_failures": None,
    }
    sql_catalog.execute("INSERT INTO makerhub_json_state VALUES (%s,%s)", ("archive_queue", Jsonb(queue)))
    with patch.object(database, "initialize_database"), patch.object(database, "database_connection", side_effect=lambda: nullcontext(sql_catalog)) as connections:
        result = database.load_json_state_archive_queue_verification_summary("archive_queue", item_limit=2)
        assert connections.call_count == 1
        assert result["cn"] == 12
        assert result["global"] == 0
        assert result["arrays"]["queued"]["count"] == 12
        assert len(result["arrays"]["queued"]["items"]) == 2
        assert result["arrays"]["recent_failures"] == {"count": 0, "items": []}
        empty = database.load_json_state_archive_queue_verification_summary("absent", item_limit=0)
        assert empty["arrays"]["active"] == {"count": 0, "items": []}


def test_revision_only_lookup_tracks_updates_and_delete_recreate(sql_catalog):
    sql_catalog.execute("CREATE UNIQUE INDEX ON makerhub_json_state (key)")
    sql_catalog.execute("ALTER TABLE makerhub_json_state ADD revision bigint DEFAULT 0")
    sql_catalog.execute("ALTER TABLE makerhub_json_state ADD updated_at timestamptz DEFAULT clock_timestamp()")
    with patch.object(database, "initialize_database"), patch.object(database, "database_connection", side_effect=lambda: nullcontext(sql_catalog)):
        first = database.load_json_state_revisions(["model_flags", "absent"])
        assert first["absent"] == (0, "")
        database.save_json_state("model_flags", {"favorites": ["M2"]})
        second = database.load_json_state_revisions(["model_flags"])
        assert second["model_flags"] != first["model_flags"]
        database.delete_json_state("model_flags")
        database.save_json_state("model_flags", {"favorites": ["M2"]})
        assert database.load_json_state_revisions(["model_flags"])["model_flags"] != first["model_flags"]


def test_group_sql_paginates_local_and_hidden_states(sql_catalog):
    from app.services import source_group_index
    result = source_group_index.query_group_models({"kind": "local"}, page=2, page_size=2)
    assert result is not None
    assert result["total"] == 5
    assert result["filtered_total"] == 5
    assert result["has_more"]
    assert [item["model_dir"] for item in result["items"]] == ["M3", "M2"]
    assert len(result["summary"]["preview_models"]) == 4
    deleted = source_group_index.query_group_models({"kind": "local_deleted"})
    assert [item["model_dir"] for item in deleted["items"]] == ["M8", "M6"]
    assert source_group_index.query_group_models({"kind": "local"}, q="no match")["total"] == 5
    assert source_group_index.query_group_models({"kind": "local"}, q="no match")["filtered_total"] == 0


def test_group_sql_preserves_source_order_fallback_and_scoped_tags(sql_catalog):
    from app.services import source_group_index
    sql_catalog.execute("UPDATE archive_model_index SET source = 'global'")
    state = {"items": [{"id": "s1", "current_items": [
        {"task_key": "model:2"}, {"task_key": "model:7"}, {"task_key": "model:2"},
        {"url": "https://makerworld.com/zh/models/4"}, {"task_key": "model:8"},
    ]}]}
    sql_catalog.execute("UPDATE makerhub_json_state SET value = %s WHERE key = 'subscriptions_state'", (Jsonb(state),))
    group = {"kind": "favorite", "subscription_id": "s1"}
    result = source_group_index.query_group_models(group, page_size=2)
    assert result is not None
    assert [item["model_dir"] for item in result["items"]] == ["M2", "M7"]
    assert result["total"] == 3
    assert result["summary"]["remote_model_count"] == 5
    assert [item["model_dir"] for item in source_group_index.query_group_models(group, page=2, page_size=2)["items"]] == ["M4"]
    assert source_group_index.query_group_tags(group, "%_") == {"items": ["100%_literal"], "has_more": False}
    state["items"][0]["tracked_items"] = state["items"][0].pop("current_items")
    sql_catalog.execute("UPDATE makerhub_json_state SET value = %s WHERE key = 'subscriptions_state'", (Jsonb(state),))
    assert source_group_index.query_group_models(group)["total"] == 3
    assert source_group_index.query_group_models({"kind": "favorite", "subscription_id": "missing"})["total"] == 0


def test_group_preview_does_not_return_unrequested_model_payloads(sql_catalog):
    from app.services import source_group_index
    result = source_group_index.query_group_models({"kind": "local"}, page_size=0)
    assert result is not None
    assert result["items"] == []
    assert len(result["summary"]["preview_models"]) == 4
    assert all("model_json" not in preview for preview in result["summary"]["preview_models"])


def test_author_group_falls_back_to_profile_and_hidden_state_filters_stay_consistent(sql_catalog):
    from app.services import source_group_index
    sql_catalog.execute("UPDATE archive_model_index SET model_json = model_json || %s WHERE model_dir IN ('M7', 'M8')",
                        (Jsonb({"author": {"url": "https://makerworld.com/en/@alice/upload"}}),))
    result = source_group_index.query_group_models({"kind": "author", "subscription_id": "missing", "canonical_url": "https://makerworld.com/zh/@alice/upload"})
    assert [item["model_dir"] for item in result["items"]] == ["M7"]
    hidden = source_group_index.query_group_models({"kind": "local_deleted"}, tag="__favorite__")
    assert [item["model_dir"] for item in hidden["items"]] == ["M6"]
    assert hidden["total"] == 2
    assert hidden["filtered_total"] == 1
    deleted = source_group_index.query_group_models({"kind": "local_favorite"}, tag=" __SOURCE_DELETED__ ")
    assert [item["model_dir"] for item in deleted["items"]] == ["M7"]


def test_large_source_group_uses_indexed_member_lookup(sql_catalog):
    from app.services import source_group_index
    sql_catalog.execute("CREATE INDEX ON archive_model_index (model_id)")
    sql_catalog.execute("CREATE INDEX ON archive_model_index (origin_url)")
    sql_catalog.execute("""INSERT INTO archive_model_index (model_dir, model_id, origin_url, source, title, author_name, cover_url, collect_ts, tags, model_json)
        SELECT 'bulk' || n, n::text, 'https://makerworld.com/zh/models/' || n,
            'global', 'Model ' || n, 'Author', '', n, ARRAY['tool'], '{}'::jsonb
        FROM generate_series(1000, 5500) n""")
    state = {"items": [{"id": "s1", "current_items": [{"task_key": f"model:{n}"} for n in range(1000, 5000)]}]}
    sql_catalog.execute("UPDATE makerhub_json_state SET value = %s WHERE key = 'subscriptions_state'", (Jsonb(state),))
    sql_catalog.execute("ANALYZE archive_model_index")
    captured = []

    class RecordingConnection:
        def execute(self, sql, params=None):
            captured.append((sql, params))
            return sql_catalog.execute(sql, params)

    with patch.object(index, "database_connection", side_effect=lambda: nullcontext(RecordingConnection())):
        result = source_group_index.query_group_models({"kind": "favorite", "subscription_id": "s1"}, page_size=12)
    assert result["total"] == 4000
    assert len(result["items"]) == 12
    sql, params = captured[-1]
    plan = sql_catalog.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql, params).fetchone()["QUERY PLAN"][0]["Plan"]
    nodes = [plan]
    while nodes:
        node = nodes.pop()
        if node.get("Relation Name") == "archive_model_index" and node.get("Node Type") == "Seq Scan":
            assert node["Actual Loops"] <= 2, node
        nodes.extend(node.get("Plans", []))
