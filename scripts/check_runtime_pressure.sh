#!/usr/bin/env bash
set -euo pipefail

docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'makerhub|self-update' || true
docker stats --no-stream --format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' makerhub-app makerhub-worker makerhub-postgres || true
docker exec -i makerhub-postgres psql -U makerhub -d makerhub -P pager=off -x <<'SQL'
SELECT updated_at AT TIME ZONE 'Asia/Shanghai' AS cn_updated_at,
       jsonb_array_length(value->'active') AS active,
       jsonb_array_length(value->'queued') AS queued,
       jsonb_array_length(value->'recent_failures') AS failures
FROM makerhub_json_state WHERE key='archive_queue';
SELECT state, wait_event_type, wait_event, count(*)
FROM pg_stat_activity
WHERE datname=current_database()
GROUP BY state, wait_event_type, wait_event
ORDER BY count(*) DESC;
SELECT created_at AT TIME ZONE 'Asia/Shanghai' AS cn_time, event, left(message, 160) AS message
FROM makerhub_logs
WHERE file_name = 'business.log' AND category = 'archive'
ORDER BY created_at DESC, id DESC LIMIT 8;
SQL
