import os
from contextlib import nullcontext
from unittest.mock import patch

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.services import archive_model_index as index


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
        with patch.object(index, "archive_model_index_configured", return_value=True), \
                patch.object(index, "_database_temporarily_unavailable", return_value=False), \
                patch.object(index, "ensure_archive_model_index_schema", return_value=True), \
                patch.object(index, "archive_model_index_is_bootstrapped", return_value=True), \
                patch.object(index, "database_connection", side_effect=lambda: nullcontext(connection)), \
                patch.object(index, "database_json_state_signature", return_value=("flags", "v1")), \
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
