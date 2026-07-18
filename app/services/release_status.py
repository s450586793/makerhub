from __future__ import annotations

import re
import time
from typing import Any


GITHUB_LATEST_RELEASE_URL = "https://api.github.com/repos/s450586793/makerhub/releases/latest"
_RELEASE_TAG_PATTERN = re.compile(r"^v?(\d+\.\d+\.\d+)$")


def read_latest_release_version(
    session: Any,
    *,
    proxies: dict[str, str] | None = None,
    url: str = GITHUB_LATEST_RELEASE_URL,
) -> dict[str, str]:
    """Read the version from the latest published GitHub Release only."""
    started = time.perf_counter()
    headers = dict(getattr(session, "headers", {}) or {})
    headers["Accept"] = "application/vnd.github+json"
    response = session.get(
        url,
        headers=headers,
        proxies=proxies or None,
        timeout=(6, 15),
        allow_redirects=True,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or bool(payload.get("draft")) or bool(payload.get("prerelease")):
        raise ValueError("GitHub 未返回可用的正式发布版本")
    tag_name = str(payload.get("tag_name") or "").strip()
    match = _RELEASE_TAG_PATTERN.fullmatch(tag_name)
    if not match:
        raise ValueError("GitHub 发布标签不是有效的语义化版本")
    return {
        "version": match.group(1),
        "source": "github_release",
        "elapsed_ms": str(round((time.perf_counter() - started) * 1000, 1)),
        "used_proxy": "true" if bool(proxies) else "false",
    }
