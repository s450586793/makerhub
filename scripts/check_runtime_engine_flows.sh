#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${MAKERHUB_BASE_URL:-http://127.0.0.1:8000}"
USERNAME="${MAKERHUB_USERNAME:-admin}"
PASSWORD="${MAKERHUB_PASSWORD:-admin}"
COOKIE_FILE="$(mktemp)"
trap 'rm -f "$COOKIE_FILE"' EXIT

echo "[runtime-flow] login"
curl -fsS -c "$COOKIE_FILE" -b "$COOKIE_FILE" \
  -H 'Content-Type: application/json' \
  -X POST "$BASE_URL/api/auth/login" \
  --data "{\"username\":\"$USERNAME\",\"password\":\"$PASSWORD\"}" >/dev/null

echo "[runtime-flow] dashboard"
curl -fsS -b "$COOKIE_FILE" "$BASE_URL/api/dashboard" >/tmp/makerhub-dashboard.json

echo "[runtime-flow] tasks"
curl -fsS -b "$COOKIE_FILE" "$BASE_URL/api/tasks" >/tmp/makerhub-tasks.json

echo "[runtime-flow] runtime"
curl -fsS -b "$COOKIE_FILE" "$BASE_URL/api/runtime" >/tmp/makerhub-runtime.json

echo "[runtime-flow] source refresh"
curl -fsS -b "$COOKIE_FILE" "$BASE_URL/api/source-refresh" >/tmp/makerhub-source-refresh.json

echo "[runtime-flow] subscriptions"
curl -fsS -b "$COOKIE_FILE" "$BASE_URL/api/subscriptions" >/tmp/makerhub-subscriptions.json

python3 - <<'PY'
import json

for path in [
    "/tmp/makerhub-dashboard.json",
    "/tmp/makerhub-tasks.json",
    "/tmp/makerhub-runtime.json",
    "/tmp/makerhub-source-refresh.json",
    "/tmp/makerhub-subscriptions.json",
]:
    with open(path, "r", encoding="utf-8") as fh:
        json.load(fh)
print("[runtime-flow] json payloads valid")
PY
