from __future__ import annotations

from contextlib import contextmanager
import os
from typing import Any
from urllib.parse import urlparse


DOMESTIC_PROXY_BYPASS_HOSTS = (
    "makerworld.com.cn",
    "api.bambulab.cn",
)
PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)


def _hostname(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    return (parsed.hostname or "").strip().lower()


def _proxy_value(proxy_config: Any, key: str) -> Any:
    if isinstance(proxy_config, dict):
        return proxy_config.get(key)
    return getattr(proxy_config, key, None)


def is_domestic_proxy_bypass_url(url: Any) -> bool:
    host = _hostname(url)
    return any(host == domain or host.endswith(f".{domain}") for domain in DOMESTIC_PROXY_BYPASS_HOSTS)


def should_bypass_proxy_for_target(target_url: Any = "", *, platform: str = "") -> bool:
    if str(platform or "").strip().lower() == "cn":
        return True
    return is_domestic_proxy_bypass_url(target_url)


def proxy_mapping(proxy_config: Any, target_url: Any = "", *, platform: str = "") -> dict[str, str]:
    if should_bypass_proxy_for_target(target_url, platform=platform):
        return {}
    if not proxy_config or not bool(_proxy_value(proxy_config, "enabled")):
        return {}
    http_proxy = str(_proxy_value(proxy_config, "http_proxy") or "").strip()
    https_proxy = str(_proxy_value(proxy_config, "https_proxy") or "").strip()
    proxies: dict[str, str] = {}
    if http_proxy:
        proxies["http"] = http_proxy
    if https_proxy:
        proxies["https"] = https_proxy
    elif http_proxy:
        proxies["https"] = http_proxy
    return proxies


def proxy_url(proxy_config: Any, target_url: Any = "", *, platform: str = "") -> str:
    proxies = proxy_mapping(proxy_config, target_url, platform=platform)
    return proxies.get("https") or proxies.get("http") or ""


def effective_proxy_cache_state(proxy_config: Any, target_url: Any = "", *, platform: str = "") -> dict[str, Any]:
    if should_bypass_proxy_for_target(target_url, platform=platform):
        return {"enabled": False, "bypass": "domestic"}
    return {
        "enabled": bool(_proxy_value(proxy_config, "enabled")),
        "http": str(_proxy_value(proxy_config, "http_proxy") or ""),
        "https": str(_proxy_value(proxy_config, "https_proxy") or ""),
    }


def _with_no_proxy_entry(value: str, entry: str) -> str:
    parts = [item.strip() for item in str(value or "").split(",") if item.strip()]
    lowered = {item.lower() for item in parts}
    if entry.lower() not in lowered:
        parts.append(entry)
    return ",".join(parts)


@contextmanager
def temporary_proxy_env(config: Any, target_url: Any = "", *, platform: str = ""):
    proxy_config = config.get("proxy", config) if isinstance(config, dict) else getattr(config, "proxy", config)
    previous = {key: os.environ.get(key) for key in PROXY_ENV_KEYS}
    bypass = should_bypass_proxy_for_target(target_url, platform=platform)

    try:
        if bypass:
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                os.environ.pop(key, None)
            no_proxy = previous.get("NO_PROXY") or previous.get("no_proxy") or str(_proxy_value(proxy_config, "no_proxy") or "")
            for host in DOMESTIC_PROXY_BYPASS_HOSTS:
                no_proxy = _with_no_proxy_entry(no_proxy, host)
                no_proxy = _with_no_proxy_entry(no_proxy, f".{host}")
            os.environ["NO_PROXY"] = no_proxy
            os.environ["no_proxy"] = no_proxy
        elif bool(_proxy_value(proxy_config, "enabled")):
            http_proxy = str(_proxy_value(proxy_config, "http_proxy") or "")
            https_proxy = str(_proxy_value(proxy_config, "https_proxy") or "")
            no_proxy = str(_proxy_value(proxy_config, "no_proxy") or "")
            if http_proxy:
                os.environ["HTTP_PROXY"] = http_proxy
                os.environ["http_proxy"] = http_proxy
            if https_proxy:
                os.environ["HTTPS_PROXY"] = https_proxy
                os.environ["https_proxy"] = https_proxy
            if no_proxy:
                os.environ["NO_PROXY"] = no_proxy
                os.environ["no_proxy"] = no_proxy
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
