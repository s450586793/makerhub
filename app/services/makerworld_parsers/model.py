import json
import re
from html import unescape
from typing import Any, Optional

from app.services.makerworld_parsers.common import extract_model_id
from app.services.three_mf import normalize_makerworld_source


def _json_loads_maybe(raw: str) -> Optional[object]:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _skip_js_whitespace(text: str, start: int) -> int:
    idx = max(start, 0)
    while idx < len(text) and text[idx] in " \t\r\n":
        idx += 1
    return idx


def _extract_script_tag_payload(html_text: str, script_id: str) -> str:
    if not html_text or not script_id:
        return ""
    markers = (f'id="{script_id}"', f"id='{script_id}'")
    for marker in markers:
        search_from = 0
        while True:
            marker_idx = html_text.find(marker, search_from)
            if marker_idx < 0:
                break
            script_start = html_text.rfind("<script", 0, marker_idx)
            if script_start >= 0:
                tag_end = html_text.find(">", marker_idx + len(marker))
                if tag_end >= 0:
                    close_tag = html_text.find("</script>", tag_end + 1)
                    if close_tag >= 0:
                        return html_text[tag_end + 1:close_tag].strip().rstrip(";")
            search_from = marker_idx + len(marker)
    return ""


def _extract_balanced_json_object(text: str, start: int) -> str:
    if start < 0 or start >= len(text) or text[start] != "{":
        return ""
    depth = 0
    quote = ""
    escaped = False
    for idx in range(start, len(text)):
        char = text[idx]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "{":
            depth += 1
            continue
        if char == "}":
            depth -= 1
            if depth == 0:
                return text[start:idx + 1].strip().rstrip(";")
    return ""


def _extract_json_object_assignment(html_text: str, token: str) -> Optional[object]:
    if not html_text or not token:
        return None
    search_from = 0
    while True:
        token_idx = html_text.find(token, search_from)
        if token_idx < 0:
            return None
        eq_idx = html_text.find("=", token_idx + len(token))
        if eq_idx < 0:
            return None
        value_idx = _skip_js_whitespace(html_text, eq_idx + 1)
        if value_idx < len(html_text) and html_text[value_idx] == "{":
            raw = _extract_balanced_json_object(html_text, value_idx)
            data = _json_loads_maybe(raw)
            if data is not None:
                return data
        search_from = token_idx + len(token)


def extract_next_data(html_text: str) -> dict:
    for script_id in ("__NEXT_DATA__", "__NUXT__"):
        payload = _extract_script_tag_payload(html_text, script_id)
        data = _json_loads_maybe(payload)
        if isinstance(data, dict):
            return data

    for token in ("window.__NEXT_DATA__", "__NEXT_DATA__", "window.__NUXT__", "__NUXT__"):
        data = _extract_json_object_assignment(html_text, token)
        if isinstance(data, dict):
            return data

    patterns = [
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        r'__NEXT_DATA__\s*=\s*({.*?})\s*;',
        r'window\.__NEXT_DATA__\s*=\s*({.*?})\s*;',
        r'__NUXT__\s*=\s*({.*?})\s*;',
        r'window\.__NUXT__\s*=\s*({.*?})\s*;',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text, re.S)
        if not match:
            continue
        raw = (match.group(1) or "").strip().rstrip(";")
        data = _json_loads_maybe(raw)
        if data is not None:
            return data
    parse_patterns = [
        r'__NEXT_DATA__\s*=\s*JSON\.parse\((".*?")\)\s*;',
        r"__NEXT_DATA__\s*=\s*JSON\.parse\(('.*?')\)\s*;",
        r'__NUXT__\s*=\s*JSON\.parse\((".*?")\)\s*;',
        r"__NUXT__\s*=\s*JSON\.parse\(('.*?')\)\s*;",
    ]
    for pattern in parse_patterns:
        match = re.search(pattern, html_text, re.S)
        if not match:
            continue
        parsed = _json_loads_maybe((match.group(1) or "").strip())
        if isinstance(parsed, str):
            data = _json_loads_maybe(parsed)
            if data is not None:
                return data
    raise RuntimeError("未找到 __NEXT_DATA__")


def _get_nested(obj: dict, keys: list[str]):
    cur = obj
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _score_design_candidate(obj: dict) -> int:
    if not isinstance(obj, dict):
        return -1
    score = 0
    if isinstance(obj.get("instances"), list):
        score += 3
    if "designExtension" in obj or "summary" in obj or "summaryHtml" in obj:
        score += 2
    if "tags" in obj or "tagsOriginal" in obj:
        score += 1
    if "coverUrl" in obj or "coverImage" in obj or "thumbnail" in obj or "thumbnailUrl" in obj:
        score += 1
    if "likeCount" in obj or "downloadCount" in obj or "printCount" in obj:
        score += 1
    if "designCreator" in obj or "creatorName" in obj or "author" in obj or "user" in obj:
        score += 1
    if obj.get("id") is not None and obj.get("title"):
        score += 1
    return score


def _find_best_design(obj: object) -> Optional[dict]:
    best = None
    best_score = -1
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if "design" in cur and isinstance(cur.get("design"), dict):
                score = _score_design_candidate(cur["design"])
                if score > best_score:
                    best = cur["design"]
                    best_score = score
            score = _score_design_candidate(cur)
            if score > best_score:
                best = cur
                best_score = score
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return best if best_score >= 2 else None


def extract_design_from_next_data(next_data: dict) -> Optional[dict]:
    if not isinstance(next_data, dict):
        return None
    paths = [
        ["props", "pageProps", "design"],
        ["props", "pageProps", "data", "design"],
        ["props", "pageProps", "pageData", "design"],
        ["props", "pageProps", "payload", "design"],
        ["props", "pageProps", "designDetail"],
        ["props", "pageProps", "model"],
        ["props", "pageProps", "detail"],
    ]
    for path in paths:
        candidate = _get_nested(next_data, path)
        if isinstance(candidate, dict):
            if "design" in candidate and isinstance(candidate.get("design"), dict):
                return candidate["design"]
            return candidate
    page_props = _get_nested(next_data, ["props", "pageProps"]) or next_data.get("pageProps")
    return _find_best_design(page_props or next_data)


def parse_design_id(url: str) -> Optional[int]:
    design_id = extract_model_id(url)
    try:
        return int(design_id) if design_id else None
    except (TypeError, ValueError):
        return None


def _coerce_positive_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    if not text or not re.fullmatch(r"\d+", text):
        return None
    parsed = int(text)
    return parsed if parsed > 0 else None


def _extract_design_payload_id(design: dict) -> Optional[int]:
    if not isinstance(design, dict):
        return None
    for key in ("id", "designId", "designID", "modelId", "modelID", "model_id"):
        parsed = _coerce_positive_int(design.get(key))
        if parsed:
            return parsed
    return None


def _design_payload_title(design: dict) -> str:
    if not isinstance(design, dict):
        return ""
    for key in ("title", "name", "modelName", "designName"):
        value = design.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return ""


def design_payload_error(design: object, source_url: str) -> str:
    if not isinstance(design, dict):
        return "源端返回内容不是模型对象"
    expected_id = parse_design_id(source_url)
    payload_id = _extract_design_payload_id(design)
    if not payload_id:
        return "源端返回的模型 ID 为空或为 0"
    if expected_id and payload_id != expected_id:
        return f"源端返回的模型 ID 不匹配（期望 {expected_id}，实际 {payload_id}）"
    if not _design_payload_title(design):
        return "源端返回的模型标题为空"
    return ""


def normalize_design_payload_identity(design: dict, source_url: str) -> None:
    if not isinstance(design, dict):
        return
    payload_id = _extract_design_payload_id(design)
    if payload_id:
        design["id"] = payload_id
    if not design.get("url"):
        design["url"] = source_url


def append_api_base_candidate(bases: list[str], base: str, source: str) -> None:
    base = str(base or "").strip().rstrip("/")
    if not base:
        return
    base_source = normalize_makerworld_source(url=base)
    if source and base_source and base_source != source:
        return
    if base not in bases:
        bases.append(base)


def extract_api_host(html_text: str) -> Optional[str]:
    if not html_text:
        return None
    match = re.search(r'API_HOST"\s*:\s*"([^"]+)"', html_text)
    if not match:
        match = re.search(r"API_HOST'\s*:\s*'([^']+)'", html_text)
    if not match:
        return None
    host = (match.group(1) or "").strip()
    if not host:
        return None
    return host if host.startswith(("http://", "https://")) else f"https://{host}"


def is_cloudflare_challenge(html_text: str) -> bool:
    if not html_text:
        return False
    lowered = html_text.lower()
    strong_markers = [
        "<title>just a moment",
        "cf-chl",
        "cf_chl",
        "challenge-platform",
        "/cdn-cgi/challenge",
        "/cdn-cgi/challenge-platform",
        "cf-browser-verification",
        "checking your browser",
        "enable javascript and cookies to continue",
        "verify you are human",
        "cf-mitigated",
    ]
    if any(marker in lowered for marker in strong_markers):
        return True
    return "cloudflare" in lowered and any(
        marker in lowered
        for marker in ("attention required", "ray id", "please enable cookies", "security check")
    )


def is_makerworld_not_found_page(html_text: str) -> bool:
    if not html_text:
        return False
    lowered = unescape(str(html_text or "")).lower()
    compact = re.sub(r"\s+", "", lowered)
    explicit_markers = (
        "该模型可能被改为草稿",
        "下架或者设为私有",
        "下架或设为私有",
        "已下架",
        "设为私有",
        "转为草稿",
        "改为草稿",
        "页面不存在",
        "route not found",
        "\"detail\":\"not found\"",
    )
    if any(marker in lowered for marker in explicit_markers):
        return True
    compact_markers = (
        "该模型可能被改为草稿、下架或者设为私有",
        "该模型可能被改为草稿下架或者设为私有",
    )
    if any(marker in compact for marker in compact_markers):
        return True
    return "404" in lowered and any(
        marker in lowered for marker in ("not found", "model may have", "模型可能", "页面找不到")
    )


def unwrap_design_payload(payload: object) -> Optional[dict]:
    if not isinstance(payload, dict):
        return _find_best_design(payload)
    direct = _find_best_design(payload)
    if direct:
        return direct
    for key in ["data", "design", "result", "detail", "model", "info"]:
        candidate = payload.get(key)
        if isinstance(candidate, dict):
            if "design" in candidate and isinstance(candidate.get("design"), dict):
                return candidate["design"]
            picked = _find_best_design(candidate)
            if picked:
                return picked
    return None
