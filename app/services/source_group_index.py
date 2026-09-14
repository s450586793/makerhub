from typing import Any

from app.services import archive_model_index as index


def _group_query(group: dict[str, Any], *, include_source_deleted: bool = False) -> tuple[str, tuple[Any, ...]]:
    kind = str(group.get("kind") or "")
    conditions = {
        "local": "source = 'local' AND NOT deleted",
        "local_favorite": "favorite AND NOT deleted",
        "printed": "printed AND NOT deleted",
        "local_deleted": "deleted",
        "source_deleted": "source_deleted",
    }
    cte = index._archive_model_query_cte(include_source_deleted=include_source_deleted or kind == "source_deleted")
    if kind in conditions:
        return cte + f""", group_members AS (
            SELECT indexed_models.*, 0::bigint AS source_rank
            FROM indexed_models WHERE {conditions[kind]}
        ), source_total AS (SELECT 0::bigint AS count)
        """, ()
    if kind not in {"author", "collection", "favorite"}:
        raise ValueError("Unsupported source group")
    author_url = str(group.get("canonical_url") or "").rstrip("/")
    if author_url.endswith("/upload"):
        author_url = author_url[:-7]
    # 在数据库内解析当前来源成员，只取匹配目录，避免把全库模型带回 Python。
    cte += """, selected_state AS (
        SELECT item FROM makerhub_json_state
        CROSS JOIN LATERAL jsonb_array_elements(COALESCE(value -> 'items', '[]'::jsonb)) item
        WHERE key = 'subscriptions_state' AND item ->> 'id' = %s
    ), source_items AS MATERIALIZED (
        SELECT child, ordinality AS source_rank
        FROM selected_state CROSS JOIN LATERAL jsonb_array_elements(
            CASE WHEN jsonb_typeof(item -> 'current_items') = 'array' AND item -> 'current_items' <> '[]'::jsonb
                THEN item -> 'current_items'
                WHEN jsonb_typeof(item -> 'tracked_items') = 'array' THEN item -> 'tracked_items'
                ELSE '[]'::jsonb END
        ) WITH ORDINALITY children(child, ordinality)
    ), source_matches AS MATERIALIZED (
        SELECT matched.model_dir, min(source_rank) AS source_rank FROM source_items
        CROSS JOIN LATERAL (
            SELECT m.model_dir FROM archive_model_index m
            WHERE m.source <> 'local'
              AND m.model_dir NOT IN (SELECT model_dir FROM deleted_models)
              AND (m.model_id = CASE WHEN btrim(child ->> 'task_key') LIKE 'model:%%' THEN substr(btrim(child ->> 'task_key'), 7) END
                OR m.origin_url = btrim(child ->> 'task_key')
                OR m.origin_url = CASE
                    WHEN btrim(child ->> 'url') LIKE '/%%' THEN 'https://makerworld.com.cn' || btrim(child ->> 'url')
                    WHEN btrim(child ->> 'url') ~ '^https?://' THEN btrim(child ->> 'url')
                    WHEN btrim(child ->> 'url') <> '' THEN 'https://' || btrim(child ->> 'url') ELSE '' END)
            ORDER BY CASE WHEN ('model:' || m.model_id) = btrim(child ->> 'task_key')
                OR m.origin_url = btrim(child ->> 'task_key') THEN 0 ELSE 1 END, m.model_dir DESC
            LIMIT 1
        ) matched GROUP BY matched.model_dir
    ), group_members AS (
        SELECT indexed_models.*, COALESCE(source_matches.source_rank, 0) AS source_rank
        FROM indexed_models LEFT JOIN source_matches USING (model_dir)
        WHERE source_matches.model_dir IS NOT NULL
          OR (%s = 'author' AND NOT EXISTS (SELECT 1 FROM source_matches)
            AND source <> 'local' AND NOT deleted
            AND regexp_replace(regexp_replace(rtrim(model_json #>> '{author,url}', '/'),
                '/upload$', ''), '/[a-z]{2}/@', '/zh/@') = %s)
    ), source_total AS (
        SELECT COALESCE(NULLIF((SELECT count(*) FROM source_items), 0),
            (SELECT CASE WHEN item ->> 'last_discovered_count' ~ '^[0-9]+$'
                THEN (item ->> 'last_discovered_count')::bigint ELSE 0 END FROM selected_state), 0) AS count
    )
    """
    return cte, (str(group.get("subscription_id") or ""), kind, author_url)


def _ready() -> bool:
    return (index.archive_model_index_configured() and not index._database_temporarily_unavailable()
            and index.ensure_archive_model_index_schema()
            and index.archive_model_index_is_bootstrapped(archive_root=index.ARCHIVE_DIR))


def query_group_models(
    group: dict[str, Any], *, q: str = "", source: str = "all", tag: str = "",
    sort_key: str = "collectDate", page: int = 1, page_size: int = 8,
) -> dict[str, Any] | None:
    try:
        if not _ready():
            return None
        cte, scope_params = _group_query(group, include_source_deleted=str(tag).strip().lower() == "__source_deleted__")
        condition, filter_params, normalized_source = index._archive_model_query_filters(q, source, tag)
        if group.get("kind") in {"source_deleted", "local_deleted"}:
            condition = condition.replace("NOT deleted", "TRUE")
        sort_column = {"publishDate": "publish_ts", "downloads": "downloads", "likes": "likes", "prints": "prints"}.get(sort_key, "collect_ts")
        order = f"{sort_column} DESC, title DESC, model_dir ASC"
        if group.get("kind") in {"favorite", "collection"} and sort_key == "collectDate":
            order = "source_rank ASC, model_dir ASC"
        safe_page = max(1, int(page or 1))
        size = max(0, min(int(page_size), 120))
        offset = (safe_page - 1) * size
        source_count = "0"
        if group.get("kind") == "source_deleted":
            source_count = """(SELECT count(*) FROM (
                SELECT subscription_id FROM source_deleted_keys
                WHERE task_key IN (SELECT 'model:' || model_id FROM group_members)
                   OR task_key IN (SELECT origin_url FROM group_members)
                UNION SELECT 'remote_refresh' WHERE EXISTS (
                    SELECT 1 FROM group_members WHERE lower(model_json #>> '{remote_sync,source_deleted}') IN ('true', '1', 'yes', 'on')
                )
            ) deleted_sources)"""
        sql = cte + f""", filtered AS NOT MATERIALIZED (
            SELECT * FROM group_members WHERE {condition}
        ), page_models AS (
            SELECT * FROM filtered ORDER BY {order} LIMIT %s OFFSET %s
        ), previews AS (
            SELECT * FROM group_members ORDER BY collect_ts DESC, title DESC, model_dir ASC LIMIT 4
        )
        SELECT
            (SELECT count(*) FROM filtered) AS filtered_total,
            count(*) AS total,
            count(*) FILTER (WHERE source = 'cn') AS cn_count,
            count(*) FILTER (WHERE source = 'global') AS global_count,
            count(*) FILTER (WHERE source = 'local') AS local_count,
            count(DISTINCT nullif(btrim(author_name), '')) AS author_count,
            COALESCE(sum(likes), 0) AS likes_count,
            {source_count} AS source_count,
            (SELECT value ->> 'revision' FROM makerhub_metadata WHERE key = '{index.ARCHIVE_MODEL_INDEX_REVISION_KEY}') AS revision,
            (SELECT count FROM source_total) AS remote_model_count,
            COALESCE((SELECT jsonb_agg(model_json || jsonb_build_object(
                'model_dir', model_dir, 'local_flags', jsonb_build_object(
                    'favorite', favorite, 'printed', printed, 'deleted', deleted)
            ) ORDER BY {order}) FROM page_models), '[]'::jsonb) AS items,
            COALESCE((SELECT jsonb_agg(jsonb_build_object(
                'model_dir', model_dir, 'title', title, 'cover_url', cover_url, 'collect_ts', collect_ts,
                'author', jsonb_build_object('name', author_name,
                    'avatar_url', model_json #>> '{{author,avatar_url}}')
            ) ORDER BY collect_ts DESC, title DESC, model_dir ASC) FROM previews), '[]'::jsonb) AS preview_models
        FROM group_members
        """
        with index.database_connection() as connection:
            row = connection.execute(sql, (*scope_params, *filter_params, size, offset)).fetchone()
        return {
            "items": row["items"], "count": len(row["items"]), "total": row["total"],
            "filtered_total": row["filtered_total"], "page": safe_page, "page_size": size,
            "revision": row.get("revision"),
            "has_more": offset + size < row["filtered_total"], "tags": [],
            "source_counts": {"all": row["total"], "cn": row["cn_count"], "global": row["global_count"], "local": row["local_count"]},
            "filters": {"q": q, "source": normalized_source, "tag": tag, "sort": sort_key},
            "summary": {key: value for key, value in row.items() if key not in {"items", "filtered_total"}},
        }
    except Exception as exc:
        index._warn_once("source_group_query_failed", "来源分组数据库查询失败。", error=str(exc))
        return None


def query_group_tags(group: dict[str, Any], q: str = "", *, limit: int = 50) -> dict[str, Any] | None:
    try:
        if not _ready():
            return None
        cte, params = _group_query(group)
        size = max(1, min(int(limit), 100))
        with index.database_connection() as connection:
            rows = connection.execute(cte + """
                SELECT DISTINCT lower(btrim(tag_value)) AS tag FROM group_members
                CROSS JOIN LATERAL unnest(tags) tag_value
                WHERE btrim(tag_value) <> '' AND lower(btrim(tag_value)) ILIKE %s ESCAPE E'\\\\'
                ORDER BY tag LIMIT %s
            """, (*params, index._escaped_ilike_pattern(q.strip().lower()[:200]), size + 1)).fetchall()
        return {"items": [row["tag"] for row in rows[:size]], "has_more": len(rows) > size}
    except Exception as exc:
        index._warn_once("source_group_tags_failed", "来源分组标签查询失败。", error=str(exc))
        return None


def query_group_merge_candidates(group: dict[str, Any]) -> list[dict[str, Any]] | None:
    try:
        if not _ready():
            return None
        cte, params = _group_query(group)
        with index.database_connection() as connection:
            return connection.execute(cte + """
                SELECT model_dir, title, cover_url, source, model_json -> 'local_import' AS local_import
                FROM group_members WHERE source = 'local' AND NOT deleted ORDER BY model_dir
            """, params).fetchall()
    except Exception as exc:
        index._warn_once("source_group_merge_failed", "本地模型合并候选查询失败。", error=str(exc))
        return None
