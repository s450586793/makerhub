from __future__ import annotations

import re
from typing import Any


def looks_like_html_message(text: str) -> bool:
    head = str(text or "").strip().lower()[:1200]
    if not head:
        return False
    if head.startswith("<!doctype html") or "<html" in head:
        return True
    return bool(re.search(r"<(html|head|body|script|title|div|meta|style)\b", head))


def sanitize_message_text(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    if looks_like_html_message(text):
        lowered = text.lower()
        if any(token in lowered for token in ("cloudflare", "cf-browser-verification", "cf-chl", "__cf_bm", "cf_clearance")):
            return "返回了风控校验页，通常是 Cookie 失效、代理异常或站点触发了 Cloudflare 校验。"
        return "返回了 HTML 页面，通常是 Cookie 失效、代理错误或站点风控页。"
    text = re.sub(r"\s+", " ", text).strip()
    return text[:400]


def normalize_source_refresh_text(value: Any) -> str:
    return str(value or "").replace("远端刷新", "源端刷新")


def normalize_source_refresh_item(item: dict) -> dict:
    normalized = dict(item or {})
    normalized["message"] = normalize_source_refresh_text(normalized.get("message") or "")
    return normalized


def normalize_task_item(item: Any, default_status: str) -> dict:
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
        "message": sanitize_message_text(item.get("message") or item.get("detail") or ""),
        "updated_at": str(item.get("updated_at") or item.get("time") or item.get("created_at") or ""),
        "url": str(item.get("url") or ""),
        "mode": str(item.get("mode") or ""),
        "meta": item.get("meta") if isinstance(item.get("meta"), dict) else {},
    }


def organizer_event_message(payload: dict[str, Any], status: str) -> str:
    raw_message = str(payload.get("message") or payload.get("error") or "").strip()
    if raw_message:
        return sanitize_message_text(raw_message)
    event = str(payload.get("event") or "").strip()
    if event == "organized":
        return "本地 3MF 已整理入库。"
    if event == "duplicate_skipped":
        return "本地 3MF 与模型库现有配置重复，已跳过。"
    if event == "deleted_model_skipped":
        return "命中 MakerHub 本地删除标记，已阻止重新入库。"
    if status == "failed":
        return "本地 3MF 整理失败。"
    return ""
