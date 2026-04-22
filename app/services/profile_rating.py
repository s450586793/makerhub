import math
import re
from typing import Any, Optional


_NUMERIC_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _parse_rating_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None

    text = str(value).strip()
    if not text:
        return None
    match = _NUMERIC_RE.search(text.replace(",", ""))
    if not match:
        return None
    try:
        numeric = float(match.group(0))
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def normalize_profile_rating(value: Any) -> Optional[float]:
    numeric = _parse_rating_number(value)
    if numeric is None or numeric <= 0:
        return None

    text = str(value).strip() if not isinstance(value, (int, float)) else ""
    if "%" in text and numeric > 1:
        numeric = numeric / 20
    elif numeric <= 1:
        numeric = numeric * 5

    if numeric > 5:
        numeric = 5
    return round(numeric, 2)
