import json
from datetime import datetime
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
                "model_url": str(item.get("model_url") or item.get("url") or ""),
                "instance_id": str(item.get("instance_id") or item.get("profileId") or item.get("instanceId") or ""),
                "message": str(item.get("message") or ""),
                "updated_at": str(item.get("updated_at") or item.get("time") or ""),
            }
        )

    return {"items": normalized}


def _missing_3mf_key(item: dict) -> tuple[str, str, str]:
    normalized = _normalize_missing_3mf([item]).get("items", [])
    if not normalized:
        return ("", "", "")
    payload = normalized[0]
    return (
        str(payload.get("model_id") or ""),
        str(payload.get("instance_id") or ""),
        str(payload.get("title") or ""),
    )


def _matches_missing_3mf_item(
    item: dict,
    *,
    model_id: str = "",
    title: str = "",
    instance_id: str = "",
    model_url: str = "",
) -> bool:
    item_model_id = str(item.get("model_id") or "").strip()
    item_title = str(item.get("title") or "").strip()
    item_instance_id = str(item.get("instance_id") or "").strip()
    item_model_url = str(item.get("model_url") or "").strip()

    target_model_id = str(model_id or "").strip()
    target_title = str(title or "").strip()
    target_instance_id = str(instance_id or "").strip()
    target_model_url = str(model_url or "").strip()

    if target_model_id and item_model_id != target_model_id:
        return False
    if target_instance_id and item_instance_id != target_instance_id:
        return False
    if target_title and item_title != target_title:
        return False
    if target_model_url and item_model_url != target_model_url:
        return False

    return any((target_model_id, target_title, target_instance_id, target_model_url))


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
            self._write_json(path, default)
            return default

        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            self._write_json(path, default)
            return default

    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save_archive_queue(self, payload: dict) -> dict:
        normalized = _normalize_archive_queue(payload)
        self._write_json(
            ARCHIVE_QUEUE_PATH,
            {
                "active": normalized["active"],
                "queued": normalized["queued"],
                "recent_failures": normalized["recent_failures"],
            },
        )
        return self.load_archive_queue()

    def save_missing_3mf(self, payload: dict) -> dict:
        normalized = _normalize_missing_3mf(payload)
        self._write_json(MISSING_3MF_PATH, normalized)
        return self.load_missing_3mf()

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

    def enqueue_archive_task(self, item: dict) -> dict:
        payload = self._read_json(
            ARCHIVE_QUEUE_PATH,
            {"active": [], "queued": [], "recent_failures": []},
        )
        queued = payload.get("queued") or []
        queued.append(_normalize_task_item(item, "queued"))
        payload["queued"] = queued
        return self.save_archive_queue(payload)

    def start_archive_task(self, task_id: str) -> dict:
        payload = self._read_json(
            ARCHIVE_QUEUE_PATH,
            {"active": [], "queued": [], "recent_failures": []},
        )
        queued = payload.get("queued") or []
        active = payload.get("active") or []
        task = None
        remaining = []
        for item in queued:
            normalized = _normalize_task_item(item, "queued")
            if normalized["id"] == task_id and task is None:
                normalized["status"] = "running"
                normalized["updated_at"] = datetime.now().isoformat()
                task = normalized
                continue
            remaining.append(normalized)

        if task is None:
            for item in active:
                normalized = _normalize_task_item(item, "running")
                if normalized["id"] == task_id:
                    task = normalized
                else:
                    remaining.append(normalized)
            active = remaining
            remaining = queued

        if task is not None:
            active.append(task)

        payload["queued"] = remaining
        payload["active"] = active
        return self.save_archive_queue(payload)

    def update_active_task(self, task_id: str, **changes: Any) -> dict:
        payload = self._read_json(
            ARCHIVE_QUEUE_PATH,
            {"active": [], "queued": [], "recent_failures": []},
        )
        active = []
        for item in payload.get("active") or []:
            normalized = _normalize_task_item(item, "running")
            if normalized["id"] == task_id:
                normalized.update({key: value for key, value in changes.items() if value is not None})
                normalized["updated_at"] = datetime.now().isoformat()
            active.append(normalized)
        payload["active"] = active
        return self.save_archive_queue(payload)

    def complete_archive_task(self, task_id: str) -> dict:
        payload = self._read_json(
            ARCHIVE_QUEUE_PATH,
            {"active": [], "queued": [], "recent_failures": []},
        )
        payload["active"] = [
            _normalize_task_item(item, "running")
            for item in (payload.get("active") or [])
            if _normalize_task_item(item, "running")["id"] != task_id
        ]
        return self.save_archive_queue(payload)

    def fail_archive_task(self, task_id: str, message: str) -> dict:
        payload = self._read_json(
            ARCHIVE_QUEUE_PATH,
            {"active": [], "queued": [], "recent_failures": []},
        )

        failed_item = None
        active = []
        for item in payload.get("active") or []:
            normalized = _normalize_task_item(item, "running")
            if normalized["id"] == task_id:
                normalized["status"] = "failed"
                normalized["message"] = message
                normalized["updated_at"] = datetime.now().isoformat()
                failed_item = normalized
            else:
                active.append(normalized)

        queued = []
        for item in payload.get("queued") or []:
            normalized = _normalize_task_item(item, "queued")
            if normalized["id"] == task_id and failed_item is None:
                normalized["status"] = "failed"
                normalized["message"] = message
                normalized["updated_at"] = datetime.now().isoformat()
                failed_item = normalized
            else:
                queued.append(normalized)

        recent_failures = [_normalize_task_item(item, "failed") for item in (payload.get("recent_failures") or [])]
        if failed_item is not None:
            recent_failures.insert(0, failed_item)
            recent_failures = recent_failures[:20]

        payload["active"] = active
        payload["queued"] = queued
        payload["recent_failures"] = recent_failures
        return self.save_archive_queue(payload)

    def remove_recent_failures_for_model(self, model_id: str, url: str = "") -> dict:
        model_key = str(model_id or "").strip()
        url_key = str(url or "").strip()
        if not model_key and not url_key:
            return self.load_archive_queue()

        payload = self._read_json(
            ARCHIVE_QUEUE_PATH,
            {"active": [], "queued": [], "recent_failures": []},
        )
        remaining = []
        for item in payload.get("recent_failures") or []:
            normalized = _normalize_task_item(item, "failed")
            haystack = " ".join(
                [
                    str(normalized.get("url") or ""),
                    str(normalized.get("title") or ""),
                    str(normalized.get("id") or ""),
                ]
            )
            if model_key and model_key in haystack:
                continue
            if url_key and (normalized.get("url") == url_key or normalized.get("title") == url_key):
                continue
            remaining.append(normalized)

        payload["recent_failures"] = remaining
        return self.save_archive_queue(payload)

    def merge_missing_3mf_items(self, items: list[dict]) -> dict:
        payload = self._read_json(MISSING_3MF_PATH, {"items": []})
        existing = _normalize_missing_3mf(payload).get("items", [])
        merged: list[dict] = []
        seen = set()

        for item in existing + (items or []):
            normalized_list = _normalize_missing_3mf([item]).get("items", [])
            if not normalized_list:
                continue
            normalized = normalized_list[0]
            key = _missing_3mf_key(normalized)
            if key in seen:
                continue
            seen.add(key)
            merged.append(normalized)

        return self.save_missing_3mf({"items": merged})

    def replace_missing_3mf_for_model(self, model_id: str, items: list[dict]) -> dict:
        model_key = str(model_id or "").strip()
        payload = self._read_json(MISSING_3MF_PATH, {"items": []})
        existing = _normalize_missing_3mf(payload).get("items", [])
        remaining = [
            item for item in existing
            if str(item.get("model_id") or "").strip() != model_key or not model_key
        ]
        return self.merge_missing_3mf_items(remaining + (items or []))

    def remove_missing_3mf_for_model(self, model_id: str) -> dict:
        return self.replace_missing_3mf_for_model(model_id, [])

    def remove_missing_3mf_item(
        self,
        *,
        model_id: str = "",
        title: str = "",
        instance_id: str = "",
        model_url: str = "",
    ) -> dict:
        payload = self._read_json(MISSING_3MF_PATH, {"items": []})
        items = _normalize_missing_3mf(payload).get("items", [])
        remaining = [
            item for item in items
            if not _matches_missing_3mf_item(
                item,
                model_id=model_id,
                title=title,
                instance_id=instance_id,
                model_url=model_url,
            )
        ]
        return self.save_missing_3mf({"items": remaining})

    def update_missing_3mf_status(
        self,
        model_id: str,
        title: str = "",
        instance_id: str = "",
        model_url: str = "",
        status: str = "",
        message: str = "",
    ) -> dict:
        payload = self._read_json(MISSING_3MF_PATH, {"items": []})
        items = _normalize_missing_3mf(payload).get("items", [])
        now = datetime.now().isoformat()

        for item in items:
            if not _matches_missing_3mf_item(
                item,
                model_id=model_id,
                title=title,
                instance_id=instance_id,
                model_url=model_url,
            ):
                continue
            if status:
                item["status"] = status
            if message:
                item["message"] = message
            item["updated_at"] = now

        return self.save_missing_3mf({"items": items})
