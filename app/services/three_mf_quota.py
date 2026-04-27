import json
import threading
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from app.core.settings import STATE_DIR, ensure_app_dirs
from app.core.timezone import now as china_now
from app.services.three_mf import normalize_makerworld_source


THREE_MF_DAILY_QUOTA_PATH = STATE_DIR / "three_mf_daily_quota.json"
THREE_MF_DAILY_QUOTA_LOCK_PATH = STATE_DIR / "three_mf_daily_quota.lock"
DEFAULT_THREE_MF_DAILY_LIMIT = 100
_FALLBACK_LOCK = threading.RLock()


def _coerce_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return DEFAULT_THREE_MF_DAILY_LIMIT
    return max(0, limit)


def _source_label(source: str) -> str:
    if source == "global":
        return "国际区"
    if source == "cn":
        return "国区"
    return "MakerWorld"


def _quota_message(source: str, limit: int) -> str:
    return f"已达到 MakerHub 设置的{_source_label(source)}每日 3MF 下载上限（{limit}），今日暂停自动重试。"


def _empty_payload() -> dict[str, Any]:
    return {"items": {}}


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_payload()
    return payload if isinstance(payload, dict) else _empty_payload()


def _write_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


@contextmanager
def _quota_file_lock(lock_path: Path):
    ensure_app_dirs()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _FALLBACK_LOCK:
        lock_file = lock_path.open("a+")
        try:
            try:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            except Exception:
                fcntl = None
            yield
        finally:
            try:
                if "fcntl" in locals() and fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                lock_file.close()


def reserve_three_mf_download_slot(
    *,
    source: Any = "",
    url: Any = "",
    limit: Any = DEFAULT_THREE_MF_DAILY_LIMIT,
    model_id: str = "",
    model_url: str = "",
    instance_id: str = "",
    quota_path: Path = THREE_MF_DAILY_QUOTA_PATH,
    lock_path: Path = THREE_MF_DAILY_QUOTA_LOCK_PATH,
) -> dict[str, Any]:
    normalized_source = normalize_makerworld_source(source=source, url=url or model_url)
    normalized_limit = _coerce_limit(limit)
    if normalized_source not in {"cn", "global"}:
        return {"allowed": True, "source": normalized_source, "limit": normalized_limit}
    if normalized_limit <= 0:
        return {
            "allowed": True,
            "source": normalized_source,
            "limit": 0,
            "used": 0,
            "remaining": None,
            "unlimited": True,
        }

    now = china_now()
    today = now.date().isoformat()
    reset_at = (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).isoformat(timespec="seconds")

    with _quota_file_lock(lock_path):
        payload = _read_payload(quota_path)
        items = payload.get("items") if isinstance(payload.get("items"), dict) else {}
        current = items.get(normalized_source) if isinstance(items.get(normalized_source), dict) else {}
        if str(current.get("date") or "") != today:
            current = {"date": today, "used": 0, "limit": normalized_limit}

        used = int(current.get("used") or 0)
        if used >= normalized_limit:
            current.update(
                {
                    "date": today,
                    "used": used,
                    "limit": normalized_limit,
                    "last_blocked_at": now.isoformat(timespec="seconds"),
                    "last_model_id": str(model_id or ""),
                    "last_model_url": str(model_url or url or ""),
                    "last_instance_id": str(instance_id or ""),
                }
            )
            items[normalized_source] = current
            payload["items"] = items
            _write_payload(quota_path, payload)
            return {
                "allowed": False,
                "source": normalized_source,
                "limit": normalized_limit,
                "used": used,
                "date": today,
                "reset_at": reset_at,
                "message": _quota_message(normalized_source, normalized_limit),
            }

        used += 1
        current.update(
            {
                "date": today,
                "used": used,
                "limit": normalized_limit,
                "last_reserved_at": now.isoformat(timespec="seconds"),
                "last_model_id": str(model_id or ""),
                "last_model_url": str(model_url or url or ""),
                "last_instance_id": str(instance_id or ""),
            }
        )
        items[normalized_source] = current
        payload["items"] = items
        _write_payload(quota_path, payload)
        return {
            "allowed": True,
            "source": normalized_source,
            "limit": normalized_limit,
            "used": used,
            "remaining": max(normalized_limit - used, 0),
            "date": today,
            "reset_at": reset_at,
        }
