import json
from pathlib import Path
from typing import Any, Optional

from app.core.settings import STATE_DIR, ensure_app_dirs


ARCHIVE_QUEUE_PATH = STATE_DIR / "archive_queue.json"
MISSING_3MF_PATH = STATE_DIR / "missing_3mf.json"
ORGANIZE_TASKS_PATH = STATE_DIR / "organize_tasks.json"


def _normalize_task_item(item: Any, default_status: str) -> dict:
    if isinstance(item, str):
        return {
            "id": "",
            "title": item,
            "status": default_status,
            "progress": 0,
            "message": "",
            "updated_at": "",
        }

    if not isinstance(item, dict):
        return {
            "id": "",
            "title": "",
            "status": default_status,
            "progress": 0,
            "message": "",
            "updated_at": "",
        }

    progress = item.get("progress")
    if progress is None:
        progress = item.get("percent") or item.get("percent_complete") or 0

    return {
        "id": str(item.get("id") or item.get("task_id") or ""),
        "title": str(item.get("title") or item.get("name") or item.get("url") or item.get("model_dir") or ""),
        "status": str(item.get("status") or default_status),
        "progress": int(progress or 0),
        "message": str(item.get("message") or item.get("detail") or ""),
        "updated_at": str(item.get("updated_at") or item.get("time") or item.get("created_at") or ""),
        "url": str(item.get("url") or ""),
    }


def _normalize_archive_queue(payload: Any) -> dict:
    if isinstance(payload, list):
        queued = [_normalize_task_item(item, "queued") for item in payload]
        return {"active": [], "queued": queued, "recent_failures": []}

    if not isinstance(payload, dict):
        return {"active": [], "queued": [], "recent_failures": []}

    active_items = payload.get("active") or payload.get("running") or []
    queued_items = payload.get("queued") or payload.get("items") or payload.get("pending") or []
    failed_items = payload.get("recent_failures") or payload.get("failed") or payload.get("failures") or []

    return {
        "active": [_normalize_task_item(item, "running") for item in active_items],
        "queued": [_normalize_task_item(item, "queued") for item in queued_items],
        "recent_failures": [_normalize_task_item(item, "failed") for item in failed_items],
    }


def _normalize_missing_3mf(payload: Any, fallback_items: Optional[list[dict]] = None) -> dict:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("items") or payload.get("models") or []
    else:
        items = []

    if not items and fallback_items:
        items = fallback_items

    normalized = []
    for item in items:
        if isinstance(item, str):
            normalized.append({"model_id": "", "title": item, "status": "missing"})
            continue
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "model_id": str(item.get("model_id") or item.get("id") or ""),
                "title": str(item.get("title") or item.get("name") or ""),
                "status": str(item.get("status") or "missing"),
            }
        )

    return {"items": normalized}


def _normalize_organize_tasks(payload: Any) -> dict:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("items") or payload.get("tasks") or []
    else:
        items = []

    normalized = []
    for item in items:
        if isinstance(item, str):
            normalized.append(
                {
                    "source_dir": item,
                    "target_dir": "",
                    "status": "pending",
                    "updated_at": "",
                    "move_files": True,
                }
            )
            continue
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "source_dir": str(item.get("source_dir") or item.get("source") or ""),
                "target_dir": str(item.get("target_dir") or item.get("target") or ""),
                "status": str(item.get("status") or "pending"),
                "updated_at": str(item.get("updated_at") or item.get("time") or ""),
                "move_files": bool(item.get("move_files", item.get("move", True))),
            }
        )

    return {"items": normalized}


class TaskStateStore:
    def __init__(self) -> None:
        ensure_app_dirs()

    def _read_json(self, path: Path, default: dict) -> dict:
        if not path.exists():
            path.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
            return default

        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            path.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
            return default

    def load_archive_queue(self) -> dict:
        payload = self._read_json(
            ARCHIVE_QUEUE_PATH,
            {"active": [], "queued": [], "recent_failures": []},
        )
        queue = _normalize_archive_queue(payload)
        queue["running_count"] = len(queue["active"])
        queue["queued_count"] = len(queue["queued"])
        queue["failed_count"] = len(queue["recent_failures"])
        return queue

    def load_missing_3mf(self, fallback_items: Optional[list[dict]] = None) -> dict:
        payload = self._read_json(MISSING_3MF_PATH, {"items": []})
        missing = _normalize_missing_3mf(payload, fallback_items=fallback_items)
        missing["count"] = len(missing["items"])
        return missing

    def load_organize_tasks(self) -> dict:
        payload = self._read_json(ORGANIZE_TASKS_PATH, {"items": []})
        tasks = _normalize_organize_tasks(payload)
        tasks["count"] = len(tasks["items"])
        return tasks
