from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.timezone import now_iso as china_now_iso


def looks_like_html_error(text: str) -> bool:
    head = str(text or "").strip().lower()[:1200]
    if not head:
        return False
    if head.startswith("<!doctype html") or "<html" in head:
        return True
    return bool(re.search(r"<(html|head|body|script|title|div|meta|style)\b", head))


def sanitize_remote_refresh_message(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    if looks_like_html_error(text):
        lowered = text.lower()
        if any(token in lowered for token in ("cloudflare", "cf-browser-verification", "cf-chl", "__cf_bm", "cf_clearance")):
            return "源端刷新返回了风控校验页，通常是 Cookie 失效、代理异常或站点触发了 Cloudflare 校验。"
        return "源端刷新返回了 HTML 页面，通常是 Cookie 失效、代理错误或站点风控页。"
    text = re.sub(r"\s+", " ", text).strip()
    return text[:400]


def json_safe_remote_refresh_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe_remote_refresh_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe_remote_refresh_value(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe_remote_refresh_value(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def remote_refresh_result_record(
    *,
    model_dir: str,
    title: str,
    url: str,
    status: str,
    message: str,
    metrics: dict[str, Any] | None = None,
    change_labels: list[Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_model_dir = str(model_dir or "").strip()
    clean_status = str(status or "success").strip() or "success"
    clean_metrics = dict(metrics or {}) if isinstance(metrics, dict) else {}
    clean_meta = dict(meta or {}) if isinstance(meta, dict) else {}
    clean_meta.setdefault("model_dir", clean_model_dir)
    if clean_metrics:
        clean_meta["metrics"] = clean_metrics
    labels = [str(item).strip() for item in (change_labels or []) if str(item).strip()]
    if labels:
        clean_meta["change_labels"] = labels
        clean_meta["change_summary"] = "，".join(labels)
    return {
        "id": clean_model_dir,
        "title": str(title or clean_model_dir or "未命名模型"),
        "url": str(url or ""),
        "status": clean_status,
        "progress": 100 if clean_status in {"success", "source_deleted"} else 0,
        "message": sanitize_remote_refresh_message(message, clean_status),
        "updated_at": china_now_iso(),
        "meta": clean_meta,
    }


def remote_refresh_batch_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = [item for item in records if isinstance(item, dict)]
    failed_records = [item for item in normalized if str(item.get("status") or "") == "failed"]
    source_deleted_records = [item for item in normalized if str(item.get("status") or "") == "source_deleted"]
    skipped_records = [item for item in normalized if str(item.get("status") or "") == "skipped"]
    failure_samples: list[dict[str, Any]] = []
    for item in failed_records[:10]:
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        failure_samples.append(
            {
                "model_dir": str(meta.get("model_dir") or item.get("id") or ""),
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "message": str(item.get("message") or ""),
            }
        )
    return {
        "records": normalized,
        "recent_items": list(reversed(normalized))[:50],
        "failed": len(failed_records),
        "skipped": len(skipped_records),
        "source_deleted": len(source_deleted_records),
        "failure_samples": failure_samples,
    }


def build_success_message(change_labels: list[str]) -> str:
    effective_labels = [label for label in change_labels if label != "已检查，无远端变化"]
    if not effective_labels:
        return "源端刷新完成，已检查，未发现远端内容变化。"
    return f"源端刷新完成：{'，'.join(effective_labels)}。"


def batch_scope_message(*, eligible_total: int, remaining_total: int) -> str:
    if eligible_total <= 0:
        return "当前没有可刷新的远端模型。"
    return (
        f"当前可刷新 {eligible_total} 个模型，"
        f"剩余 {max(int(remaining_total or 0), 0)} 个待补跑。"
    )
