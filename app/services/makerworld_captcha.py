from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any

from app.services import cloakbrowser_session as browser
from app.services import makerworld_browser_client as client
from app.services.makerworld_parsers.model import is_cloudflare_challenge


_CAPTCHA_ID = re.compile(r"[0-9a-fA-F]{32}")
_VISIBLE_ASCII = re.compile(r"[\x21-\x7e]+")
_PROOF_FIELDS = ("captcha_id", "lot_number", "pass_token", "gen_time", "captcha_output")
_API_SERVERS = ("gcaptcha4.geetest.com", "gcaptcha4.geevisit.com", "gcaptcha4.gsensebot.com")
_STATIC_SERVERS = ("static.geetest.com", "static.geevisit.com")
_PROOF_ERROR = "验证码结果无效，请重新验证。"
_MAX_RESPONSE_BYTES = 256 * 1024


def _official_servers(payload: dict[str, Any], camel_key: str, snake_key: str,
                      allowed: tuple[str, ...]) -> list[str] | None:
    values = payload.get(camel_key, payload.get(snake_key, list(allowed)))
    if not isinstance(values, list) or not 1 <= len(values) <= len(allowed):
        return None
    servers = []
    for value in values:
        if not isinstance(value, str) or value.lower() not in allowed:
            return None
        if value.lower() not in servers:
            servers.append(value.lower())
    return servers


def challenge_config(payload: Any) -> dict[str, Any] | None:
    """只返回官网验证码 SDK 所需的公开配置，不转发任意地址或响应字段。"""
    if not isinstance(payload, dict):
        return None
    captcha_id = payload.get("captchaId", payload.get("captcha_id", payload.get("captchaID")))
    if not isinstance(captcha_id, str) or not _CAPTCHA_ID.fullmatch(captcha_id):
        return None
    api_servers = _official_servers(payload, "apiServers", "api_servers", _API_SERVERS)
    static_servers = _official_servers(payload, "staticServers", "static_servers", _STATIC_SERVERS)
    if api_servers is None or static_servers is None:
        return None
    return {"captcha_id": captcha_id, "api_servers": api_servers, "static_servers": static_servers}


def encode_proof(proof: Any, captcha_id: str) -> str:
    """按 MW 官网格式编码官方 SDK 的五字段结果，错误文本不包含票据。"""
    if (not isinstance(captcha_id, str) or not _CAPTCHA_ID.fullmatch(captcha_id)
            or not isinstance(proof, dict) or set(proof) != set(_PROOF_FIELDS)
            or proof.get("captcha_id") != captcha_id):
        raise ValueError(_PROOF_ERROR)
    for key in _PROOF_FIELDS:
        value = proof[key]
        limit = {"captcha_id": 32, "lot_number": 128, "pass_token": 512, "gen_time": 13, "captcha_output": 8192}[key]
        if not isinstance(value, str) or not 1 <= len(value) <= limit or not _VISIBLE_ASCII.fullmatch(value):
            raise ValueError(_PROOF_ERROR)
    if not re.fullmatch(r"[0-9]{10,13}", proof["gen_time"]):
        raise ValueError(_PROOF_ERROR)
    normalized = {key: proof[key] for key in _PROOF_FIELDS}
    return base64.b64encode(json.dumps(normalized, separators=(",", ":"), ensure_ascii=True).encode("ascii")).decode("ascii")


def fetch_authorization(
    platform: str, profile_id: str, target: dict[str, str], *,
    proxy_config: Any = None, proof_header: str = "",
) -> dict[str, Any]:
    """用既有关联 profile 单发授权 JSON 请求，人工等待期间不保留页面。"""
    instance_id = target.get("instance_id") if isinstance(target, dict) else None
    if (platform not in {"cn", "global"} or not isinstance(profile_id, str) or not profile_id.strip()
            or not isinstance(instance_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", instance_id)):
        raise ValueError("验证码下载目标无效，请重新打开验证。")
    if not isinstance(proof_header, str) or len(proof_header) > 16384:
        raise ValueError(_PROOF_ERROR)
    if proof_header:
        try:
            base64.b64decode(proof_header, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError(_PROOF_ERROR) from None
    origin = browser.PLATFORM_ORIGINS[platform]
    endpoint = f"{origin}/api/v1/design-service/instance/{instance_id}/f3mf?type=download&fileType=3mf"
    headers = {"Accept": "application/json, text/plain, */*", "Origin": origin, "Referer": f"{origin}/"}
    if proof_header:
        headers["x-bbl-captcha-result"] = proof_header
    response = client.makerworld_browser_managed_authorization(
        platform, endpoint, profile_id=profile_id, proxy_config=proxy_config,
        headers=headers, timeout_seconds=30,
    )
    safe_headers = {str(key).lower(): str(value)[:1000]
                    for key, value in response.headers.items() if str(key).lower() == "retry-after"}
    status_code = response.status_code if type(response.status_code) is int else 0
    result = {"status_code": status_code, "payload": {}, "headers": safe_headers}
    text = response.text if isinstance(response.text, str) else ""
    challenge = any(str(key).lower() == "cf-mitigated" and str(value).lower() == "challenge"
                    for key, value in response.headers.items())
    html = ("html" in str(response.content_type).lower()
            or bool(re.match(r"\s*<(?:!doctype\s+html|html|title)\b", text, re.IGNORECASE)))
    if challenge or (html and is_cloudflare_challenge(text[:_MAX_RESPONSE_BYTES])):
        result["payload"] = {"code": "MAKERHUB_CLOUDFLARE"}
        return result
    payload = None
    if len(text.encode("utf-8")) <= _MAX_RESPONSE_BYTES:
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            pass
    result["payload"] = payload if isinstance(payload, dict) else {"code": "MAKERHUB_INVALID_AUTHORIZATION_RESPONSE"}
    return result
