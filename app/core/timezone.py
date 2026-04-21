from __future__ import annotations

import os
import time as time_module
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo

    CHINA_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

CHINA_TZ_NAME = "Asia/Shanghai"


def _pin_process_timezone() -> None:
    try:
        os.environ["TZ"] = CHINA_TZ_NAME
        if hasattr(time_module, "tzset"):
            time_module.tzset()
    except Exception:
        return


_pin_process_timezone()


def now() -> datetime:
    return datetime.now(CHINA_TZ)


def now_iso(*, timespec: str = "seconds") -> str:
    return now().isoformat(timespec=timespec)


def ensure_timezone(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=CHINA_TZ)
    return value.astimezone(CHINA_TZ)


def parse_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_timezone(value)

    raw = str(value or "").strip()
    if not raw:
        return None

    normalized = raw.replace("Z", "+00:00")
    if " " in normalized and "T" not in normalized:
        normalized = normalized.replace(" ", "T", 1)

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return ensure_timezone(parsed)


def from_timestamp(value: int | float) -> datetime:
    return datetime.fromtimestamp(float(value), CHINA_TZ)


def parse_timestamp(value: Any) -> int:
    if isinstance(value, datetime):
        return int(ensure_timezone(value).timestamp())
    if isinstance(value, (int, float)):
        raw = int(value)
        if raw > 10_000_000_000:
            return raw // 1000
        return raw

    raw = str(value or "").strip()
    if not raw:
        return 0
    if raw.isdigit():
        digits = int(raw)
        if digits > 10_000_000_000:
            return digits // 1000
        return digits

    parsed = parse_datetime(raw)
    if parsed is None:
        return 0
    return int(parsed.timestamp())
