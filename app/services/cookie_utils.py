import re
from typing import Any


COOKIE_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
HEADER_LINE_RE = re.compile(r"^[A-Za-z0-9-]+\s*:")
CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]+")


def sanitize_cookie_header(raw_cookie: Any) -> str:
    text = str(raw_cookie or "")
    if not text:
        return ""

    text = text.replace("\ufeff", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    raw_lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not raw_lines:
        return ""

    has_cookie_prefix = any(line.lower().startswith("cookie:") for line in raw_lines)
    lines: list[str] = []
    for line in raw_lines:
        clean_line = CONTROL_CHAR_RE.sub("", line).strip()
        if not clean_line:
            continue
        if clean_line.lower().startswith("cookie:"):
            clean_line = clean_line.split(":", 1)[1].strip()
        elif has_cookie_prefix and HEADER_LINE_RE.match(clean_line):
            continue
        elif not has_cookie_prefix and HEADER_LINE_RE.match(clean_line):
            continue
        if clean_line:
            lines.append(clean_line)

    if not lines:
        return ""

    cookies: list[str] = []
    for part in "; ".join(lines).split(";"):
        clean_part = CONTROL_CHAR_RE.sub("", part).strip()
        if "=" not in clean_part:
            continue
        key, value = clean_part.split("=", 1)
        clean_key = key.strip()
        if not clean_key or not COOKIE_NAME_RE.fullmatch(clean_key):
            continue
        cookies.append(f"{clean_key}={value.strip()}")

    return "; ".join(cookies)


def parse_cookie_values(raw_cookie: Any) -> dict[str, str]:
    values: dict[str, str] = {}
    for part in sanitize_cookie_header(raw_cookie).split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        clean_key = key.strip()
        if clean_key:
            values[clean_key] = value.strip()
    return values


def extract_auth_token(raw_cookie: Any) -> str:
    cookies = parse_cookie_values(raw_cookie)
    return (
        cookies.get("token")
        or cookies.get("access_token")
        or cookies.get("accessToken")
        or ""
    )
