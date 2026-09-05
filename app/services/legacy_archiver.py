import hashlib
import json
import math
import os
import random
import re
import shutil
import sys
import threading
import time
import zipfile
from fnmatch import fnmatchcase
from html import escape, unescape
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit

"""
archiver.py (app2)
提取自 mw_fetch5.0.py，作为可导入模块使用：
- 使用 archive_model(url, cookie, download_dir, logs_dir, logger=None)
- 不包含全局 URL/COOKIE/OUT_DIR 配置
- 保持 mw_fetch5.0 的采集、3MF 获取、实例/图片处理逻辑
"""

import requests
from bs4 import BeautifulSoup
from app.core.timezone import now as china_now, now_iso as china_now_iso, parse_datetime
from app.services.business_logs import append_business_log
from app.services.asset_downloader import (
    AssetDownloadError,
    download_file,
    download_with_fresh_session,
    run_asset_tasks,
    safe_asset_url,
)
from app.services.cookie_utils import extract_auth_token, parse_cookie_values, sanitize_cookie_header
from app.services.makerworld_browser_client import (
    MakerWorldBrowserError,
    makerworld_browser_get_json,
    makerworld_browser_get_text,
)
from app.services.makerworld_parsers.model import (
    append_api_base_candidate as _append_api_base_candidate,
    design_payload_error as _design_payload_error,
    extract_api_host as _extract_api_host,
    extract_design_from_next_data,
    extract_next_data,
    is_cloudflare_challenge as _is_cloudflare_challenge,
    is_makerworld_not_found_page as _is_makerworld_not_found_page,
    normalize_design_payload_identity as _normalize_design_payload_identity,
    parse_design_id as _parse_design_id,
    unwrap_design_payload as _unwrap_design_payload,
)
from app.services.makerworld_parsers.comments import (
    _collect_comment_tree,
    _collect_comments_from_payload,
    _comment_list_payload_declares_empty,
    _comment_list_payload_hit_count,
    _comment_list_payload_total,
    _comment_numeric,
    _comment_reply_count,
    _comment_reply_items,
    _comment_tree_items,
    _count_comment_threads,
    _extract_comment_reply_payload_has_more,
    _merge_threaded_comment_list,
    extract_comment_list_items as _extract_comment_list_items,
    extract_comment_replies as _extract_comment_replies_from_payload,
    extract_comment_sections as _extract_comment_sections,
    normalize_threaded_comments,
    resolve_comment_count as _resolved_comment_count,
)
from app.services.profile_rating import normalize_profile_rating
from app.services.resource_limiter import resource_slot
from app.services.three_mf import (
    THREE_MF_NOT_DOWNLOADABLE_STATE,
    describe_three_mf_failure,
    is_three_mf_download_prohibited,
    is_three_mf_daily_download_limited,
    merge_three_mf_failure,
    normalize_makerworld_source,
    normalize_three_mf_failure_state,
)
from app.services.three_mf_quota import reserve_three_mf_download_slot


VOLATILE_ASSET_QUERY_KEYS = {
    "auth_key",
    "authkey",
    "expires",
    "signature",
    "token",
    "ts",
    "timestamp",
    "x-oss-process",
    "x-oss-signature",
    "x-oss-signature-method",
    "x-oss-signature-version",
    "x-oss-date",
    "x-oss-expires",
    "x-amz-algorithm",
    "x-amz-credential",
    "x-amz-date",
    "x-amz-expires",
    "x-amz-signature",
    "x-amz-signedheaders",
    "image_process",
    "imageprocess",
    "imagemogr2",
    "imageview2",
    "resize",
    "thumb",
    "thumbnail",
    "width",
    "height",
    "format",
    "quality",
    "webp",
    "w",
    "h",
    "q",
}

SAFE_VERIFICATION_PROVIDERS = frozenset({"geetest4", "turnstile", "unknown"})
SAFE_VERIFICATION_CHALLENGE_TYPES = frozenset({"icon_click", "slider", "checkbox", "unknown"})
SAFE_REASON_CODES = frozenset(
    {
        "aborted",
        "ambiguous_candidates",
        "ambiguous_gap",
        "attempts_exhausted",
        "candidate_count_invalid",
        "challenge_unchanged",
        "challenge_unsupported",
        "checkbox_unavailable",
        "cleanup_failed",
        "click_layout_invalid",
        "click_target_unavailable",
        "completed",
        "confidence_too_low",
        "discovery_failed",
        "distance_invalid",
        "empty_screenshot",
        "gap_not_found",
        "geometry_invalid",
        "image_base64_invalid",
        "image_decode_failed",
        "image_dimensions_invalid",
        "image_format_invalid",
        "image_foreground_missing",
        "image_size_invalid",
        "image_width_invalid",
        "input_too_large",
        "interaction_failed",
        "json_invalid",
        "low_confidence",
        "mode_invalid",
        "no_challenge",
        "outcome_failed",
        "payload_invalid",
        "piece_restore_failed",
        "piece_unavailable",
        "request_invalid",
        "slider_geometry_invalid",
        "solved",
        "timeout",
        "trajectory_out_of_bounds",
        "unsupported_fields",
        "verification_failed",
        "vision_rejected",
    }
)
VOLATILE_ASSET_QUERY_PREFIXES = (
    "x-amz-",
    "x-oss-",
    "response-",
    "utm_",
)


def log(*args):
    if args and args[0] is None:
        args = args[1:]
    if args and callable(args[0]):
        logger = args[0]
        message = " ".join(str(arg) for arg in args[1:])
        try:
            logger(message)
            return
        except Exception:
            args = args[1:]
    print("[MW-FETCH]", *args)


def log_section(title: str):
    log("")
    log("=" * 10, title, "=" * 10)


def _log_perf(label: str, started_at: float, logger=None, **extra) -> float:
    elapsed_ms = round((time.perf_counter() - started_at) * 1000, 1)
    extra_parts = []
    for key, value in extra.items():
        if value in ("", None, [], {}, ()):
            continue
        extra_parts.append(f"{key}={value}")
    suffix = f" ({', '.join(extra_parts)})" if extra_parts else ""
    message = f"[perf] {label}: {elapsed_ms} ms{suffix}"
    if logger is None:
        log(message)
    else:
        log(logger, message)
    return elapsed_ms


def _trace_url(value: str) -> str:
    try:
        parsed = urlparse(str(value or ""))
        if not parsed.netloc:
            return ""
        return urlunsplit((parsed.scheme or "https", parsed.netloc, parsed.path or "", "", ""))
    except Exception:
        return ""


def _scrapling_trace(
    stage: str,
    result=None,
    *,
    url: str = "",
    state: str = "",
    used: bool = False,
    fallback: str = "",
    logger=None,
    level: str = "info",
    **extra,
) -> None:
    engine = str(getattr(result, "engine", "") or "").strip()
    status_code = int(getattr(result, "status_code", 0) or 0)
    error = str(getattr(result, "error", "") or "").strip()
    ok = bool(getattr(result, "ok", False))
    safe_extra = {key: value for key, value in extra.items() if value not in (None, "", [], {}, ())}
    trace_state = state or ("ok" if ok else "failed")
    message = (
        f"[scrapling] stage={stage} engine={engine or '-'} "
        f"status={status_code or '-'} state={trace_state} used={bool(used)}"
    )
    if fallback:
        message = f"{message} fallback={fallback}"
    if logger is not None:
        log(logger, message)
    try:
        append_business_log(
            "scrapling",
            "fetch_trace",
            message,
            level=level,
            stage=stage,
            engine=engine,
            status_code=status_code,
            ok=ok,
            state=trace_state,
            used=bool(used),
            fallback=fallback,
            url=_trace_url(url),
            error=error[:300] if error else "",
            **safe_extra,
        )
    except Exception:
        return


def _record_missing_3mf_summary(logs_dir: Path, base_name: str, missing_3mf: list[dict], logger=None) -> None:
    if not missing_3mf:
        return
    sample = []
    for item in missing_3mf[:5]:
        if not isinstance(item, dict):
            continue
        sample.append(
            {
                "instance_id": str(item.get("id") or item.get("instance_id") or ""),
                "title": str(item.get("title") or item.get("name") or "")[:120],
                "state": str(item.get("downloadState") or item.get("status") or "missing"),
                "message": str(item.get("downloadMessage") or item.get("message") or "未获取到 3MF 下载地址")[:180],
            }
        )
    try:
        append_business_log(
            "archive",
            "missing_3mf_detected",
            "归档发现缺失 3MF 实例。",
            base_name=base_name,
            count=len(missing_3mf),
            sample=sample,
        )
    except Exception:
        return
    log(logger, "缺失 3MF 已记录到数据库日志。")


def _mark_instance_3mf_download_failed(inst: dict, error: Exception, logger=None) -> None:
    message = f"3MF 静态文件下载失败：{error}"
    if isinstance(error, AssetDownloadError):
        state = "http_error"
    else:
        state = normalize_three_mf_failure_state("", message)
        if state == "missing":
            state = "http_error"
    inst["downloadState"] = state
    inst["downloadMessage"] = message
    log(logger, "3MF 静态文件下载失败:", inst.get("id") or inst.get("title") or inst.get("fileName") or "", error)


def _instance_3mf_file_exists(work_dir: Path, inst: dict) -> bool:
    file_name = str(inst.get("fileName") or "").strip()
    return bool(file_name and (work_dir / "instances" / file_name).exists())


def _missing_3mf_instances(instances: list[dict], work_dir: Path, *, require_local_file: bool = False) -> list[dict]:
    missing = []
    for inst in instances:
        if not isinstance(inst, dict):
            continue
        if is_three_mf_download_prohibited(inst):
            continue
        if not inst.get("downloadUrl") or str(inst.get("downloadState") or "").strip():
            missing.append(inst)
            continue
        if require_local_file and not _instance_3mf_file_exists(work_dir, inst):
            inst["downloadState"] = "missing"
            inst["downloadMessage"] = "3MF 文件尚未保存到本地，等待重新下载。"
            missing.append(inst)
    return missing


def emit_progress(progress_callback, percent: int, message: str, extra: Optional[dict] = None):
    if not callable(progress_callback):
        return
    payload = {
        "percent": max(min(int(percent or 0), 100), 0),
        "message": str(message or "").strip(),
    }
    if isinstance(extra, dict) and extra:
        payload.update(extra)
    try:
        progress_callback(payload)
    except Exception:
        pass


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()


def pick_ext_from_url(url: str, fallback: str = "jpg") -> str:
    clean = url.split("#")[0].split("?")[0]
    m = re.search(r"\.([A-Za-z0-9]+)$", clean)
    return m.group(1) if m else fallback


def parse_cookies(cookie_str: str) -> Dict[str, str]:
    return parse_cookie_values(cookie_str)


def summarize_cookie_header(raw_cookie: str, parsed_cookies: Optional[Dict[str, str]] = None) -> str:
    cookie_header = sanitize_cookie_header(raw_cookie)
    cookies = parsed_cookies if parsed_cookies is not None else parse_cookie_values(cookie_header)
    keys = list(cookies.keys())
    if not keys:
        return f"keys=none; has_cf_clearance=False; length={len(cookie_header)}"
    visible_keys = keys[:12]
    keys_text = ",".join(visible_keys)
    if len(keys) > len(visible_keys):
        keys_text = f"{keys_text},...(+{len(keys) - len(visible_keys)})"
    return (
        f"keys={keys_text}; "
        f"has_cf_clearance={'cf_clearance' in cookies}; "
        f"length={len(cookie_header)}"
    )


def _extract_auth_token(raw_cookie: str) -> str:
    return extract_auth_token(raw_cookie or "")


def _missing_3mf_failure_for_skipped_fetch(
    *,
    skip_state: str = "",
    skip_message: str = "",
    existing_state: Any = "",
    existing_message: Any = "",
    fetch_url: str = "",
) -> dict[str, str]:
    normalized_skip_state = str(skip_state or "").strip() or "pending_download"
    if normalized_skip_state == "download_limited" and (existing_state or existing_message):
        return {
            "state": str(existing_state or "missing"),
            "message": str(existing_message or "未获取到 3MF 下载地址。"),
        }

    return {
        "state": normalized_skip_state,
        "message": str(skip_message or "").strip() or describe_three_mf_failure(normalized_skip_state, url=fetch_url),
    }


IMAGE_TRANSFER_TIMEOUT_SECONDS = 45
BINARY_TRANSFER_TIMEOUT_SECONDS = 300
CONNECT_TIMEOUT_SECONDS = 15
READ_TIMEOUT_SECONDS = 30
THREE_MF_DOWNLOAD_WAIT_MIN_SECONDS = 5.0
THREE_MF_DOWNLOAD_WAIT_MAX_SECONDS = 10.0
FAKE_THREE_MF_DOWNLOAD_ENV = "MAKERHUB_FAKE_THREE_MF_DOWNLOADS"
FAKE_THREE_MF_DOWNLOAD_URL_PREFIX = "makerhub://fake-3mf/"
FAKE_THREE_MF_DOWNLOAD_MESSAGE = "本地 Docker 已启用 3MF 假下载，不会请求 MakerWorld 实际文件。"
COMMENT_ASSET_DOWNLOAD_WORKERS = 4
IMAGE_ASSET_DOWNLOAD_WORKERS = 4
SHARED_AVATAR_REL_DIR = "_shared/avatars"
VERBOSE_THREE_MF_FETCH_LOG = os.getenv("MAKERHUB_VERBOSE_THREE_MF_FETCH_LOG", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
TERMINAL_THREE_MF_FETCH_STATES = {
    "download_limited",
    "verification_required",
    "cloudflare",
    "auth_required",
}
MAKERWORLD_API_BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "X-BBL-Client-Type": "web",
    "X-BBL-Client-Version": "00.00.00.01",
    "X-BBL-App-Source": "makerworld",
    "X-BBL-Client-Name": "MakerWorld",
}


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name) or default)
    except (TypeError, ValueError):
        value = default
    return max(min(value, maximum), minimum)


def _three_mf_download_wait_range() -> tuple[float, float]:
    min_wait = _env_float(
        "MAKERHUB_THREE_MF_DOWNLOAD_WAIT_MIN_SECONDS",
        THREE_MF_DOWNLOAD_WAIT_MIN_SECONDS,
        0.0,
        120.0,
    )
    max_wait = _env_float(
        "MAKERHUB_THREE_MF_DOWNLOAD_WAIT_MAX_SECONDS",
        THREE_MF_DOWNLOAD_WAIT_MAX_SECONDS,
        0.0,
        120.0,
    )
    if max_wait <= 0:
        return 0.0, 0.0
    return min(min_wait, max_wait), max_wait


def _three_mf_download_wait_seconds() -> float:
    if fake_three_mf_downloads_enabled():
        return 0.0
    min_wait, max_wait = _three_mf_download_wait_range()
    if max_wait <= 0:
        return 0.0
    if max_wait <= min_wait:
        return max_wait
    return random.uniform(min_wait, max_wait)


def _wait_before_three_mf_download(reason: str = "", logger=None) -> float:
    wait_seconds = _three_mf_download_wait_seconds()
    if wait_seconds <= 0:
        return 0.0
    label = f"（{reason}）" if reason else ""
    log(logger, f"[3MF] 随机等待 {wait_seconds:.1f}s 后继续{label}")
    time.sleep(wait_seconds)
    return wait_seconds


def _should_pause_three_mf_fetch(failure_info: Any) -> bool:
    if not isinstance(failure_info, dict):
        return False
    return str(failure_info.get("state") or "").strip().lower() in {
        "verification_required",
        "cloudflare",
        "auth_required",
        "cookie_invalid",
        "download_limited",
        "http_error",
    }


def fake_three_mf_downloads_enabled() -> bool:
    return str(os.getenv(FAKE_THREE_MF_DOWNLOAD_ENV, "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "enabled",
    }


def _is_three_mf_fake_download_target(url: str, dest: Path) -> bool:
    if str(url or "").startswith(FAKE_THREE_MF_DOWNLOAD_URL_PREFIX):
        return True
    return dest.suffix.lower() == ".3mf"


def _fake_three_mf_download_url(inst_id: Any) -> str:
    safe_id = sanitize_filename(str(inst_id or "model")) or "model"
    return f"{FAKE_THREE_MF_DOWNLOAD_URL_PREFIX}{safe_id}.3mf"


def _write_fake_three_mf_file(dest: Path, source_url: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp_dest = dest.with_name(f"{dest.name}.{os.getpid()}.{threading.get_ident()}.part")
    payload = {
        "fake": True,
        "generator": "MakerHub",
        "source_url": str(source_url or ""),
        "message": FAKE_THREE_MF_DOWNLOAD_MESSAGE,
        "generated_at": china_now_iso(),
    }
    model_xml = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="zh-CN" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <metadata name="Title">MakerHub fake 3MF placeholder</metadata>
  <metadata name="MakerHubFakeDownload">true</metadata>
  <resources/>
  <build/>
</model>
"""
    rels_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""
    content_types_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
  <Default Extension="json" ContentType="application/json"/>
</Types>
"""
    try:
        with zipfile.ZipFile(temp_dest, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", content_types_xml)
            archive.writestr("_rels/.rels", rels_xml)
            archive.writestr("3D/3dmodel.model", model_xml)
            archive.writestr(
                "Metadata/makerhub_fake_download.json",
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
        temp_dest.replace(dest)
    except Exception:
        try:
            if temp_dest.exists():
                temp_dest.unlink()
        except Exception:
            pass
        raise


_OFFLINE_TEMPLATE_CACHE_LOCK = threading.RLock()
_OFFLINE_TEMPLATE_CACHE: dict[str, Any] = {
    "signature": (),
    "bundle": None,
    "unavailable_signature": None,
}
_OFFLINE_TEMPLATE_VARS_TOKEN = "<!--OFFLINE_INLINE_VARIABLES-->"
_OFFLINE_TEMPLATE_MODEL_TOKEN = "<!--OFFLINE_INLINE_MODEL-->"
_OFFLINE_TEMPLATE_SCRIPT_TOKEN = "<!--OFFLINE_INLINE_SCRIPT-->"
_OFFLINE_MODEL_CSS_IMPORT_RE = re.compile(
    r"@import\s+url\(['\"]?/static/css/(?:variables|components)\.css[^)]*\)\s*;?",
    flags=re.I,
)
_OFFLINE_ICON_LINK_RE = re.compile(
    r'<link[^>]*rel=["\']icon["\'][^>]*>\s*',
    flags=re.I,
)
_OFFLINE_FONT_AWESOME_RE = re.compile(
    r'<link[^>]*href=["\']https?://[^"\']*font-awesome[^"\']*["\'][^>]*>\s*',
    flags=re.I,
)
_OFFLINE_VARIABLES_LINK_RE = re.compile(
    r'<link[^>]*href=["\']/static/css/variables\.css[^"\']*["\'][^>]*>',
    flags=re.I,
)
_OFFLINE_MODEL_LINK_RE = re.compile(
    r'<link[^>]*href=["\']/static/css/model\.css[^"\']*["\'][^>]*>',
    flags=re.I,
)
_OFFLINE_MODEL_SCRIPT_RE = re.compile(
    r'<script[^>]*src=["\']/static/js/model\.js[^"\']*["\'][^>]*>\s*</script>',
    flags=re.I,
)
_OFFLINE_HEAD_CLOSE_RE = re.compile(r"</head>", flags=re.I)
_OFFLINE_BODY_CLOSE_RE = re.compile(r"</body>", flags=re.I)


def _stage_percent(start: int, end: int, current: int, total: int) -> int:
    if total <= 0:
        return end
    bounded_current = min(max(current, 0), total)
    return start + int((bounded_current / total) * max(end - start, 0))


def _emit_stage_progress(
    progress_callback,
    start: int,
    end: int,
    current: int,
    total: int,
    message: str,
):
    if total <= 0:
        emit_progress(progress_callback, end, message)
        return
    emit_progress(
        progress_callback,
        _stage_percent(start, end, current, total),
        f"{message}（{current}/{total}）",
        {"current": current, "total": total},
    )


def pick_instance_filename(inst: dict, name_hint: str = "") -> str:
    base = sanitize_filename(
        inst.get("fileName")
        or inst.get("name")
        or inst.get("sourceFileName")
        or inst.get("localName")
        or inst.get("title")
        or str(inst.get("id") or "model")
    ).strip()
    if not base:
        base = str(inst.get("id") or "model")
    # base 可能已经包含 .3mf，避免拼成 xxx.3mf.3mf
    if base.lower().endswith(".3mf"):
        return base
    return f"{base}.3mf"


def choose_unique_instance_filename(
    inst: dict,
    all_instances: List[dict],
    instances_dir: Path,
    name_hint: str = "",
    reserved_names: Optional[set[str]] = None,
    existing_files: Optional[set[str]] = None,
) -> str:
    """
    为实例选择“不会与其它实例冲突”的 3MF 文件名。

    规则：
    1) 优先使用当前实例已有 fileName（若安全）
    2) 否则使用 pick_instance_filename 结果
    3) 若冲突（其它实例已占用或磁盘已存在）则自动追加 _{id} / _{n}
    """
    explicit_raw = str(inst.get("fileName") or "").strip()
    explicit_name = ""
    if explicit_raw:
        explicit_name = sanitize_filename(Path(explicit_raw).name)
        if explicit_name and not explicit_name.lower().endswith(".3mf"):
            explicit_name += ".3mf"

    preferred = explicit_name or pick_instance_filename(inst, name_hint)
    if not preferred:
        preferred = f"{inst.get('id') or 'model'}.3mf"

    used_by_others = reserved_names
    if used_by_others is None:
        used_by_others = set()
        for other in all_instances or []:
            if other is inst or not isinstance(other, dict):
                continue
            raw = str(other.get("fileName") or "").strip()
            if not raw:
                continue
            nm = sanitize_filename(Path(raw).name)
            if nm and not nm.lower().endswith(".3mf"):
                nm += ".3mf"
            if nm:
                used_by_others.add(nm)

    existing_names = existing_files
    if existing_names is None:
        try:
            existing_names = {
                path.name
                for path in instances_dir.iterdir()
                if path.is_file()
            }
        except OSError:
            existing_names = set()

    def _can_use(name: str) -> bool:
        if not name:
            return False
        if name in used_by_others:
            return False
        # 当前实例若已有 fileName，且文件存在，可复用该文件名
        if explicit_name and name == explicit_name:
            return True
        # 否则磁盘存在同名文件时视为冲突，避免覆盖/误复用
        if name in existing_names:
            return False
        return True

    if _can_use(preferred):
        return preferred

    stem = Path(preferred).stem or str(inst.get("id") or "model")
    ext = Path(preferred).suffix or ".3mf"

    inst_id = str(inst.get("id") or "").strip()
    if inst_id:
        candidate = sanitize_filename(f"{stem}_{inst_id}{ext}")
        if _can_use(candidate):
            return candidate

    idx = 1
    while True:
        candidate = sanitize_filename(f"{stem}_{idx}{ext}")
        if _can_use(candidate):
            return candidate
        idx += 1


def fetch_html_with_browser(session: requests.Session, url: str, raw_cookie: str) -> Optional[str]:
    cookie_header = sanitize_cookie_header(raw_cookie) or _session_cookie_header(session)
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "User-Agent": session.headers.get("User-Agent", "Mozilla/5.0 (MW-Fetcher)"),
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    try:
        return makerworld_browser_get_text(
            url,
            raw_cookie=cookie_header,
            headers=headers,
            session=session,
        )
    except MakerWorldBrowserError as exc:
        log("CloakBrowser 获取页面失败:", exc)
        raise


def _session_cookie_header(session: requests.Session) -> str:
    try:
        return "; ".join(
            f"{cookie.name}={cookie.value}"
            for cookie in session.cookies
            if getattr(cookie, "name", "") and getattr(cookie, "value", "") is not None
        )
    except Exception:
        return ""


def _makerworld_not_found_message() -> str:
    return "模型页面返回 404，可能已下架、设为私有或转为草稿。"


def fetch_design_from_api(
    session: requests.Session,
    raw_cookie: str,
    url: str,
    api_host_hint: Optional[str] = None,
    logger=None,
) -> Optional[dict]:
    design_id = _parse_design_id(url)
    if not design_id:
        return None
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else "https://makerworld.com.cn"
    source = normalize_makerworld_source(url=url)
    bases = []
    if source == "global":
        _append_api_base_candidate(bases, "https://api.bambulab.com", source)
    elif source == "cn":
        _append_api_base_candidate(bases, "https://api.bambulab.cn", source)
    else:
        _append_api_base_candidate(bases, "https://api.bambulab.cn", source)
        _append_api_base_candidate(bases, "https://api.bambulab.com", source)
    _append_api_base_candidate(bases, api_host_hint or "", source)
    _append_api_base_candidate(bases, origin, source)

    endpoints = []
    for base in bases:
        host = urlparse(base).netloc.lower()
        if host.startswith("api.bambulab."):
            path_templates = [
                "/v1/design-service/design/{id}",
                "/v1/design-service/design/{id}/detail",
                "/v1/design-service/design/{id}/detail?source=web",
                "/v1/design-service/design/{id}?lang=zh",
            ]
        else:
            path_templates = [
                "/api/v1/design-service/design/{id}",
                "/api/v1/design-service/design/{id}/detail",
                "/api/v1/design-service/design/{id}/detail?source=web",
                "/api/v1/design-service/design/{id}?lang=zh",
                "/v1/design-service/design/{id}",
                "/v1/design-service/design/{id}/detail",
            ]
        for path in path_templates:
            candidate = f"{base.rstrip('/')}{path.format(id=design_id)}"
            if candidate not in endpoints:
                endpoints.append(candidate)
    cookie_header = sanitize_cookie_header(raw_cookie) or _session_cookie_header(session)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": url,
        "User-Agent": session.headers.get("User-Agent", "Mozilla/5.0 (MW-Fetcher)"),
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    for api_url in endpoints:
        try:
            payload = makerworld_browser_get_json(
                api_url,
                raw_cookie=cookie_header,
                headers=headers,
                session=session,
                allow_non_json=True,
            )
        except MakerWorldBrowserError as exc:
            log(logger, "CloakBrowser API 请求失败，尝试下一个候选:", api_url, exc)
            continue
        if isinstance(payload, dict):
            design = _unwrap_design_payload(payload)
            if design:
                payload_error = _design_payload_error(design, url)
                if payload_error:
                    log(logger, "CloakBrowser API 返回的模型数据无效，跳过:", api_url, payload_error)
                else:
                    _normalize_design_payload_identity(design, url)
                    return design
    return None




def _build_existing_comment_lookup(items: object) -> dict[str, dict]:
    lookup: dict[str, dict] = {}
    for item in _comment_tree_items(items):
        comment_id = str(item.get("id") or "").strip()
        if comment_id and comment_id not in lookup:
            lookup[comment_id] = item
    return lookup


def _archive_root_from_comment_out_dir(out_dir: Path) -> Path:
    model_root = out_dir.parent if out_dir.name == "images" else out_dir
    return model_root.parent


def _shared_avatar_dir(out_dir: Path) -> Path:
    return _archive_root_from_comment_out_dir(out_dir) / SHARED_AVATAR_REL_DIR


def _avatar_cache_key(avatar_url: str) -> str:
    normalized = _normalize_url_value(avatar_url) or str(avatar_url or "").strip()
    return hashlib.sha1(normalized.encode("utf-8", errors="ignore")).hexdigest()


def _avatar_cache_filename(avatar_url: str, fallback_path: Optional[Path] = None) -> str:
    ext = pick_ext_from_url(avatar_url, "")
    if not ext and fallback_path is not None:
        ext = fallback_path.suffix.lower().lstrip(".")
    if not ext:
        ext = "jpg"
    return f"{_avatar_cache_key(avatar_url)}.{ext}"


def _shared_avatar_rel_path(filename: str) -> str:
    return f"{SHARED_AVATAR_REL_DIR}/{filename}"


def _comment_model_root(out_dir: Path) -> Path:
    return out_dir.parent if out_dir.name == "images" else out_dir


def _comment_local_asset_path(out_dir: Path, rel_path: str = "", local_name: str = "") -> Optional[Path]:
    model_root = _comment_model_root(out_dir)
    archive_root = _archive_root_from_comment_out_dir(out_dir)
    candidates: list[Path] = []
    clean_rel = str(rel_path or "").strip().lstrip("/")
    clean_local = str(local_name or "").strip().lstrip("/")
    if clean_rel:
        if clean_rel.startswith(f"{SHARED_AVATAR_REL_DIR}/"):
            candidates.append(archive_root / clean_rel)
        else:
            candidates.append(model_root / clean_rel)
    if clean_local:
        candidates.append(out_dir / Path(clean_local).name)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _avatar_rel_path_exists(out_dir: Path, rel_path: str = "") -> bool:
    return _comment_local_asset_path(out_dir, rel_path=rel_path) is not None


def _copy_existing_avatar_to_shared(out_dir: Path, avatar_url: str, existing_author: dict) -> Optional[str]:
    if not isinstance(existing_author, dict):
        return None
    existing_rel = str(existing_author.get("avatarRelPath") or "").strip()
    existing_local = str(existing_author.get("avatarLocal") or "").strip()
    if existing_rel.startswith(f"{SHARED_AVATAR_REL_DIR}/") and _avatar_rel_path_exists(out_dir, existing_rel):
        return existing_rel
    source = _comment_local_asset_path(out_dir, rel_path=existing_rel, local_name=existing_local)
    if source is None:
        return None
    filename = _avatar_cache_filename(avatar_url, source)
    target = _shared_avatar_dir(out_dir) / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        temp_target = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.part")
        try:
            shutil.copy2(source, temp_target)
            temp_target.replace(target)
        except Exception:
            try:
                if temp_target.exists():
                    temp_target.unlink()
            except Exception:
                pass
            return None
    return _shared_avatar_rel_path(filename)


def _apply_author_avatar_ref(author: dict, rel_path: str) -> None:
    filename = Path(rel_path).name
    author["avatarLocal"] = filename
    author["avatarRelPath"] = rel_path


def _apply_comment_image_ref(image: dict, local_name: str, rel_path: str) -> None:
    image["localName"] = local_name
    image["relPath"] = rel_path


def _comment_resource_stats(comments: list[dict]) -> dict[str, int]:
    items = list(_comment_tree_items(comments))
    avatar_urls: list[str] = []
    image_urls: list[str] = []
    shared_avatar_refs = 0
    local_image_refs = 0
    for item in items:
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        avatar_url = str(author.get("avatarUrl") or "").strip()
        if avatar_url:
            avatar_urls.append(_normalize_url_value(avatar_url) or avatar_url)
        if str(author.get("avatarRelPath") or "").strip().startswith(f"{SHARED_AVATAR_REL_DIR}/"):
            shared_avatar_refs += 1
        images = item.get("images") if isinstance(item.get("images"), list) else []
        for image in images:
            if not isinstance(image, dict):
                continue
            image_url = str(image.get("url") or "").strip()
            if image_url:
                image_urls.append(_normalize_url_value(image_url) or image_url)
            if str(image.get("relPath") or image.get("localName") or "").strip():
                local_image_refs += 1
    return {
        "comment_roots": len(comments or []),
        "comment_total": len(items),
        "reply_total": max(len(items) - len(comments or []), 0),
        "avatar_urls": len(avatar_urls),
        "unique_avatar_urls": len(set(avatar_urls)),
        "comment_images": len(image_urls),
        "unique_comment_image_urls": len(set(image_urls)),
        "shared_avatar_refs": shared_avatar_refs,
        "local_comment_image_refs": local_image_refs,
    }


def _download_asset_with_fresh_session(base_session: requests.Session, url: str, dest: Path) -> None:
    download_with_fresh_session(base_session, url, dest, download_func=download_file)


def _log_asset_download_failure(message: str, url: str, error: Exception, logger=None) -> None:
    log(logger, message, safe_asset_url(url), error)


def _download_comment_assets(tasks: list[dict], progress_callback, progress_start: int, progress_end: int) -> dict[str, int]:
    def on_progress(completed: int, total: int) -> None:
        if completed == 1 or completed == total or completed % 5 == 0:
            _emit_stage_progress(
                progress_callback,
                progress_start,
                progress_end,
                completed,
                total,
                "正在下载评论资源",
            )

    return run_asset_tasks(
        tasks,
        max_workers=COMMENT_ASSET_DOWNLOAD_WORKERS,
        on_progress=on_progress,
        on_error=lambda task, exc: _log_asset_download_failure(
            task.get("error_message") or "评论资源下载失败，保留原始链接：",
            task.get("url") or "",
            exc,
        ),
    )


def _download_image_assets(
    tasks: list[dict],
    progress_callback,
    progress_start: int,
    progress_end: int,
    message: str,
) -> dict[str, int]:
    return run_asset_tasks(
        tasks,
        max_workers=IMAGE_ASSET_DOWNLOAD_WORKERS,
        on_progress=lambda completed, total: _emit_stage_progress(
            progress_callback,
            progress_start,
            progress_end,
            completed,
            total,
            message,
        ),
        on_error=lambda task, exc: _log_asset_download_failure(
            task.get("error_message") or "图片下载失败，保留原始链接：",
            task.get("url") or "",
            exc,
        ),
    )


def _apply_existing_comment_assets(
    comments: list[dict],
    existing_comment_lookup: dict[str, dict],
    out_dir: Path,
    *,
    migrate_avatars: bool = True,
) -> dict[str, int]:
    stats = {
        "avatar_existing_reused": 0,
        "avatar_shared_reused": 0,
        "avatar_shared_migrated": 0,
        "comment_image_existing_reused": 0,
    }
    for item in _comment_tree_items(comments):
        existing = existing_comment_lookup.get(str(item.get("id") or "").strip()) or {}
        existing_author = existing.get("author") if isinstance(existing.get("author"), dict) else {}
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        avatar_url = str(author.get("avatarUrl") or existing_author.get("avatarUrl") or "").strip()
        existing_avatar_rel = str(existing_author.get("avatarRelPath") or "").strip()
        existing_avatar_matches = _media_item_remote_matches(existing_author, avatar_url, url_fields=("avatarUrl", "url"))
        migrated_rel = (
            _copy_existing_avatar_to_shared(out_dir, avatar_url, existing_author)
            if migrate_avatars and avatar_url and existing_avatar_matches
            else None
        )
        if migrated_rel:
            _apply_author_avatar_ref(author, migrated_rel)
            if existing_avatar_rel.startswith(f"{SHARED_AVATAR_REL_DIR}/"):
                stats["avatar_shared_reused"] += 1
            else:
                stats["avatar_shared_migrated"] += 1
        else:
            if existing_avatar_matches and str(existing_author.get("avatarLocal") or "").strip():
                author["avatarLocal"] = str(existing_author.get("avatarLocal") or "").strip()
                stats["avatar_existing_reused"] += 1
            if existing_avatar_matches and str(existing_author.get("avatarRelPath") or "").strip():
                author["avatarRelPath"] = str(existing_author.get("avatarRelPath") or "").strip()

        existing_images = existing.get("images") if isinstance(existing.get("images"), list) else []
        existing_image_lookup = _build_existing_media_lookup(existing_images, url_fields=("url", "originalUrl"))
        images = item.get("images") if isinstance(item.get("images"), list) else []
        for img_idx, image in enumerate(images, start=1):
            if not isinstance(image, dict):
                continue
            existing_image = _match_existing_media_item_from_lookup(
                existing_image_lookup,
                url=str(image.get("url") or ""),
                index=img_idx,
            )
            existing_image_matches = _media_item_remote_matches(
                existing_image,
                str(image.get("url") or ""),
                url_fields=("url", "originalUrl"),
            )
            if existing_image_matches and str(existing_image.get("localName") or "").strip():
                image["localName"] = str(existing_image.get("localName") or "").strip()
                stats["comment_image_existing_reused"] += 1
            if existing_image_matches and str(existing_image.get("relPath") or "").strip():
                image["relPath"] = str(existing_image.get("relPath") or "").strip()
    return stats




def _normalize_service_base(base: Optional[str]) -> str:
    normalized = str(base or "").strip()
    if not normalized:
        return ""
    if not normalized.startswith("http://") and not normalized.startswith("https://"):
        normalized = f"https://{normalized}"
    return normalized.rstrip("/")


def _comment_api_base_candidates(source_url: str, api_host_hint: Optional[str] = None) -> List[str]:
    normalized_source = normalize_makerworld_source(url=source_url)
    if normalized_source == "global":
        preferred_site = "https://makerworld.com"
        preferred_api = "https://api.bambulab.com"
    else:
        preferred_site = "https://makerworld.com.cn"
        preferred_api = "https://api.bambulab.cn"

    parsed = urlparse(str(source_url or "").strip())
    origin = (
        f"{parsed.scheme}://{parsed.netloc}"
        if parsed.scheme and parsed.netloc
        else preferred_site
    )

    bases: List[str] = []
    for candidate in (preferred_api, api_host_hint, origin, preferred_site):
        normalized = _normalize_service_base(candidate)
        if normalized and normalized not in bases:
            bases.append(normalized)
    return bases


def _comment_service_endpoint_candidates(
    source_url: str,
    path: str,
    *,
    api_host_hint: Optional[str] = None,
) -> List[str]:
    clean_path = "/" + str(path or "").lstrip("/")
    endpoints: List[str] = []
    for base in _comment_api_base_candidates(source_url, api_host_hint=api_host_hint):
        host = urlparse(base).netloc.lower()
        service_prefixes = (
            ("/v1/comment-service",)
            if host.startswith("api.bambulab.")
            else ("/api/v1/comment-service", "/v1/comment-service")
        )
        for service_prefix in service_prefixes:
            candidate = f"{base}{service_prefix}{clean_path}"
            if candidate not in endpoints:
                endpoints.append(candidate)
    return endpoints


def _build_makerworld_api_headers(session: requests.Session, referer: str) -> dict[str, str]:
    headers = dict(MAKERWORLD_API_BROWSER_HEADERS)
    effective_referer = str(referer or "").strip() or "https://makerworld.com.cn/"
    parsed = urlparse(effective_referer)
    headers["Referer"] = effective_referer
    headers["Origin"] = (
        f"{parsed.scheme}://{parsed.netloc}"
        if parsed.scheme and parsed.netloc
        else "https://makerworld.com.cn"
    )
    headers["User-Agent"] = session.headers.get("User-Agent", "Mozilla/5.0 (MW-Fetcher)")
    return headers


def _looks_like_html_response(text: object) -> bool:
    if not isinstance(text, str):
        return False
    lowered = text.lstrip().lower()
    return lowered.startswith("<!doctype html") or lowered.startswith("<html")




def _fetch_comment_reply_payload(
    session: requests.Session,
    source_url: str,
    root_comment_id: str,
    *,
    params: Optional[dict[str, object]] = None,
    api_host_hint: Optional[str] = None,
    reply_kind: str = "comment",
) -> Optional[object]:
    headers = _build_makerworld_api_headers(session, source_url)
    reply_path = (
        f"/rating/{root_comment_id}/reply"
        if str(reply_kind or "").strip().lower() == "rating"
        else f"/comment/{root_comment_id}/reply"
    )
    fallback_payload: Optional[object] = None
    for api_url in _comment_service_endpoint_candidates(
        source_url,
        reply_path,
        api_host_hint=api_host_hint,
    ):
        try:
            payload = makerworld_browser_get_json(
                api_url,
                raw_cookie=_session_cookie_header(session),
                headers=headers,
                params=params or None,
                session=session,
                allow_non_json=True,
            )
        except MakerWorldBrowserError:
            continue
        if payload is not None:
            if _extract_comment_replies_from_payload(payload, root_comment_id):
                return payload
            if fallback_payload is None:
                fallback_payload = payload
    return fallback_payload


_COMMENT_LIST_PAGE_LIMIT = 100
_COMMENT_LIST_MAX_PAGES = 250


def _fetch_comment_list_payload(
    session: requests.Session,
    source_url: str,
    design_id: str,
    *,
    offset: int,
    limit: int,
    api_host_hint: Optional[str] = None,
) -> Optional[object]:
    if not design_id:
        return None
    headers = _build_makerworld_api_headers(session, source_url)
    params: dict[str, object] = {
        "designId": design_id,
        "offset": max(int(offset or 0), 0),
        "limit": max(min(int(limit or _COMMENT_LIST_PAGE_LIMIT), _COMMENT_LIST_PAGE_LIMIT), 1),
        "type": 0,
        "sort": 0,
    }
    fallback_payload: Optional[object] = None
    for api_url in _comment_service_endpoint_candidates(
        source_url,
        "/commentandrating",
        api_host_hint=api_host_hint,
    ):
        try:
            payload = makerworld_browser_get_json(
                api_url,
                raw_cookie=_session_cookie_header(session),
                headers=headers,
                params=params,
                session=session,
                allow_non_json=True,
            )
        except MakerWorldBrowserError:
            continue
        if payload is not None:
            if _extract_comment_list_items(payload):
                return payload
            if fallback_payload is None or (
                _comment_list_payload_total(payload) > _comment_list_payload_total(fallback_payload)
                or _comment_list_payload_hit_count(payload) > _comment_list_payload_hit_count(fallback_payload)
            ):
                fallback_payload = payload
    return fallback_payload




def _fetch_paginated_comment_list(
    comments: List[dict],
    session: requests.Session,
    design: dict,
    source_url: str,
    *,
    api_host_hint: Optional[str] = None,
    logger=None,
) -> tuple[List[dict], dict[str, int]]:
    design_id = str(design.get("id") or _parse_design_id(source_url) or "").strip()
    if not design_id or not source_url:
        return comments, {"pages": 0, "roots": 0, "total": 0}

    fetched_comments: List[dict] = []
    offset = 0
    total = 0
    total_known = False
    pages = 0
    seen_offsets: set[int] = set()
    list_started_at = time.perf_counter()

    for _ in range(_COMMENT_LIST_MAX_PAGES):
        if offset in seen_offsets:
            break
        seen_offsets.add(offset)
        payload = _fetch_comment_list_payload(
            session,
            source_url,
            design_id,
            offset=offset,
            limit=_COMMENT_LIST_PAGE_LIMIT,
            api_host_hint=api_host_hint,
        )
        if payload is None:
            break

        hit_count = _comment_list_payload_hit_count(payload)
        payload_total = _comment_list_payload_total(payload)
        if payload_total > 0 or _comment_list_payload_declares_empty(payload):
            total_known = True
            total = max(total, payload_total)
        if hit_count <= 0 and total <= 0:
            break
        page_items = _extract_comment_list_items(payload)
        if not page_items and hit_count <= 0:
            break

        if page_items:
            fetched_comments = _merge_threaded_comment_list(fetched_comments, page_items)
        pages += 1

        step = max(hit_count, len(page_items), 1)
        offset += step
        if total > 0 and offset >= total:
            break
        if total <= 0 and hit_count < _COMMENT_LIST_PAGE_LIMIT:
            break

    if fetched_comments:
        comments = _merge_threaded_comment_list(comments, fetched_comments)

    _log_perf(
        "comments.fetch_pages",
        list_started_at,
        logger=logger,
        pages=pages,
        roots=len(fetched_comments),
        total=total,
        total_known=total_known,
    )
    return comments, {"pages": pages, "roots": len(fetched_comments), "total": total, "total_known": total_known}


def _hydrate_missing_comment_replies(
    comments: List[dict],
    session: requests.Session,
    source_url: str,
    *,
    api_host_hint: Optional[str] = None,
    logger=None,
) -> tuple[List[dict], dict[str, int]]:
    if not comments or not source_url:
        return comments, {"roots": 0, "replies": 0}

    hydrated: List[dict] = []
    hydrated_root_count = 0
    hydrated_reply_count = 0
    fetch_started_at = time.perf_counter()

    for root in comments:
        if not isinstance(root, dict):
            continue
        normalized_root = dict(root)
        root_comment_id = str(normalized_root.get("id") or normalized_root.get("commentId") or "").strip()
        existing_replies = _comment_reply_items(normalized_root)
        expected_reply_count = _comment_reply_count(normalized_root)
        merged_replies = _merge_threaded_comment_list([], existing_replies)
        root_comment_source = str(normalized_root.get("commentSource") or "").strip().lower()
        root_rating_id = str(normalized_root.get("ratingId") or "").strip()
        is_rating_root = root_comment_source == "rating" or (
            bool(root_rating_id)
            and root_rating_id == root_comment_id
            and _comment_numeric(normalized_root.get("rating")) > 0
        )
        cursor_param = "msgRatingReplyId" if is_rating_root else "msgCommentReplyId"

        if root_comment_id and expected_reply_count > len(merged_replies):
            page_limit = min(max(expected_reply_count, 20), 200)
            after: Optional[int] = None
            last_reply_id = ""
            seen_offsets: set[int] = set()

            for _ in range(5):
                params: dict[str, object] = {"limit": page_limit}
                if after is not None:
                    params["after"] = after
                if last_reply_id:
                    params[cursor_param] = last_reply_id

                payload = _fetch_comment_reply_payload(
                    session,
                    source_url,
                    root_comment_id,
                    params=params,
                    api_host_hint=api_host_hint,
                    reply_kind="rating" if is_rating_root else "comment",
                )
                if payload is None:
                    break

                fetched_replies = _extract_comment_replies_from_payload(payload, root_comment_id)
                if not fetched_replies:
                    break

                before_count = len(merged_replies)
                merged_replies = _merge_threaded_comment_list(merged_replies, fetched_replies)
                if len(merged_replies) > before_count:
                    last_reply = merged_replies[-1] if merged_replies else {}
                    last_reply_id = str(last_reply.get("id") or last_reply_id).strip()

                if len(merged_replies) >= expected_reply_count:
                    break

                has_more = _extract_comment_reply_payload_has_more(payload)
                if has_more is False:
                    break
                if has_more is None and len(fetched_replies) < page_limit:
                    break

                next_after = len(merged_replies)
                if next_after in seen_offsets:
                    break
                seen_offsets.add(next_after)
                after = next_after

        if len(merged_replies) > len(existing_replies):
            hydrated_root_count += 1
            hydrated_reply_count += max(len(merged_replies) - len(existing_replies), 0)
        if merged_replies:
            normalized_root["replies"] = merged_replies
        elif "replies" in normalized_root:
            normalized_root["replies"] = []
        normalized_root["replyCount"] = max(len(merged_replies), expected_reply_count)
        hydrated.append(normalized_root)

    _log_perf(
        "comments.fetch_replies",
        fetch_started_at,
        logger=logger,
        roots=hydrated_root_count,
        replies=hydrated_reply_count,
    )
    return hydrated, {"roots": hydrated_root_count, "replies": hydrated_reply_count}


def collect_comments(
    next_data: dict,
    design: dict,
    session: requests.Session,
    out_dir: Path,
    progress_callback=None,
    progress_start: int = 50,
    progress_end: int = 55,
    download_assets: bool = True,
    existing_comments: Optional[List[dict]] = None,
    api_host_hint: Optional[str] = None,
) -> dict:
    emit_progress(progress_callback, progress_start, "正在整理评论数据")
    total_started_at = time.perf_counter()
    comments: List[dict] = []
    seen: dict[str, dict] = {}

    section_lookup_started_at = time.perf_counter()
    candidate_sections = _extract_comment_sections(next_data)
    candidate_sections.extend(_extract_comment_sections(design))
    unique_sections: List[object] = []
    seen_sections: set[int] = set()
    for section in candidate_sections:
        marker = id(section)
        if marker in seen_sections:
            continue
        seen_sections.add(marker)
        unique_sections.append(section)
    _log_perf(
        "comments.find_sections",
        section_lookup_started_at,
        sections=len(unique_sections),
    )

    extract_started_at = time.perf_counter()
    search_mode = "candidate_sections" if unique_sections else "full_scan"
    if unique_sections:
        comments = _extract_comment_list_items(unique_sections)
    if not comments:
        search_mode = "full_scan"
        comments = _extract_comment_list_items([next_data, design])
    comments, page_fetch_stats = _fetch_paginated_comment_list(
        comments,
        session,
        design,
        str(design.get("url") or next_data.get("url") or ""),
        api_host_hint=api_host_hint,
    )
    existing_comment_items = existing_comments if isinstance(existing_comments, list) else []
    if existing_comment_items:
        comments = _merge_threaded_comment_list(existing_comment_items, comments)
    comments, reply_fetch_stats = _hydrate_missing_comment_replies(
        comments,
        session,
        str(design.get("url") or next_data.get("url") or ""),
        api_host_hint=api_host_hint,
    )
    comment_total = _count_comment_threads(comments)
    _log_perf(
        "comments.extract",
        extract_started_at,
        mode=search_mode,
        comments=comment_total,
        roots=len(comments),
        sections=len(unique_sections),
        fetched_pages=page_fetch_stats.get("pages") or 0,
        fetched_roots=page_fetch_stats.get("roots") or 0,
        fetched_total=page_fetch_stats.get("total") or 0,
        hydrated_roots=reply_fetch_stats.get("roots") or 0,
        hydrated_replies=reply_fetch_stats.get("replies") or 0,
    )

    comment_count = _resolved_comment_count(
        unique_sections=unique_sections,
        next_data=next_data,
        design=design,
        comment_total=comment_total,
        page_fetch_stats=page_fetch_stats,
    )

    existing_comment_lookup = _build_existing_comment_lookup(existing_comments)
    existing_asset_stats = _apply_existing_comment_assets(comments, existing_comment_lookup, out_dir)
    asset_stats = {
        **_comment_resource_stats(comments),
        **existing_asset_stats,
        "avatar_cache_hits": 0,
        "comment_image_cache_hits": 0,
        "avatar_download_tasks": 0,
        "comment_image_download_tasks": 0,
        "download_tasks": 0,
        "download_completed": 0,
        "download_failed": 0,
        "deduped_downloads": 0,
    }

    if not download_assets:
        emit_progress(progress_callback, progress_end, "评论整理完成")
        _log_perf(
            "comments.total",
            total_started_at,
            mode=search_mode,
            comments=comment_total,
            roots=len(comments),
            assets=0,
            download_assets=False,
        )
        return {
            "count": max(comment_count, comment_total),
            "items": comments,
            "assetStats": asset_stats,
        }

    avatar_tasks: dict[str, dict] = {}
    image_tasks: dict[str, dict] = {}
    for idx, item in enumerate(_comment_tree_items(comments), start=1):
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        avatar_url = str(author.get("avatarUrl") or "").strip()
        if avatar_url:
            current_rel = str(author.get("avatarRelPath") or "").strip()
            if current_rel.startswith(f"{SHARED_AVATAR_REL_DIR}/") and _avatar_rel_path_exists(out_dir, current_rel):
                continue
            avatar_name = _avatar_cache_filename(avatar_url)
            avatar_rel = _shared_avatar_rel_path(avatar_name)
            avatar_target = _shared_avatar_dir(out_dir) / avatar_name
            if avatar_target.exists():
                asset_stats["avatar_cache_hits"] += 1
                _apply_author_avatar_ref(author, avatar_rel)
            else:
                avatar_key = avatar_rel
                if avatar_key not in avatar_tasks:
                    avatar_tasks[avatar_key] = {
                        "url": avatar_url,
                        "apply": [],
                        "error_message": "评论头像下载失败，保留原始链接：",
                        "download": lambda s=session, u=avatar_url, d=avatar_target: _download_asset_with_fresh_session(s, u, d),
                    }
                avatar_tasks[avatar_key]["apply"].append(
                    lambda author=author, rel=avatar_rel: _apply_author_avatar_ref(author, rel)
                )
        images = item.get("images") if isinstance(item.get("images"), list) else []
        for img_idx, image in enumerate(images, start=1):
            if not isinstance(image, dict):
                continue
            url = str(image.get("url") or "").strip()
            if not url:
                continue
            existing_rel = str(image.get("relPath") or "").strip()
            existing_local = str(image.get("localName") or "").strip()
            if _comment_local_asset_path(out_dir, rel_path=existing_rel, local_name=existing_local) is not None:
                continue
            image_name = f"comment_{idx:02d}_img_{img_idx:02d}.{pick_ext_from_url(url)}"
            image_rel = f"images/{image_name}"
            image_target = out_dir / image_name
            normalized_image_url = _normalize_url_value(url) or url
            if image_target.exists() and (existing_rel or existing_local):
                asset_stats["comment_image_cache_hits"] += 1
                _apply_comment_image_ref(image, image_name, image_rel)
            elif normalized_image_url in image_tasks:
                existing_image_task = image_tasks[normalized_image_url]
                existing_image_task["apply"].append(
                    lambda image=image, name=existing_image_task["local_name"], rel=existing_image_task["rel_path"]: _apply_comment_image_ref(image, name, rel)
                )
            else:
                image_tasks[normalized_image_url] = {
                    "url": url,
                    "local_name": image_name,
                    "rel_path": image_rel,
                    "apply": [
                        lambda image=image, name=image_name, rel=image_rel: _apply_comment_image_ref(image, name, rel)
                    ],
                    "error_message": "评论图片下载失败，保留原始链接：",
                    "download": lambda s=session, u=url, d=image_target: _download_asset_with_fresh_session(s, u, d),
                }

    asset_tasks = list(avatar_tasks.values()) + list(image_tasks.values())
    download_stats = _download_comment_assets(asset_tasks, progress_callback, progress_start, progress_end)
    asset_stats["avatar_download_tasks"] = len(avatar_tasks)
    asset_stats["comment_image_download_tasks"] = len(image_tasks)
    asset_stats["download_tasks"] = len(asset_tasks)
    asset_stats["download_completed"] = int(download_stats.get("completed") or 0)
    asset_stats["download_failed"] = int(download_stats.get("failed") or 0)
    skipped_or_reused = (
        int(asset_stats.get("avatar_existing_reused") or 0)
        + int(asset_stats.get("avatar_shared_reused") or 0)
        + int(asset_stats.get("avatar_shared_migrated") or 0)
        + int(asset_stats.get("comment_image_existing_reused") or 0)
        + int(asset_stats.get("avatar_cache_hits") or 0)
        + int(asset_stats.get("comment_image_cache_hits") or 0)
    )
    requested_resources = int(asset_stats.get("avatar_urls") or 0) + int(asset_stats.get("comment_images") or 0)
    asset_stats["deduped_downloads"] = max(requested_resources - skipped_or_reused - len(asset_tasks), 0)

    emit_progress(progress_callback, progress_end, "评论整理完成")
    _log_perf(
        "comments.total",
        total_started_at,
        mode=search_mode,
        comments=comment_total,
        roots=len(comments),
        assets=len(asset_tasks),
    )

    return {
        "count": max(comment_count, comment_total),
        "items": comments,
        "assetStats": asset_stats,
    }


def parse_summary(
    design: dict,
    base_name: str,
    session: requests.Session,
    out_dir: Path,
    progress_callback=None,
    progress_start: int = 40,
    progress_end: int = 45,
    download_assets: bool = True,
    existing_meta: Optional[dict] = None,
):
    raw_html = (
        design.get("summary")
        or design.get("summaryHtml")
        or design.get("summary_html")
        or design.get("summaryContent")
        or design.get("description")
        or design.get("desc")
        or ""
    )
    if isinstance(raw_html, dict):
        raw_html = raw_html.get("html") or raw_html.get("raw") or raw_html.get("text") or ""
    raw_html = str(raw_html or "")
    stripped_html = raw_html.strip()
    if not stripped_html:
        return {
            "raw": raw_html,
            "html": "",
            "text": "",
            "summaryImages": [],
        }
    if "<" not in stripped_html and "&" not in stripped_html:
        return {
            "raw": raw_html,
            "html": stripped_html,
            "text": " ".join(stripped_html.split()),
            "summaryImages": [],
        }

    soup = BeautifulSoup(raw_html, "html.parser")
    summary_images = []
    image_nodes = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if src:
            image_nodes.append((img, src))

    existing_summary_images = []
    if isinstance(existing_meta, dict):
        existing_summary_images = existing_meta.get("summaryImages") if isinstance(existing_meta.get("summaryImages"), list) else []
    existing_summary_lookup = _build_existing_media_lookup(existing_summary_images, url_fields=("originalUrl", "url"))

    total_images = len(image_nodes)
    summary_download_tasks: list[dict[str, Any]] = []
    for idx, (img, src) in enumerate(image_nodes, start=1):
        _emit_stage_progress(
            progress_callback,
            progress_start,
            progress_end,
            idx,
            total_images,
            "正在下载摘要图片",
        )
        ext = pick_ext_from_url(src)
        name = f"summary_img_{idx:02d}.{ext}"
        existing_image = _match_existing_media_item_from_lookup(
            existing_summary_lookup,
            url=src,
            index=idx,
        )
        if not download_assets:
            if _media_item_remote_matches(existing_image, src, url_fields=("originalUrl", "url", "src")):
                rel_path = str(existing_image.get("relPath") or "").strip()
                file_name = str(existing_image.get("fileName") or "").strip()
            else:
                rel_path = ""
                file_name = ""
            if rel_path:
                img["src"] = f"./{rel_path}" if not rel_path.startswith(("./", "http://", "https://")) else rel_path
            summary_images.append(
                {
                    "index": idx,
                    "originalUrl": src,
                    "relPath": rel_path,
                    "fileName": file_name,
                }
            )
            continue
        if _media_item_remote_matches(existing_image, src, url_fields=("originalUrl", "url", "src")) and _media_item_local_exists(out_dir, existing_image):
            rel_path, file_name = _existing_media_ref(existing_image)
            if rel_path:
                img["src"] = f"./{rel_path}" if not rel_path.startswith(("./", "http://", "https://")) else rel_path
            summary_images.append(
                {
                    "index": idx,
                    "originalUrl": src,
                    "relPath": rel_path,
                    "fileName": file_name,
                }
            )
            continue
        try:
            target = out_dir / name
            summary_download_tasks.append(
                {
                    "url": src,
                    "error_message": "摘要图片下载失败，保留原始链接：",
                    "download": lambda s=session, u=src, d=target: _download_asset_with_fresh_session(s, u, d),
                    "apply": [
                        lambda img=img, src=src, name=name, idx=idx: (
                            img.__setitem__("src", f"./images/{name}"),
                            summary_images.append(
                                {
                                    "index": idx,
                                    "originalUrl": src,
                                    "relPath": f"images/{name}",
                                    "fileName": name,
                                }
                            ),
                        )
                    ],
                }
            )
        except Exception as exc:
            _log_asset_download_failure("摘要图片任务准备失败，保留原始链接：", src, exc)
            continue

    if summary_download_tasks:
        _download_image_assets(
            summary_download_tasks,
            progress_callback,
            progress_start,
            progress_end,
            "正在下载摘要图片",
        )

    if total_images:
        emit_progress(progress_callback, progress_end, "摘要图片整理完成")

    summary_images.sort(key=lambda item: int(item.get("index") or 0) if isinstance(item, dict) else 0)
    html_local = str(soup)
    text_plain = " ".join(unescape(soup.get_text()).split())

    return {
        "raw": raw_html,
        "html": html_local,
        "text": text_plain,
        "summaryImages": summary_images,
    }


def extract_author(design: dict, html_text: str = None):
    def _extract_author_handle(href_or_url: str) -> str:
        if not href_or_url:
            return ""
        raw = href_or_url.strip()
        if raw.startswith("@"):
            raw = f"/zh/{raw}"
        parsed = urlparse(raw)
        path = parsed.path or raw
        m = re.search(r"(?:^|/)@([A-Za-z0-9_.-]+)(?:[/?#]|$)", path)
        return m.group(1) if m else ""

    def _build_author_url(handle: str) -> str:
        handle = (handle or "").lstrip("@").strip()
        return f"https://makerworld.com.cn/zh/@{handle}" if handle else ""

    def _is_suspect_author_text(text: str) -> bool:
        normalized = " ".join(str(text or "").split()).strip().lower()
        return normalized in {
            "浏览历史",
            "browse history",
            "browsing history",
            "history",
            "收藏夹",
            "collections",
            "设置",
            "settings",
            "通知",
            "notifications",
        }

    def _is_suspect_author_href(href: str, text: str = "") -> bool:
        if _is_suspect_author_text(text):
            return True
        raw = str(href or "").strip()
        if not raw:
            return False
        parsed = urlparse(raw)
        path = (parsed.path or raw).lower()
        suspect_markers = (
            "/browsing-history",
            "/collections",
            "/collection",
            "/likes",
            "/search",
            "/settings",
            "/notifications",
            "/messages",
            "/following",
            "/followers",
        )
        return any(marker in path for marker in suspect_markers)

    creator = design.get("designCreator") or {}
    name = creator.get("name") or design.get("creatorName") or ""
    username = creator.get("username") or creator.get("handle") or design.get("creatorUsername") or ""
    url = ""
    avatar_url = ""
    cand = design.get("user") or design.get("author") or design.get("designCreator") or design.get("creator") or {}
    if isinstance(cand, dict):
        name = cand.get("nickname") or cand.get("name") or cand.get("username") or name
        username = cand.get("username") or cand.get("userName") or cand.get("slug") or cand.get("handle") or username
        url = cand.get("homepage") or cand.get("url") or ""
        avatar_url = cand.get("avatarUrl") or cand.get("avatar") or cand.get("headImg") or ""
    elif isinstance(cand, str) and not name:
        name = cand
    if _is_suspect_author_text(name):
        name = ""
    # 兜底从 design 层获取用户名
    if not username:
        username = design.get("creatorName") or design.get("creatorUsername") or username
    if url:
        if _is_suspect_author_href(url, name):
            url = ""
            username = ""
        else:
            handle_from_url = _extract_author_handle(url)
            if handle_from_url:
                # 统一收敛到作者主页地址，避免 /upload 等路径污染
                url = _build_author_url(handle_from_url)
                if not username:
                    username = handle_from_url

    # HTML 兜底：优先从作者链接提取 @userid
    # 若 design 中 url 被污染为 /browsing-history，也强制用 HTML 纠正。
    suspect_url = _is_suspect_author_href(url, name)
    if (not url or not avatar_url or not name or suspect_url) and html_text:
        try:
            soup = BeautifulSoup(html_text, "html.parser")
            link_candidates = list(soup.select("a.user_link[href]")) or list(
                soup.find_all("a", href=re.compile(r"/(?:zh/)?@"))
            )
            for link in link_candidates:
                href = link.get("href") or ""
                link_text = (link.get_text() or "").strip()
                if _is_suspect_author_href(href, link_text):
                    continue
                handle = _extract_author_handle(href)
                if not handle:
                    continue
                # 命中作者 id 后，始终使用标准主页地址
                if not url or suspect_url:
                    url = _build_author_url(handle)
                if not username:
                    username = handle
                if not name:
                    name = link_text if not _is_suspect_author_text(link_text) else ""
                if not avatar_url:
                    img = link.find("img")
                    if img and img.get("src"):
                        avatar_url = img.get("src")
                break
        except Exception as e:
            log("解析作者 DOM 失败:", e)

    # 最终统一为 https://makerworld.com.cn/zh/@{userid}
    final_handle = _extract_author_handle(url) or username
    if _is_suspect_author_text(name) or _is_suspect_author_href(url, name):
        name = ""
        final_handle = ""
    if final_handle:
        url = _build_author_url(final_handle)
    else:
        url = ""
    avatar_local = f"author_avatar.{pick_ext_from_url(avatar_url)}" if avatar_url else ""
    return {
        "name": name,
        "url": url,
        "avatarUrl": avatar_url,
        "avatarLocal": avatar_local,
    }


def _normalize_design_pictures(design: dict) -> List[dict]:
    pics = design.get("designExtension", {}).get("design_pictures")
    if isinstance(pics, list) and pics:
        return pics
    for key in ["designPictures", "design_pictures", "designImages", "images", "pictures"]:
        cand = design.get(key)
        if isinstance(cand, list) and cand:
            return cand
    cover_url = design.get("coverUrl") or design.get("coverImage") or design.get("thumbnail") or design.get("thumbnailUrl")
    if cover_url:
        return [{"url": cover_url}]
    return []


def _attachment_name_from_url(url: str, fallback: str) -> str:
    parsed = urlparse(url or "")
    raw_name = Path(parsed.path or "").name
    safe_name = sanitize_filename(raw_name)
    if safe_name:
        return safe_name
    return sanitize_filename(fallback) or fallback


def _dedupe_attachment_local_name(preferred: str, used: set[str]) -> str:
    candidate = sanitize_filename(preferred or "attachment") or "attachment"
    stem = Path(candidate).stem or "attachment"
    suffix = Path(candidate).suffix
    if candidate not in used:
        used.add(candidate)
        return candidate
    idx = 1
    while True:
        alt = sanitize_filename(f"{stem}_{idx}{suffix}") or f"attachment_{idx}{suffix}"
        if alt not in used:
            used.add(alt)
            return alt
        idx += 1


def extract_design_attachments(design: dict) -> List[dict]:
    ext = design.get("designExtension") or {}
    if not isinstance(ext, dict):
        return []

    category_map = {
        "design_guide": "guide",
        "design_bom": "bom",
        "design_other": "other",
    }
    attachments = []
    seen_urls = set()
    used_local_names = set()

    for key, category in category_map.items():
        items = ext.get(key)
        if not isinstance(items, list):
            continue
        for idx, item in enumerate(items, start=1):
            raw_name = ""
            url = ""
            if isinstance(item, dict):
                raw_name = str(item.get("name") or item.get("fileName") or item.get("filename") or item.get("title") or "").strip()
                url = str(item.get("url") or item.get("downloadUrl") or item.get("download_url") or item.get("src") or "").strip()
            elif isinstance(item, str):
                url = item.strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            fallback_name = f"{category}_{idx}.{pick_ext_from_url(url, 'bin')}"
            source_name = sanitize_filename(Path(raw_name).name) if raw_name else ""
            display_name = source_name or _attachment_name_from_url(url, fallback_name)
            local_name = _dedupe_attachment_local_name(display_name, used_local_names)

            attachments.append({
                "category": category,
                "name": display_name,
                "url": url,
                "source": "page",
                "localName": local_name,
                "relPath": f"file/{local_name}",
            })
    return attachments


def collect_design_images(
    design: dict,
    session: requests.Session,
    out_dir: Path,
    base_name: str,
    progress_callback=None,
    progress_start: int = 45,
    progress_end: int = 50,
    download_assets: bool = True,
    existing_images: Optional[List[dict]] = None,
):
    pics = _normalize_design_pictures(design)
    if not pics:
        return [], None
    design_images = []
    cover_meta = None
    existing_lookup = _build_existing_media_lookup(existing_images or [], url_fields=("originalUrl", "url"))
    design_download_tasks: list[dict[str, Any]] = []
    for idx, p in enumerate(pics, start=1):
        _emit_stage_progress(
            progress_callback,
            progress_start,
            progress_end,
            idx,
            len(pics),
            "正在下载设计图片" if download_assets else "正在整理设计图片",
        )
        url = ""
        if isinstance(p, str):
            url = p
        elif isinstance(p, dict):
            url = p.get("url") or p.get("imageUrl") or p.get("src") or p.get("originalUrl") or ""
        if not url:
            continue
        ext = pick_ext_from_url(url)
        fname = f"design_{idx:02d}.{ext}"
        rel = f"images/{fname}"
        existing_image = _match_existing_media_item_from_lookup(
            existing_lookup,
            url=url,
            index=idx,
        )
        if not download_assets:
            if _media_item_remote_matches(existing_image, url, url_fields=("originalUrl", "url")):
                rel_path = str(existing_image.get("relPath") or "").strip()
                file_name = str(existing_image.get("fileName") or "").strip()
            else:
                rel_path = ""
                file_name = ""
            meta = {
                "index": idx,
                "originalUrl": url,
                "relPath": rel_path,
                "fileName": file_name,
            }
            design_images.append(meta)
            if cover_meta is None:
                cover_meta = meta
            continue
        if _media_item_remote_matches(existing_image, url, url_fields=("originalUrl", "url")) and _media_item_local_exists(out_dir, existing_image):
            existing_rel, existing_file = _existing_media_ref(existing_image)
            meta = {
                "index": idx,
                "originalUrl": url,
                "relPath": existing_rel,
                "fileName": existing_file,
            }
            design_images.append(meta)
            if cover_meta is None:
                cover_meta = meta
            continue
        meta = {
            "index": idx,
            "originalUrl": url,
            "relPath": rel,
            "fileName": fname,
        }
        target = out_dir / fname
        design_download_tasks.append(
            {
                "url": url,
                "error_message": "设计图片下载失败，保留原始链接：",
                "download": lambda s=session, u=url, d=target: _download_asset_with_fresh_session(s, u, d),
                "apply": [
                    lambda meta=meta: design_images.append(meta)
                ],
            }
        )

    if design_download_tasks:
        _download_image_assets(
            design_download_tasks,
            progress_callback,
            progress_start,
            progress_end,
            "正在下载设计图片",
        )
    design_images.sort(key=lambda item: int(item.get("index") or 0) if isinstance(item, dict) else 0)
    if cover_meta is None and design_images:
        cover_meta = design_images[0]
    emit_progress(progress_callback, progress_end, "设计图片整理完成")
    return design_images, cover_meta


def _normalize_api_base(base: Optional[str]) -> Optional[str]:
    if not base:
        return None
    base = base.strip()
    if not base:
        return None
    if not base.startswith("http://") and not base.startswith("https://"):
        base = f"https://{base}"
    return base.rstrip("/")


def _unique_preserve(seq: List[str]) -> List[str]:
    seen = set()
    out = []
    for item in seq:
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _build_instance_api_candidates(
    inst_id: int,
    api_url: Optional[str],
    origin: Optional[str],
    api_host_hint: Optional[str],
) -> List[str]:
    candidates = []
    source = (
        normalize_makerworld_source(url=origin)
        or normalize_makerworld_source(url=api_url)
        or normalize_makerworld_source(url=api_host_hint)
    )

    bases = []
    preferred_api_bases = (
        ["https://api.bambulab.com", "https://api.bambulab.cn"]
        if source == "global"
        else ["https://api.bambulab.cn", "https://api.bambulab.com"]
    )
    for base in [api_host_hint, *preferred_api_bases, origin]:
        normalized = _normalize_api_base(base)
        if normalized and normalized not in bases:
            bases.append(normalized)

    for base in bases:
        host = urlparse(base).netloc.lower()
        if "api.bambulab" in host:
            path_templates = [
                "/v1/design-service/instance/{id}/f3mf",
            ]
        elif "makerworld.com" in host:
            path_templates = [
                "/api/v1/design-service/instance/{id}/f3mf",
            ]
        else:
            path_templates = [
                "/api/v1/design-service/instance/{id}/f3mf",
                "/v1/design-service/instance/{id}/f3mf",
            ]
        for path in path_templates:
            for file_type in ("3mf", ""):
                candidates.append(
                    f"{base}{path.format(id=inst_id)}?type=download&fileType={file_type}"
                )

    if api_url:
        candidates.append(api_url)

    return _unique_preserve(candidates)


def _extract_instance_download(data: object) -> tuple[str, str]:
    payload = data
    if isinstance(data, dict):
        payload = data.get("data") or data.get("result") or data
    if not isinstance(payload, dict):
        return "", ""
    name = (
        payload.get("name")
        or payload.get("fileName")
        or payload.get("filename")
        or payload.get("file_name")
        or ""
    )
    url = (
        payload.get("url")
        or payload.get("downloadUrl")
        or payload.get("download_url")
        or payload.get("downloadURL")
        or ""
    )
    return name or "", url or ""


def _normalize_url_value(value: object) -> str:
    if not isinstance(value, str):
        return ""
    raw = value.strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        return f"https:{raw}"
    return raw


def _normalize_asset_url_value(value: object) -> str:
    raw = _normalize_url_value(value)
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw.split("#", 1)[0]
    if not parsed.scheme and not parsed.netloc:
        return raw.split("#", 1)[0]
    kept_params: list[tuple[str, str]] = []
    for key, param_value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        compact = lowered.replace("_", "").replace("-", "")
        if lowered in VOLATILE_ASSET_QUERY_KEYS or compact in VOLATILE_ASSET_QUERY_KEYS:
            continue
        if any(lowered.startswith(prefix) for prefix in VOLATILE_ASSET_QUERY_PREFIXES):
            continue
        kept_params.append((key, param_value))
    query = urlencode(sorted(kept_params))
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, query, ""))


def _media_item_remote_matches(item: object, url: str, *, url_fields: tuple[str, ...]) -> bool:
    if not isinstance(item, dict):
        return False
    normalized_url = _normalize_asset_url_value(url)
    if not normalized_url:
        return False
    return any(_normalize_asset_url_value(item.get(field)) == normalized_url for field in url_fields)


def _media_item_local_path(
    base_dir: Path,
    item: object,
    *,
    rel_fields: tuple[str, ...] = ("relPath",),
    local_fields: tuple[str, ...] = ("fileName", "localName"),
) -> Optional[Path]:
    if not isinstance(item, dict):
        return None
    model_root = base_dir.parent if base_dir.name in {"images", "file"} else base_dir
    for field in rel_fields:
        rel_path = str(item.get(field) or "").strip().lstrip("/")
        if not rel_path:
            continue
        if rel_path.startswith(f"{SHARED_AVATAR_REL_DIR}/"):
            return model_root.parent / rel_path
        return model_root / rel_path
    for field in local_fields:
        local_name = str(item.get(field) or "").strip()
        if local_name:
            return base_dir / Path(local_name).name
    return None


def _media_item_local_exists(
    base_dir: Path,
    item: object,
    *,
    rel_fields: tuple[str, ...] = ("relPath",),
    local_fields: tuple[str, ...] = ("fileName", "localName"),
) -> bool:
    path = _media_item_local_path(base_dir, item, rel_fields=rel_fields, local_fields=local_fields)
    if path is None:
        return False
    try:
        return path.is_file()
    except OSError:
        return False


def _existing_media_ref(item: object, *, rel_field: str = "relPath", local_field: str = "fileName") -> tuple[str, str]:
    if not isinstance(item, dict):
        return "", ""
    rel_path = str(item.get(rel_field) or "").strip()
    local_name = str(item.get(local_field) or "").strip()
    if not rel_path and local_name:
        rel_path = f"images/{local_name}"
    return rel_path, local_name


def _looks_like_instance_api_url(url: object) -> bool:
    raw = _normalize_url_value(url).lower()
    if not raw:
        return False
    return "/f3mf" in raw or ("design-service/instance/" in raw and "download" in raw)


def _looks_like_3mf_file_url(url: object) -> bool:
    raw = _normalize_url_value(url).lower()
    if not raw:
        return False
    markers = (
        ".3mf",
        "filetype=3mf",
        "/f3mf/download",
        "content-disposition=",
        "application%2fvnd.ms-package.3dmanufacturing",
        "application/vnd.ms-package.3dmanufacturing",
    )
    return any(marker in raw for marker in markers)


def _instance_identity_keys(inst: object) -> List[str]:
    if not isinstance(inst, dict):
        return []
    keys: List[str] = []
    field_names = (
        "id",
        "profileId",
        "profile_id",
        "profileID",
        "instanceId",
        "instance_id",
        "instanceID",
    )
    for field in field_names:
        value = str(inst.get(field) or "").strip()
        if value:
            token = f"{field}:{value}"
            if token not in keys:
                keys.append(token)
    for field in ("fileName", "name", "title"):
        value = sanitize_filename(str(inst.get(field) or "").strip()).lower()
        if value:
            token = f"{field}:{value}"
            if token not in keys:
                keys.append(token)
    return keys


def _build_existing_instance_index(instances: object) -> Dict[str, dict]:
    index: Dict[str, dict] = {}
    if not isinstance(instances, list):
        return index
    for item in instances:
        if not isinstance(item, dict):
            continue
        for key in _instance_identity_keys(item):
            index.setdefault(key, item)
    return index


def _find_existing_instance(inst: object, instance_index: Dict[str, dict]) -> dict:
    if not isinstance(inst, dict) or not instance_index:
        return {}
    for key in _instance_identity_keys(inst):
        matched = instance_index.get(key)
        if matched:
            return matched
    return {}


def _extract_instance_download_hint(payload: object) -> tuple[str, str, str]:
    best_name = ""
    best_url = ""
    best_api_url = ""
    best_score = -1
    stack: list[tuple[object, int]] = [(payload, 0)]
    seen: set[int] = set()

    while stack:
        current, depth = stack.pop()
        if depth > 8:
            continue
        if isinstance(current, (dict, list)):
            marker = id(current)
            if marker in seen:
                continue
            seen.add(marker)

        if isinstance(current, dict):
            name = str(
                current.get("name")
                or current.get("fileName")
                or current.get("filename")
                or current.get("file_name")
                or current.get("title")
                or ""
            ).strip()
            file_type = str(current.get("fileType") or current.get("type") or current.get("ext") or "").strip().lower()
            api_url = _normalize_url_value(
                current.get("apiUrl")
                or current.get("api_url")
                or current.get("downloadApiUrl")
                or current.get("download_api_url")
            )
            explicit_url = _normalize_url_value(
                current.get("downloadUrl")
                or current.get("download_url")
                or current.get("downloadURL")
            )
            loose_url = _normalize_url_value(current.get("url") or current.get("src") or current.get("href"))

            candidate_url = ""
            if explicit_url:
                if _looks_like_instance_api_url(explicit_url):
                    api_url = api_url or explicit_url
                else:
                    candidate_url = explicit_url
            elif loose_url:
                if _looks_like_instance_api_url(loose_url):
                    api_url = api_url or loose_url
                elif _looks_like_3mf_file_url(loose_url) or file_type == "3mf" or name.lower().endswith(".3mf"):
                    candidate_url = loose_url

            score = 0
            if explicit_url and candidate_url:
                score += 60
            elif candidate_url:
                score += 35
            if _looks_like_3mf_file_url(candidate_url):
                score += 30
            if file_type == "3mf":
                score += 20
            if name.lower().endswith(".3mf"):
                score += 20
            score -= depth

            if candidate_url and score > best_score:
                best_name = name
                best_url = candidate_url
                best_api_url = api_url
                best_score = score
            elif api_url and not best_api_url:
                best_api_url = api_url

            for value in current.values():
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))
        elif isinstance(current, list):
            for value in current:
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))

    return best_name, best_url, best_api_url


def _looks_like_html(text: str) -> bool:
    if not text:
        return False
    head = text.lstrip()[:200].lower()
    return head.startswith("<!doctype html") or "<html" in head


def _stringify_3mf_failure_payload(payload) -> str:
    if payload in ("", None):
        return ""
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False)
    except Exception:
        return str(payload)


def _extract_three_mf_verification_payload(payload) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    captcha_id = str(
        payload.get("captchaId")
        or payload.get("captcha_id")
        or payload.get("captchaID")
        or ""
    ).strip()
    if not captcha_id:
        return {}
    return {
        "captcha_id": captcha_id,
        "provider": "geetest",
    }


def _classify_3mf_fetch_failure(
    *,
    status_code: int = 0,
    text: str = "",
    payload=None,
    cloudflare: bool = False,
    source: str = "",
) -> dict:
    raw_text = str(text or "").strip()
    payload_text = _stringify_3mf_failure_payload(payload)
    combined = " ".join(part for part in (raw_text, payload_text) if part).lower()
    normalized_source = normalize_makerworld_source(source=source)

    if is_three_mf_daily_download_limited(combined):
        return {
            "state": "download_limited",
            "message": describe_three_mf_failure("download_limited", source=normalized_source),
        }
    if status_code == 418 or any(
        keyword in combined
        for keyword in (
            "captcha",
            "verification required",
            "verify you are human",
            "security check",
            "challenge",
        )
    ):
        result = {
            "state": "verification_required",
            "message": describe_three_mf_failure("verification_required", source=normalized_source),
        }
        verification = _extract_three_mf_verification_payload(payload)
        if verification:
            result["verification"] = verification
        return result
    if "please log in to download models" in combined or "log in to download models" in combined:
        return {
            "state": "auth_required",
            "message": describe_three_mf_failure("auth_required", source=normalized_source),
        }
    if status_code in {401, 403}:
        return {
            "state": "auth_required",
            "message": describe_three_mf_failure("auth_required", source=normalized_source),
        }
    if _is_makerworld_not_found_page(raw_text):
        return {
            "state": "not_found",
            "message": _makerworld_not_found_message(),
        }
    if status_code == 404 or "route not found" in combined or "\"detail\":\"not found\"" in combined or "not found" in combined:
        return {
            "state": "not_found",
            "message": describe_three_mf_failure("not_found", source=normalized_source),
        }
    if cloudflare or _is_cloudflare_challenge(raw_text) or _looks_like_html(raw_text):
        return {
            "state": "cloudflare",
            "message": describe_three_mf_failure("cloudflare", source=normalized_source),
        }
    if status_code >= 400:
        return {
            "state": "http_error",
            "message": f"下载 3MF 失败：上游返回 HTTP {status_code}。",
        }
    return {
        "state": "missing",
        "message": "未获取到 3MF 下载地址。",
    }


def _should_stop_three_mf_fetch(failure: Optional[dict]) -> bool:
    return _should_pause_three_mf_fetch(failure)


def _summarize_three_mf_fetch_attempts(attempts: list[dict]) -> str:
    if not attempts:
        return "no-attempts"
    status_counts: dict[str, int] = {}
    state_counts: dict[str, int] = {}
    host_counts: dict[str, int] = {}
    for attempt in attempts:
        status = str(attempt.get("status") or "error")
        state = str(attempt.get("state") or "unknown")
        host = urlparse(str(attempt.get("url") or "")).netloc or "unknown-host"
        status_counts[status] = status_counts.get(status, 0) + 1
        state_counts[state] = state_counts.get(state, 0) + 1
        host_counts[host] = host_counts.get(host, 0) + 1

    def _compact(values: dict[str, int]) -> str:
        return ",".join(f"{key}:{count}" for key, count in sorted(values.items())[:8])

    return f"attempts={len(attempts)} statuses={_compact(status_counts)} states={_compact(state_counts)} hosts={_compact(host_counts)}"


def _browser_three_mf_authorization_failure(
    *,
    status_code: int = 0,
    text: str = "",
    payload: Optional[dict] = None,
    source: str = "",
) -> dict:
    failure = _classify_3mf_fetch_failure(
        status_code=status_code,
        text=text,
        payload=payload,
        source=source,
    )
    if str(failure.get("state") or "") in {"auth_required", "cookie_invalid", "cloudflare", "verification_required"}:
        return {
            "state": "verification_required",
            "message": "指纹浏览器未取得 3MF 授权，请在官网完成验证后点击“已验证”继续归档。",
        }
    return failure


def _normalized_auto_verification_diagnostics(verification: object) -> dict:
    if not isinstance(verification, dict):
        return {"attempted": False, "completed": False, "fields": {}}

    def _safe_code(value: object, allowed: frozenset[str]) -> str:
        if isinstance(value, str) and value in allowed:
            return value
        return "unknown"

    def _bounded_attempts(value: object) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return max(0, min(value, 99))
        if not isinstance(value, float) or not math.isfinite(value):
            return 0
        if value <= 0:
            return 0
        if value >= 99:
            return 99
        return int(value)

    def _bounded_confidence(value: object) -> float:
        if isinstance(value, bool):
            return 0.0
        if isinstance(value, int):
            return float(max(0, min(value, 1)))
        if not isinstance(value, float) or not math.isfinite(value):
            return 0.0
        return round(max(0.0, min(float(value), 1.0)), 2)

    return {
        "attempted": verification.get("attempted") is True,
        "completed": verification.get("completed") is True,
        "fields": {
            "provider": _safe_code(verification.get("provider"), SAFE_VERIFICATION_PROVIDERS),
            "challenge_type": _safe_code(verification.get("challenge_type"), SAFE_VERIFICATION_CHALLENGE_TYPES),
            "attempts": _bounded_attempts(verification.get("attempts")),
            "reason": _safe_code(verification.get("reason"), SAFE_REASON_CODES),
            "confidence": _bounded_confidence(verification.get("confidence")),
        },
    }


def _log_auto_verification_result(diagnostics: dict, *, signed_url_available: bool) -> None:
    fields = diagnostics.get("fields") if isinstance(diagnostics.get("fields"), dict) else {}
    try:
        if diagnostics.get("completed") is True and signed_url_available:
            append_business_log(
                "archive",
                "cloakbrowser_auto_verification_completed",
                "指纹浏览器已完成 3MF 自动验证。",
                level="info",
                **fields,
            )
        elif diagnostics.get("attempted") is True and not signed_url_available:
            append_business_log(
                "archive",
                "cloakbrowser_auto_verification_fallback",
                "指纹浏览器未取得 3MF 授权，请在官网完成验证后点击“已验证”继续归档。",
                level="warning",
                **fields,
            )
    except Exception:
        pass


def browser_authorize_3mf_download(
    platform: str,
    api_url: str,
    *,
    profile_id: str,
    model_url: str = "",
    instance_id: str = "",
) -> dict:
    # Import lazily because cloakbrowser_session reaches legacy_archiver through account discovery.
    from app.services.cloakbrowser_session import browser_authorize_3mf_download as authorize

    return authorize(
        platform,
        api_url,
        profile_id=profile_id,
        model_url=model_url,
        instance_id=instance_id,
    )


def fetch_instance_3mf(
    session: requests.Session,
    inst_id: int,
    raw_cookie: str,
    api_url: str = None,
    api_host_hint: Optional[str] = None,
    origin: Optional[str] = None,
    captcha_result_header: str = "",
    browser_authorization: bool = False,
    browser_profile_id: str = "",
    model_page_url: str = "",
):
    """
    获取实例的 3MF 下载地址，允许外部传入 api_url，并自动回退不同 API Host。
    返回: (name, url, used_api_url, failure_info)
    """
    if fake_three_mf_downloads_enabled():
        fake_name = f"{sanitize_filename(str(inst_id or 'model')) or 'model'}.3mf"
        return fake_name, _fake_three_mf_download_url(inst_id), api_url or "", {
            "state": "available",
            "message": FAKE_THREE_MF_DOWNLOAD_MESSAGE,
        }
    effective_cookie = sanitize_cookie_header(raw_cookie) or _session_cookie_header(session)
    candidates = _build_instance_api_candidates(inst_id, api_url, origin, api_host_hint)
    auth_token = _extract_auth_token(effective_cookie)
    last_error = None
    last_failure = {"state": "missing", "message": "未获取到 3MF 下载地址。"}
    attempts: list[dict] = []
    source_hint = (
        normalize_makerworld_source(url=origin)
        or normalize_makerworld_source(url=api_url)
        or normalize_makerworld_source(url=api_host_hint)
    )
    if candidates:
        _wait_before_three_mf_download(f"获取下载地址 {inst_id}")
    if browser_authorization:
        candidate = candidates[0] if candidates else api_url or ""
        if not candidate:
            return "", "", "", {"state": "missing", "message": "未获取到 3MF 下载地址。"}
        try:
            browser_result = browser_authorize_3mf_download(
                source_hint or normalize_makerworld_source(url=candidate),
                candidate,
                profile_id=browser_profile_id,
                model_url=model_page_url or origin or "",
                instance_id=str(inst_id or ""),
            )
        except Exception as exc:
            from app.services.cloakbrowser_session import CloakBrowserError

            if not isinstance(exc, CloakBrowserError):
                raise
            error_detail = str(exc).strip()[:300]
            log(None, "指纹浏览器 3MF 授权失败:", error_detail or type(exc).__name__)
            try:
                append_business_log(
                    "archive",
                    "cloakbrowser_3mf_authorization_error",
                    "指纹浏览器暂时无法完成 3MF 授权。",
                    level="warning",
                    instance_id=str(inst_id or ""),
                    model_url=_trace_url(model_page_url or origin or ""),
                    error=error_detail,
                )
            except Exception:
                pass
            return "", "", candidate, {
                "state": "http_error",
                "message": "指纹浏览器暂时无法完成 3MF 授权，将稍后自动重试。",
            }
        browser_payload = browser_result.get("payload") if isinstance(browser_result.get("payload"), dict) else {}
        verification_diagnostics = _normalized_auto_verification_diagnostics(browser_result.get("verification"))
        name, url = _extract_instance_download(browser_payload)
        if url:
            _log_auto_verification_result(verification_diagnostics, signed_url_available=True)
            return name, url, candidate, {"state": "available", "message": ""}
        failure = _browser_three_mf_authorization_failure(
            status_code=int(browser_result.get("status_code") or 0),
            text=str(browser_result.get("text") or ""),
            payload=browser_payload,
            source=source_hint or candidate,
        )
        _log_auto_verification_result(verification_diagnostics, signed_url_available=False)
        return "", "", candidate, failure
    for candidate in candidates:
        candidate_source = source_hint or normalize_makerworld_source(url=candidate)
        cookie_header = effective_cookie
        headers = {
            **MAKERWORLD_API_BROWSER_HEADERS,
            "Accept": "application/json, text/plain, */*",
            "Referer": origin or "https://makerworld.com.cn/",
            "Origin": origin or "https://makerworld.com.cn",
            "User-Agent": session.headers.get("User-Agent", "Mozilla/5.0 (MW-Fetcher)"),
        }
        if cookie_header:
            headers["Cookie"] = cookie_header
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
            headers["token"] = auth_token
            headers["X-Token"] = auth_token
            headers["X-Access-Token"] = auth_token
        clean_captcha_result = str(captcha_result_header or "").strip()
        if clean_captcha_result:
            headers["x-bbl-captcha-result"] = clean_captcha_result
        try:
            data = makerworld_browser_get_json(
                candidate,
                raw_cookie=cookie_header,
                headers=headers,
                session=session,
                allow_non_json=True,
            )
        except MakerWorldBrowserError as e:
            last_error = e
            failure = _classify_3mf_fetch_failure(text=str(e), source=candidate_source)
            last_failure = merge_three_mf_failure(last_failure, failure)
            attempts.append({"method": "cloakbrowser", "url": candidate, "status": "exception", "state": failure.get("state")})
            if _should_stop_three_mf_fetch(failure):
                log("3MF 获取失败", inst_id, str(failure.get("state") or "missing"), str(failure.get("message") or ""))
                return "", "", candidate, last_failure
            continue
        if not isinstance(data, dict):
            failure = _classify_3mf_fetch_failure(text="", source=candidate_source)
            last_failure = merge_three_mf_failure(last_failure, failure)
            attempts.append({"method": "cloakbrowser", "url": candidate, "status": "non-json", "state": failure.get("state")})
            continue
        name, url = _extract_instance_download(data)
        if url:
            return name, url, candidate, {"state": "available", "message": ""}
        text_preview = json.dumps(data, ensure_ascii=False)[:400]
        failure = _classify_3mf_fetch_failure(
            status_code=200,
            text=text_preview,
            payload=data,
            source=candidate_source,
        )
        last_failure = merge_three_mf_failure(last_failure, failure)
        attempts.append({"method": "cloakbrowser", "url": candidate, "status": 200, "state": failure.get("state")})
        if _should_stop_three_mf_fetch(failure):
            log("3MF 获取失败", inst_id, str(failure.get("state") or "missing"), str(failure.get("message") or ""))
            return "", "", candidate, last_failure

    if _should_stop_three_mf_fetch(last_failure):
        log("3MF 获取失败", inst_id, str(last_failure.get("state") or "missing"), str(last_failure.get("message") or ""))
        return "", "", candidates[-1] if candidates else api_url or "", last_failure
    log("3MF 获取失败", inst_id, _summarize_three_mf_fetch_attempts(attempts), str(last_failure.get("message") or ""))
    return "", "", api_url or "", last_failure

def _match_existing_media_item(items: object, *, url: str = "", index: object = None, url_fields: tuple[str, ...] = ("url",)) -> dict:
    lookup = _build_existing_media_lookup(items, url_fields=url_fields)
    return _match_existing_media_item_from_lookup(lookup, url=url, index=index)


def _build_existing_media_lookup(items: object, *, url_fields: tuple[str, ...] = ("url",)) -> dict[str, dict[str, dict]]:
    by_url: dict[str, dict] = {}
    by_index: dict[str, dict] = {}
    if not isinstance(items, list):
        return {"by_url": by_url, "by_index": by_index}
    for item in items:
        if not isinstance(item, dict):
            continue
        for field in url_fields:
            candidate = _normalize_asset_url_value(item.get(field))
            if candidate and candidate not in by_url:
                by_url[candidate] = item
        item_index = str(item.get("index") if item.get("index") is not None else "").strip()
        if item_index and item_index not in by_index:
            by_index[item_index] = item
    return {"by_url": by_url, "by_index": by_index}


def _match_existing_media_item_from_lookup(
    lookup: Optional[dict[str, dict[str, dict]]],
    *,
    url: str = "",
    index: object = None,
) -> dict:
    if not isinstance(lookup, dict):
        return {}
    normalized_url = _normalize_asset_url_value(url)
    normalized_index = str(index if index is not None else "").strip()
    if normalized_url:
        matched = (lookup.get("by_url") or {}).get(normalized_url)
        if matched:
            return matched
    if normalized_index:
        matched = (lookup.get("by_index") or {}).get(normalized_index)
        if matched:
            return matched
    return {}


def collect_instance_media(
    inst: dict,
    session: requests.Session,
    out_dir: Path,
    base_name: str,
    *,
    download_assets: bool = True,
    existing_instance: Optional[dict] = None,
):
    model_info = (
        inst.get("extention", {}).get("modelInfo")
        or inst.get("extension", {}).get("modelInfo")
        or inst.get("modelInfo")
        or {}
    )
    plates = model_info.get("plates") or model_info.get("plateList") or []
    aux_pics = model_info.get("auxiliaryPictures") or model_info.get("pictures") or inst.get("pictures") or inst.get("auxiliaryPictures") or []
    existing_plates = existing_instance.get("plates") if isinstance(existing_instance, dict) else []
    existing_pics = existing_instance.get("pictures") if isinstance(existing_instance, dict) else []
    existing_plate_lookup = _build_existing_media_lookup(
        existing_plates,
        url_fields=("thumbnailUrl", "url"),
    )
    existing_pic_lookup = _build_existing_media_lookup(
        existing_pics,
        url_fields=("url", "originalUrl"),
    )
    plate_out = []
    pics_out = []
    # plates thumbs
    for p in plates:
        thumb = p.get("thumbnail", {}).get("url") or p.get("thumbnailUrl") or p.get("url")
        if not thumb:
            continue
        plate_index = int(p.get("index", 0))
        existing_plate = _match_existing_media_item_from_lookup(
            existing_plate_lookup,
            url=thumb,
            index=plate_index,
        )
        record = {
            "index": p.get("index", 0),
            "prediction": p.get("prediction"),
            "weight": p.get("weight"),
            "filaments": p.get("filaments") or [],
            "thumbnailUrl": thumb,
        }
        plate_remote_matches = _media_item_remote_matches(existing_plate, thumb, url_fields=("thumbnailUrl", "url"))
        if plate_remote_matches and str(existing_plate.get("thumbnailRelPath") or "").strip():
            record["thumbnailRelPath"] = str(existing_plate.get("thumbnailRelPath") or "").strip()
        if plate_remote_matches and str(existing_plate.get("thumbnailFile") or "").strip():
            record["thumbnailFile"] = str(existing_plate.get("thumbnailFile") or "").strip()
        if download_assets:
            ext = pick_ext_from_url(thumb)
            fname = sanitize_filename(
                str(existing_plate.get("thumbnailFile") or f"{base_name}_inst{inst.get('id')}_plate_{plate_index:02d}.{ext}")
            ) or f"{base_name}_inst{inst.get('id')}_plate_{plate_index:02d}.{ext}"
            if not (
                plate_remote_matches
                and _media_item_local_exists(out_dir, existing_plate, rel_fields=("thumbnailRelPath",), local_fields=("thumbnailFile",))
            ):
                try:
                    download_file(
                        session,
                        thumb,
                        out_dir / fname,
                        overwrite=True,
                        max_duration=IMAGE_TRANSFER_TIMEOUT_SECONDS,
                    )
                    record["thumbnailRelPath"] = f"images/{fname}"
                    record["thumbnailFile"] = fname
                except Exception as exc:
                    _log_asset_download_failure("实例分盘缩略图下载失败，保留原始链接：", thumb, exc)
        plate_out.append(record)
    # auxiliary pictures
    pic_idx = 1
    for pic in aux_pics:
        url = ""
        is_real = 0
        if isinstance(pic, str):
            url = pic
        elif isinstance(pic, dict):
            url = pic.get("url") or pic.get("imageUrl") or pic.get("src") or ""
            is_real = pic.get("isRealLifePhoto", 0)
        if not url:
            continue
        existing_pic = _match_existing_media_item_from_lookup(
            existing_pic_lookup,
            url=url,
            index=pic_idx,
        )
        record = {
            "index": pic_idx,
            "url": url,
            "isRealLifePhoto": is_real,
        }
        pic_remote_matches = _media_item_remote_matches(existing_pic, url, url_fields=("url", "originalUrl"))
        if pic_remote_matches and str(existing_pic.get("relPath") or "").strip():
            record["relPath"] = str(existing_pic.get("relPath") or "").strip()
        if pic_remote_matches and str(existing_pic.get("fileName") or "").strip():
            record["fileName"] = str(existing_pic.get("fileName") or "").strip()
        if download_assets:
            ext = pick_ext_from_url(url)
            fname = sanitize_filename(
                str(existing_pic.get("fileName") or f"{base_name}_inst{inst.get('id')}_pic_{pic_idx:02d}.{ext}")
            ) or f"{base_name}_inst{inst.get('id')}_pic_{pic_idx:02d}.{ext}"
            if not (
                pic_remote_matches
                and _media_item_local_exists(out_dir, existing_pic)
            ):
                try:
                    download_file(
                        session,
                        url,
                        out_dir / fname,
                        overwrite=True,
                        max_duration=IMAGE_TRANSFER_TIMEOUT_SECONDS,
                    )
                    record["relPath"] = f"images/{fname}"
                    record["fileName"] = fname
                except Exception as exc:
                    _log_asset_download_failure("实例图片下载失败，保留原始链接：", url, exc)
        pics_out.append(record)
        pic_idx += 1
    if not pics_out:
        cover = inst.get("cover") or inst.get("coverUrl")
        if cover:
            existing_pic = _match_existing_media_item_from_lookup(
                existing_pic_lookup,
                url=cover,
                index=pic_idx,
            )
            record = {
                "index": pic_idx,
                "url": cover,
                "isRealLifePhoto": 0,
            }
            cover_remote_matches = _media_item_remote_matches(existing_pic, cover, url_fields=("url", "originalUrl"))
            if cover_remote_matches and str(existing_pic.get("relPath") or "").strip():
                record["relPath"] = str(existing_pic.get("relPath") or "").strip()
            if cover_remote_matches and str(existing_pic.get("fileName") or "").strip():
                record["fileName"] = str(existing_pic.get("fileName") or "").strip()
            if download_assets:
                ext = pick_ext_from_url(cover)
                fname = sanitize_filename(
                    str(existing_pic.get("fileName") or f"{base_name}_inst{inst.get('id')}_pic_{pic_idx:02d}.{ext}")
                ) or f"{base_name}_inst{inst.get('id')}_pic_{pic_idx:02d}.{ext}"
                if not (
                    cover_remote_matches
                    and _media_item_local_exists(out_dir, existing_pic)
                ):
                    try:
                        download_file(
                            session,
                            cover,
                            out_dir / fname,
                            overwrite=True,
                            max_duration=IMAGE_TRANSFER_TIMEOUT_SECONDS,
                        )
                        record["relPath"] = f"images/{fname}"
                        record["fileName"] = fname
                    except Exception as exc:
                        _log_asset_download_failure("实例封面下载失败，保留原始链接：", cover, exc)
            pics_out.append(record)
    return plate_out, pics_out


def extract_instances(design: dict) -> List[dict]:
    for key in ["instances", "instanceList", "modelInstances", "profiles", "printProfiles", "printingProfiles"]:
        cand = design.get(key)
        if isinstance(cand, list) and cand:
            return cand
    return []


PROFILE_DETAIL_SCHEMA_VERSION = 4
COMMENT_SCHEMA_VERSION = 4
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _coerce_number(value: Any) -> Optional[float]:
    if value in ("", None):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number >= 0 else None
    raw = str(value).strip()
    if not raw:
        return None
    match = _NUMBER_RE.search(raw.replace(",", ""))
    if not match:
        return None
    try:
        number = float(match.group(0))
    except ValueError:
        return None
    return number if number >= 0 else None


def _round_profile_number(value: Any, digits: int = 2) -> Optional[float]:
    number = _coerce_number(value)
    if number is None:
        return None
    rounded = round(number, digits)
    return int(rounded) if float(rounded).is_integer() else rounded


def _walk_values(payload: Any, max_depth: int = 6):
    stack: list[tuple[Any, int]] = [(payload, 0)]
    seen: set[int] = set()
    while stack:
        current, depth = stack.pop()
        if depth > max_depth:
            continue
        if isinstance(current, (dict, list)):
            marker = id(current)
            if marker in seen:
                continue
            seen.add(marker)
        yield current
        if isinstance(current, dict):
            for value in current.values():
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))
        elif isinstance(current, list):
            for value in current:
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))


def _first_value_by_keys(payload: Any, keys: tuple[str, ...]) -> Any:
    wanted = {key.lower() for key in keys}
    for current in _walk_values(payload):
        if not isinstance(current, dict):
            continue
        for key, value in current.items():
            if str(key).lower() in wanted and value not in ("", None, [], {}):
                return value
    return None


def _normalize_rgb_triplet(values: Any) -> str:
    if not isinstance(values, (list, tuple)) or len(values) < 3:
        return ""
    numbers = []
    for value in values[:3]:
        number = _coerce_number(value)
        if number is None:
            return ""
        numbers.append(max(0, min(int(round(number)), 255)))
    return f"rgb({numbers[0]}, {numbers[1]}, {numbers[2]})"


def _normalize_color_value(value: Any) -> str:
    if value in ("", None):
        return ""
    if isinstance(value, dict):
        for key in ("hex", "color", "colorHex", "rgb", "value"):
            normalized = _normalize_color_value(value.get(key))
            if normalized:
                return normalized
        rgb = _normalize_rgb_triplet([value.get("r"), value.get("g"), value.get("b")])
        if rgb:
            return rgb
        return ""
    if isinstance(value, (list, tuple)):
        return _normalize_rgb_triplet(value)

    raw = str(value).strip()
    if not raw:
        return ""
    if raw.lower().startswith("rgb"):
        numbers = [int(float(item)) for item in re.findall(r"\d+(?:\.\d+)?", raw)[:3]]
        if len(numbers) == 3:
            return f"rgb({numbers[0]}, {numbers[1]}, {numbers[2]})"
        return raw
    if re.fullmatch(r"#[0-9a-fA-F]{3}", raw):
        return "#" + "".join(char * 2 for char in raw[1:])
    if re.fullmatch(r"#[0-9a-fA-F]{6}", raw):
        return raw
    if re.fullmatch(r"[0-9a-fA-F]{6}", raw):
        return f"#{raw}"
    if "," in raw:
        rgb = _normalize_rgb_triplet([part.strip() for part in raw.split(",")])
        if rgb:
            return rgb
    return raw


def _truthy_flag(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value in ("", None):
        return None
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "y", "需要", "是"}:
        return True
    if raw in {"0", "false", "no", "n", "不需要", "否"}:
        return False
    return None


_FILAMENT_MATERIAL_KEYS = (
    "material",
    "materialName",
    "filamentType",
    "filament_type",
    "type",
    "name",
    "filamentName",
    "filament",
)
_FILAMENT_COLOR_KEYS = (
    "color",
    "hex",
    "colorHex",
    "color_hex",
    "filamentColor",
    "filament_color",
    "materialColor",
    "trayColor",
    "displayColor",
)
_FILAMENT_WEIGHT_KEYS = (
    "weight",
    "weightUsed",
    "weightLabel",
    "weight_label",
    "weightG",
    "weight_g",
    "usedWeight",
    "used_weight",
    "usedWeightG",
    "used_weight_g",
    "usedG",
    "used_g",
    "filamentWeight",
    "filamentWeightG",
    "filament_weight",
    "filament_weight_g",
    "materialWeight",
    "materialWeightG",
    "material_weight",
    "material_weight_g",
    "grams",
    "gram",
    "usedGrams",
    "usage",
    "usageG",
    "used",
    "consume",
    "consumeG",
    "consumption",
    "consumptionG",
)
_FILAMENT_WEIGHT_LIST_KEYS = (
    "weight",
    "weights",
    "weightList",
    "weight_list",
    "filamentWeight",
    "filament_weight",
    "filamentWeights",
    "filament_weights",
    "filamentWeightList",
    "filament_weight_list",
    "materialWeight",
    "material_weight",
    "materialWeights",
    "material_weights",
    "materialWeightList",
    "material_weight_list",
    "usedWeight",
    "used_weight",
    "usedWeights",
    "used_weights",
    "usedWeightList",
    "used_weight_list",
    "consumption",
    "consumptionWeights",
    "consumption_weights",
    "consumptionWeightList",
    "consumption_weight_list",
)
_FILAMENT_WRAPPER_KEYS = (
    "trayInfo",
    "tray_info",
    "filamentInfo",
    "filament_info",
    "materialInfo",
    "material_info",
    "consumableInfo",
    "consumable_info",
)
_PROFILE_PRINT_TIME_KEYS = (
    "printTimeSeconds",
    "print_time_seconds",
    "printingTimeSeconds",
    "printing_time_seconds",
    "estimatedPrintTimeSeconds",
    "estimated_print_time_seconds",
    "durationSeconds",
    "duration_seconds",
    "printTime",
    "print_time",
    "printingTime",
    "printing_time",
    "estimatedPrintTime",
    "estimated_print_time",
    "duration",
)


def _extract_filament_weight_sequence(value: Any) -> list[Optional[float]]:
    if not isinstance(value, list):
        return []

    weights: list[Optional[float]] = []
    has_weight = False
    for entry in value:
        candidate = entry
        if isinstance(entry, dict):
            candidate = _first_value_by_keys(entry, _FILAMENT_WEIGHT_KEYS)
        weight = _round_profile_number(candidate, digits=1)
        weights.append(weight)
        has_weight = has_weight or weight is not None
    return weights if has_weight else []


def _first_filament_weight_sequence(container: Any, expected_count: int) -> list[Optional[float]]:
    if not isinstance(container, dict) or expected_count <= 0:
        return []

    wanted = {key.lower() for key in _FILAMENT_WEIGHT_LIST_KEYS}
    for key, value in container.items():
        if str(key).lower() not in wanted:
            continue
        weights = _extract_filament_weight_sequence(value)
        if len(weights) == expected_count:
            return weights
    return []


def _apply_parallel_filament_weights(items: list[Any], container: Any) -> list[Any]:
    weights = _first_filament_weight_sequence(container, len(items))
    if not weights:
        return items

    weighted_items: list[Any] = []
    for item, weight in zip(items, weights):
        if weight is None:
            weighted_items.append(item)
            continue
        if isinstance(item, dict):
            current_weight = _round_profile_number(_first_value_by_keys(item, _FILAMENT_WEIGHT_KEYS), digits=1)
            if current_weight in (None, 0):
                merged = dict(item)
                merged["weight"] = weight
                weighted_items.append(merged)
                continue
        weighted_items.append(item)
    return weighted_items


def _dict_get_case_insensitive(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    wanted = {key.lower() for key in keys}
    for key, value in item.items():
        if str(key).lower() in wanted:
            return value
    return None


def _has_direct_filament_signal(item: dict[str, Any]) -> bool:
    item_keys = {str(key).lower() for key in item.keys()}
    material_keys = {key.lower() for key in _FILAMENT_MATERIAL_KEYS if key != "name"}
    color_keys = {key.lower() for key in _FILAMENT_COLOR_KEYS}
    return bool(item_keys & (material_keys | color_keys))


def _is_filament_candidate_entry(entry: Any) -> bool:
    if isinstance(entry, str):
        return bool(entry.strip())
    if not isinstance(entry, dict):
        return False
    if _has_direct_filament_signal(entry):
        return True
    wrapped = _dict_get_case_insensitive(entry, _FILAMENT_WRAPPER_KEYS)
    return isinstance(wrapped, dict) and _is_filament_candidate_entry(wrapped)


def _normalize_filament_item(item: Any, default_ams: bool = False) -> Optional[dict[str, Any]]:
    if isinstance(item, str):
        material = item.strip()
        return {"material": material, "color": "", "weight": 0, "ams": default_ams} if material else None
    if not isinstance(item, dict):
        return None

    material = str(
        _first_value_by_keys(
            item,
            _FILAMENT_MATERIAL_KEYS,
        )
        or ""
    ).strip()
    if "｜" in material:
        material = material.split("｜", 1)[0].strip()
    if "|" in material:
        material = material.split("|", 1)[0].strip()

    color = _normalize_color_value(
        _first_value_by_keys(
            item,
            _FILAMENT_COLOR_KEYS,
        )
    )
    weight = _round_profile_number(
        _first_value_by_keys(
            item,
            _FILAMENT_WEIGHT_KEYS,
        ),
        digits=1,
    )
    slot = _first_value_by_keys(item, ("slot", "slotIndex", "trayIndex", "trayId", "index"))
    ams_flag = _truthy_flag(_first_value_by_keys(item, ("ams", "isAms", "isAMS", "needAms")))

    if not material and not color and weight in (None, 0):
        return None
    result: dict[str, Any] = {
        "material": material or "耗材",
        "color": color,
        "weight": weight or 0,
        "ams": default_ams if ams_flag is None else ams_flag,
    }
    if slot not in ("", None):
        result["slot"] = slot
    return result


def _collect_recursive_filament_items(payload: Any) -> list[Any]:
    fallback_items: list[Any] = []
    seen_signatures: set[str] = set()
    for current in _walk_values(payload):
        if isinstance(current, dict):
            for filament_key in ("instanceFilaments", "filaments", "filamentList", "materials"):
                source = current.get(filament_key)
                if not isinstance(source, list) or not source:
                    continue
                for entry in _apply_parallel_filament_weights(source, current):
                    if _normalize_filament_item(entry) is None:
                        continue
                    try:
                        signature = json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)
                    except TypeError:
                        signature = repr(entry)
                    if signature in seen_signatures:
                        continue
                    seen_signatures.add(signature)
                    fallback_items.append(entry)
        if not isinstance(current, list) or not current:
            continue
        for entry in current:
            if not _is_filament_candidate_entry(entry):
                continue
            if _normalize_filament_item(entry) is None:
                continue
            if isinstance(entry, (dict, list)):
                try:
                    signature = json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)
                except TypeError:
                    signature = repr(entry)
            else:
                signature = str(entry)
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            fallback_items.append(entry)
    return fallback_items


def _collect_raw_filament_items(inst: dict, plates: list[dict]) -> list[Any]:
    model_info = (
        inst.get("extention", {}).get("modelInfo")
        or inst.get("extension", {}).get("modelInfo")
        or inst.get("modelInfo")
        or {}
    )
    sources = [
        inst.get("instanceFilaments"),
        inst.get("filaments"),
        inst.get("filamentList"),
        inst.get("materials"),
        model_info.get("instanceFilaments"),
        model_info.get("filaments"),
        model_info.get("filamentList"),
        model_info.get("materials"),
    ]
    raw_items: list[Any] = []
    for source in sources:
        if isinstance(source, list):
            raw_items.extend(source)
    for container in (inst, model_info):
        if not isinstance(container, dict):
            continue
        for filament_key in ("instanceFilaments", "filaments", "filamentList", "materials"):
            source = container.get(filament_key)
            if isinstance(source, list):
                raw_items.extend(_apply_parallel_filament_weights(source, container))

    raw_items.extend(_collect_recursive_filament_items(inst))

    for plate in plates or []:
        if not isinstance(plate, dict):
            continue
        filaments = plate.get("filaments")
        if isinstance(filaments, list):
            raw_items.extend(filaments)

    deduped_items: list[Any] = []
    seen_signatures: set[str] = set()
    for entry in raw_items:
        if _normalize_filament_item(entry) is None:
            continue
        if isinstance(entry, (dict, list)):
            try:
                signature = json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)
            except TypeError:
                signature = repr(entry)
        else:
            signature = str(entry)
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        deduped_items.append(entry)
    return deduped_items


def _merge_profile_filaments(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    order: list[tuple[str, str, str]] = []
    for item in items:
        material = str(item.get("material") or "耗材").strip()
        color = str(item.get("color") or "").strip()
        slot = str(item.get("slot") or "").strip()
        key = (material.lower(), color.lower(), slot)
        if key not in merged:
            merged[key] = {
                "material": material,
                "color": color,
                "weight": 0,
                "ams": bool(item.get("ams")),
            }
            if slot:
                merged[key]["slot"] = item.get("slot")
            order.append(key)
        merged[key]["weight"] = _round_profile_number(
            float(merged[key].get("weight") or 0) + float(item.get("weight") or 0),
            digits=1,
        ) or 0
        merged[key]["ams"] = bool(merged[key].get("ams") or item.get("ams"))
    return [merged[key] for key in order]


def _plate_total_filament_weight(plates: list[dict]) -> Optional[float]:
    total = 0.0
    found = False
    for plate in plates or []:
        if not isinstance(plate, dict):
            continue
        prediction = plate.get("prediction") if isinstance(plate.get("prediction"), dict) else {}
        weight = _round_profile_number(
            plate.get("filamentWeight")
            or plate.get("materialWeight")
            or plate.get("weight")
            or prediction.get("filamentWeight")
            or prediction.get("materialWeight")
            or prediction.get("weight"),
            digits=1,
        )
        if weight is None:
            continue
        total += float(weight)
        found = True
    if not found:
        return None
    return _round_profile_number(total, digits=1)


def _prediction_time_seconds(payload: Any) -> Optional[float]:
    if isinstance(payload, dict):
        return _round_profile_number(_first_value_by_keys(payload, _PROFILE_PRINT_TIME_KEYS), digits=0)
    return _round_profile_number(payload, digits=0)


def normalize_profile_details(inst: dict, plates: list[dict], existing_inst: Optional[dict] = None) -> dict[str, Any]:
    existing_inst = existing_inst if isinstance(existing_inst, dict) else {}
    existing_details = existing_inst.get("profileDetails") if isinstance(existing_inst.get("profileDetails"), dict) else {}
    need_ams_flag = _truthy_flag(inst.get("needAms"))
    raw_filaments = _collect_raw_filament_items(inst, plates)
    filaments = [
        normalized
        for raw in raw_filaments
        if (normalized := _normalize_filament_item(raw, default_ams=bool(need_ams_flag)))
    ]
    if not filaments and isinstance(existing_inst.get("filaments"), list):
        filaments = [dict(item) for item in existing_inst.get("filaments") or [] if isinstance(item, dict)]
    if not filaments and isinstance(existing_details.get("filaments"), list):
        filaments = [dict(item) for item in existing_details.get("filaments") or [] if isinstance(item, dict)]
    filaments = _merge_profile_filaments(filaments)

    filament_weight = (
        _round_profile_number(inst.get("filamentWeight") or inst.get("filamentWeightG") or inst.get("materialWeight") or inst.get("weight"), digits=1)
        or _round_profile_number((inst.get("prediction") or {}).get("weight") if isinstance(inst.get("prediction"), dict) else None, digits=1)
        or _round_profile_number(existing_inst.get("filamentWeight"), digits=1)
        or _round_profile_number(existing_details.get("filamentWeight"), digits=1)
        or _plate_total_filament_weight(plates)
    )
    if filament_weight is None and filaments:
        total_weight = sum(float(item.get("weight") or 0) for item in filaments)
        filament_weight = _round_profile_number(total_weight, digits=1) if total_weight > 0 else None

    nozzle_diameter = (
        _round_profile_number(
            _first_value_by_keys(
                inst,
                (
                    "nozzleDiameter",
                    "nozzle_diameter",
                    "nozzleDiameterMm",
                    "nozzleDiameterMM",
                    "nozzleSize",
                    "nozzle",
                ),
            ),
            digits=2,
        )
        or _round_profile_number(existing_inst.get("nozzleDiameter"), digits=2)
        or _round_profile_number(existing_details.get("nozzleDiameter"), digits=2)
    )
    if nozzle_diameter is not None and nozzle_diameter > 5:
        nozzle_diameter = None

    plate_count = (
        _round_profile_number(inst.get("plateCount") or inst.get("plateNum"), digits=0)
        or len(plates or [])
        or _round_profile_number(existing_inst.get("plateCount") or existing_inst.get("plateNum"), digits=0)
        or _round_profile_number(existing_details.get("plateCount"), digits=0)
        or 0
    )
    print_time_seconds = (
        _round_profile_number(_first_value_by_keys(inst, _PROFILE_PRINT_TIME_KEYS), digits=0)
        or _prediction_time_seconds(inst.get("prediction"))
        or _round_profile_number(_first_value_by_keys(existing_inst, _PROFILE_PRINT_TIME_KEYS), digits=0)
        or _prediction_time_seconds(existing_inst.get("prediction"))
        or _round_profile_number(_first_value_by_keys(existing_details, _PROFILE_PRINT_TIME_KEYS), digits=0)
        or _prediction_time_seconds(existing_details.get("prediction"))
        or 0
    )
    need_ams = bool(
        need_ams_flag
        if need_ams_flag is not None
        else existing_inst.get("needAms") or existing_details.get("needAms") or any(item.get("ams") for item in filaments)
    )

    return {
        "schemaVersion": PROFILE_DETAIL_SCHEMA_VERSION,
        "plateCount": int(plate_count or 0),
        "printTimeSeconds": int(print_time_seconds or 0),
        "nozzleDiameter": nozzle_diameter,
        "filamentWeight": filament_weight,
        "needAms": need_ams,
        "filaments": filaments,
    }


def build_meta(
    design: dict,
    summary: dict,
    design_images: List[dict],
    cover_meta: Optional[dict],
    instances: List[dict],
    author: dict,
    base_name: str,
    attachments: Optional[List[dict]] = None,
    comments_bundle: Optional[dict] = None,
):
    collect_ts = int(china_now().timestamp())
    update_time = china_now_iso()
    counts = design.get("counts") or {}
    comments_bundle = comments_bundle if isinstance(comments_bundle, dict) else {}
    comment_items = comments_bundle.get("items") if isinstance(comments_bundle.get("items"), list) else []
    comment_count = _comment_numeric(comments_bundle.get("count"))
    stats = {
        "likes": design.get("likeCount") or counts.get("likes") or 0,
        "favorites": design.get("collectionCount") or design.get("favoriteCount") or design.get("favCount") or counts.get("favorites") or 0,
        "downloads": design.get("downloadCount") or counts.get("downloads") or 0,
        "prints": design.get("printCount") or counts.get("prints") or 0,
        "views": design.get("readCount") or counts.get("views") or 0,
        "comments": comment_count or len(comment_items),
    }
    images_design_list = [str(d.get("fileName") or "") for d in design_images if str(d.get("fileName") or "").strip()]
    summary_image_list = [
        str(i.get("fileName") or "")
        for i in summary.get("summaryImages", [])
        if isinstance(i, dict) and str(i.get("fileName") or "").strip()
    ]
    cover_local = str(cover_meta.get("fileName") or "") if cover_meta else ""
    cover_url = (
        design.get("coverUrl")
        or design.get("coverImage")
        or design.get("thumbnail")
        or design.get("thumbnailUrl")
        or (cover_meta.get("originalUrl") if cover_meta else "")
    )
    author_avatar_local = author.get("avatarLocal") or ""
    author_rel = f"images/{author_avatar_local}" if author_avatar_local else ""
    source_url = str(design.get("url") or "").strip().lower()
    if "makerworld.com/" in source_url and "makerworld.com.cn" not in source_url:
        source_value = "mw_global"
    else:
        source_value = "mw_cn"

    return {
        "baseName": base_name,
        "source": source_value,
        "url": design.get("url") or "",
        "id": design.get("id"),
        "slug": design.get("slug") or "",
        "title": design.get("title") or "",
        "titleTranslated": design.get("titleTranslated") or "",
        "license": design.get("license") or "",
        "threeMfDownloadAllowed": not is_three_mf_download_prohibited(design),
        "coverUrl": cover_url,
        "tags": design.get("tags") or [],
        "tagsOriginal": design.get("tagsOriginal") or [],
        "stats": stats,
        "cover": {
            "url": cover_url if cover_meta is None else str(cover_meta.get("originalUrl") or ""),
            "localName": cover_local,
            "relPath": str(cover_meta.get("relPath") or "") if cover_meta else "",
        },
        "author": {
            "name": author.get("name") or "",
            "url": author.get("url") or "",
            "avatarUrl": author.get("avatarUrl") or "",
            "avatarLocal": author_avatar_local,
            "avatarRelPath": author_rel,
        },
        "images": {
            "cover": cover_local,
            "design": images_design_list,
            "summary": summary_image_list,
        },
        "designImages": design_images,
        "summaryImages": summary.get("summaryImages", []),
        "summary": {
            "raw": summary.get("raw", ""),
            "html": summary.get("html", ""),
            "text": summary.get("text", ""),
        },
        "commentSchemaVersion": COMMENT_SCHEMA_VERSION,
        "commentCount": comment_count or len(comment_items),
        "comments": comment_items,
        "instances": instances,
        "attachments": attachments or [],
        "collectDate": collect_ts,
        "offlineFiles": {
            "attachments": [str(item.get("localName") or "") for item in (attachments or []) if str(item.get("localName") or "").strip()],
            "printed": [],
        },
        "update_time": update_time,
        "generatedAt": Path().absolute().as_posix(),
        "note": "本文件包含结构化数据与打印配置详情。",
    }


# ============ 本地归档与页面生成（集成 5.0.py 逻辑） ============
REBUILD_SESSION = requests.Session()
REBUILD_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (MW-Fetcher-Rebuild)"
})


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def _list_directory_entries(root: Path) -> list[Path]:
    try:
        return sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        return []


def possible_prefixes(base_name: str):
    prefixes = {base_name}
    if base_name.endswith("_"):
        prefixes.add(base_name.rstrip("_"))
    else:
        prefixes.add(base_name + "_")
    return prefixes


def iter_patterns(root: Path, base_name: str, middles):
    for prefix in possible_prefixes(base_name):
        for mid in middles:
            yield from root.glob(prefix + mid)


def _iter_matching_entries(entries: list[Path], pattern: str):
    for entry in entries:
        if fnmatchcase(entry.name, pattern):
            yield entry


def glob_with_prefix_or_plain(root: Path, base_name: str, middles, entries: Optional[list[Path]] = None):
    seen = set()
    matcher = _iter_matching_entries if entries is not None else None

    # 先匹配无前缀
    for mid in middles:
        candidates = matcher(entries, mid) if matcher else root.glob(mid)
        for p in candidates:
            if p in seen:
                continue
            seen.add(p)
            yield p
    # 再匹配带前缀
    if matcher:
        for prefix in possible_prefixes(base_name):
            for mid in middles:
                for p in matcher(entries, prefix + mid):
                    if p in seen:
                        continue
                    seen.add(p)
                    yield p
    else:
        for p in iter_patterns(root, base_name, middles):
            if p in seen:
                continue
            seen.add(p)
            yield p


def strip_prefix(name: str, base_name: str) -> str:
    for prefix in sorted(possible_prefixes(base_name), key=len, reverse=True):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def move_or_replace(src: Path, dst: Path, label: str):
    if not src.exists():
        return False
    try:
        if src.resolve() == dst.resolve():
            return True
    except Exception:
        pass
    ensure_dir(dst.parent)
    if dst.exists():
        if dst.is_dir():
            log("跳过移动，目标是目录:", src, "->", dst)
            return False
        log(f"覆盖 {label}:", src, "->", dst)
        dst.unlink()
    else:
        log(f"移动 {label}:", src, "->", dst)
    shutil.move(str(src), str(dst))
    return True


def choose_archive_base_name(
    design_id: int,
    title: str,
    existing_root: Optional[Path] = None,
    existing_model_dir: str = "",
) -> tuple[str, str]:
    desired = f"MW_{design_id}_{sanitize_filename(title or 'model')}"
    if existing_root is None:
        return desired, "created"
    try:
        root = existing_root.resolve()
    except Exception:
        root = existing_root

    clean_existing_model_dir = str(existing_model_dir or "").strip().strip("/")
    if clean_existing_model_dir:
        existing_dir = (root / clean_existing_model_dir).resolve()
        try:
            existing_dir.relative_to(root)
        except ValueError:
            existing_dir = root / clean_existing_model_dir
        if existing_dir.exists() and existing_dir.is_dir():
            try:
                return existing_dir.relative_to(root).as_posix(), "updated"
            except ValueError:
                return clean_existing_model_dir, "updated"

    exact_dir = root / desired
    if exact_dir.exists() and exact_dir.is_dir():
        return desired, "updated"

    candidates = [p for p in root.glob(f"MW_{design_id}_*") if p.is_dir()]
    if candidates:
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[0].name, "updated"
    return desired, "created"


def load_existing_meta(work_dir: Path) -> dict:
    meta_path = work_dir / "meta.json"
    if not meta_path.exists() or not meta_path.is_file():
        return {}
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


STYLE_CSS = """
body {
  font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
  margin: 0;
  padding: 0;
  background: #f5f5f5;
  color: #222;
}

.container {
  max-width: 980px;
  margin: 24px auto 40px;
  padding: 24px;
  background: #ffffff;
  box-shadow: 0 0 12px rgba(0,0,0,0.06);
  border-radius: 10px;
}

h1.title {
  font-size: 26px;
  margin: 0 0 8px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.title a.origin-link {
  font-size: 14px;
  text-decoration: none;
  color: #1976d2;
}

.title a.origin-link::before {
  content: "↗ ";
}

.author {
  margin: 4px 0 14px;
  font-size: 14px;
  color: #555;
  display: flex;
  align-items: center;
  gap: 10px;
}

.author img.avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  object-fit: cover;
}

.hero {
  width: 100%;
  max-height: 540px;
  object-fit: contain;
  border-radius: 8px;
  margin-bottom: 12px;
  background: #000;
}

.collect-date {
  font-size: 13px;
  color: #777;
  margin: 0 0 16px;
}

.section-title {
  font-size: 18px;
  margin: 22px 0 10px;
  border-left: 4px solid #1976d2;
  padding-left: 10px;
}

.stats {
  margin: 6px 0 14px;
  color: #666;
  font-size: 14px;
}

.tag-list span {
  display: inline-block;
  background: #e3f2fd;
  padding: 4px 10px;
  margin: 4px 6px 0 0;
  border-radius: 14px;
  font-size: 13px;
}

.summary img {
  max-width: 100%;
  border-radius: 6px;
  margin: 6px 0;
}

.attachments {
  margin-bottom: 10px;
}

.printed {
  margin-bottom: 10px;
}

.printed-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 10px;
}

.printed-item {
  width: 160px;
}

.printed-item img {
  width: 100%;
  height: 120px;
  object-fit: cover;
  border-radius: 6px;
  border: 1px solid #eee;
  background: #000;
  cursor: zoom-in;
}

.printed-caption {
  font-size: 12px;
  color: #555;
  margin-top: 4px;
  word-break: break-all;
}

.printed-empty {
  color: #888;
  font-size: 13px;
  margin-top: 6px;
}

.attach-upload {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.attach-upload input[type="file"] {
  font-size: 13px;
}

.attach-btn {
  background: #1976d2;
  color: #fff;
  border: none;
  border-radius: 6px;
  padding: 6px 12px;
  cursor: pointer;
  font-size: 13px;
}

.attach-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.attach-msg {
  font-size: 12px;
  color: #666;
}

.attach-msg.error {
  color: #b00020;
}

.attach-list {
  list-style: none;
  padding-left: 0;
  margin: 10px 0 0;
}

.attach-list li {
  margin: 4px 0;
  font-size: 13px;
}

.attach-list a {
  color: #1976d2;
  text-decoration: none;
}

.attach-list a:hover {
  text-decoration: underline;
}

.attach-empty {
  color: #888;
}

.instances .inst-card {
  border: 1px solid #e6e6e6;
  padding: 12px;
  border-radius: 10px;
  margin-bottom: 12px;
  transition: box-shadow 0.2s ease, transform 0.2s ease;
}

.instances .inst-card:hover {
  box-shadow: 0 6px 18px rgba(0,0,0,0.08);
  transform: translateY(-2px);
}

.inst-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  font-size: 13px;
  color: #555;
}

.meta-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 12px;
  background: #f7f7f7;
  border: 1px solid #eee;
}

.meta-item:hover {
  background: #eef5ff;
  border-color: #d0e0ff;
}

.meta-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  margin-left: 6px;
  border-radius: 12px;
  background: #e8f5e9;
  color: #1b5e20;
  font-size: 12px;
  border: 1px solid #c8e6c9;
}

.inst-download {
  margin-left: 6px;
  font-size: 12px;
  text-decoration: none;
  background: #1976d2;
  color: #fff;
  padding: 2px 8px;
  border-radius: 10px;
}

.inst-download:hover {
  background: #0f5fb6;
}

.inst-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-left: 6px;
  font-size: 12px;
  text-decoration: none;
  background: #1976d2;
  color: #fff;
  padding: 4px 10px;
  border-radius: 12px;
  border: none;
  white-space: nowrap;
  font-weight: bold;
  font-family: inherit;
  cursor: pointer;
}

.inst-btn.alt {
  background: #e53935;
}

.inst-btn.alt:hover {
  background: #c62828;
}

.inst-btn:hover {
  opacity: 0.9;
}

.inst-thumb {
  width: 140px;
  height: 140px;
  object-fit: cover;
  border-radius: 8px;
  border: 1px solid #eee;
  background: #000;
  cursor: zoom-in;
}

.plates {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 10px;
}

.plate-item {
  width: 120px;
  border: 1px solid #eee;
  border-radius: 8px;
  padding: 6px;
  font-size: 12px;
}

.plate-item img {
  width: 100%;
  height: 70px;
  object-fit: contain;
  border-radius: 6px;
  background: #000;
  cursor: zoom-in;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}

.chip {
  display: inline-block;
  padding: 2px 8px 2px 6px;
  border-radius: 12px;
  font-size: 12px;
  background: #f0f0f0;
  border: 1px solid #e8e8e8;
}

.chip .color-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  margin-right: 6px;
  border: 1px solid #ccc;
  vertical-align: middle;
}

.thumbs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 6px 0 12px;
}

.thumbs img {
  width: 82px;
  height: 82px;
  object-fit: cover;
  border-radius: 6px;
  border: 2px solid transparent;
}

.thumbs img.active {
  border-color: #1976d2;
  box-shadow: 0 0 6px rgba(25, 118, 210, 0.6);
}

.zoomable {
  cursor: zoom-in;
}

.lightbox {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.8);
  display: none;
  align-items: center;
  justify-content: center;
  z-index: 999999;
}

.lightbox img {
  max-width: 90vw;
  max-height: 90vh;
  border-radius: 10px;
  box-shadow: 0 12px 32px rgba(0,0,0,0.4);
}

.lightbox.show {
  display: flex;
}
.carousel {
  position: relative;
  margin: 10px 0 20px;
  overflow: hidden;
  border-radius: 8px;
  background: #000;
}

.carousel-track {
  display: flex;
  transition: transform 0.3s ease;
}

.carousel img {
  width: 100%;
  max-height: 480px;
  object-fit: contain;
  flex-shrink: 0;
  background: #000;
}

.carousel-btn {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  width: 32px;
  height: 32px;
  border-radius: 16px;
  border: none;
  background: rgba(0,0,0,0.45);
  color: #fff;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}

.carousel-btn:hover {
  background: rgba(0,0,0,0.7);
}

.carousel-btn.prev {
  left: 10px;
}

.carousel-btn.next {
  right: 10px;
}
""".strip()


def normalize_stats(meta: dict) -> dict:
    stats = meta.get("stats") or meta.get("counts") or {}
    likes = stats.get("likes") or stats.get("like") or 0
    favorites = stats.get("favorites") or stats.get("favorite") or 0
    downloads = stats.get("downloads") or stats.get("download") or 0
    prints = stats.get("prints") or stats.get("print") or 0
    views = stats.get("views") or stats.get("read") or stats.get("reads") or 0
    return {
        "likes": likes,
        "favorites": favorites,
        "downloads": downloads,
        "prints": prints,
        "views": views,
    }


def normalize_author(meta: dict) -> dict:
    author_raw = meta.get("author")
    if isinstance(author_raw, str):
        return {"name": author_raw, "url": "", "avatar": None}
    if not isinstance(author_raw, dict):
        return {"name": "", "url": "", "avatar": None}

    avatar_local = author_raw.get("avatarLocal") or author_raw.get("avatar_local")
    avatar_rel = author_raw.get("avatarRelPath") or author_raw.get("avatar_local_path")
    if not avatar_rel and avatar_local:
        avatar_rel = f"images/{avatar_local}"

    return {
        "name": author_raw.get("name") or "",
        "url": author_raw.get("url") or "",
        "avatar": avatar_rel,
    }


def normalize_images(meta: dict) -> dict:
    images_raw = meta.get("images")
    design = []
    summary = []
    cover = None

    def to_name(item):
        if not item:
            return None
        return Path(item).name

    if isinstance(images_raw, dict):
        design = [to_name(x) for x in images_raw.get("design", []) if to_name(x)]
        summary = [to_name(x) for x in images_raw.get("summary", []) if to_name(x)]
        cover = to_name(images_raw.get("cover"))
    elif isinstance(images_raw, list):
        design = [to_name(x) for x in images_raw if to_name(x)]

    if not design and meta.get("designImages"):
        for item in meta.get("designImages", []):
            if isinstance(item, dict):
                val = item.get("fileName") or item.get("localName") or item.get("relPath")
                name = to_name(val)
                if name:
                    design.append(name)

    if not summary and meta.get("summaryImages"):
        for item in meta.get("summaryImages", []):
            if isinstance(item, dict):
                val = item.get("fileName") or item.get("relPath")
                name = to_name(val)
                if name:
                    summary.append(name)
            elif isinstance(item, str):
                name = to_name(item)
                if name:
                    summary.append(name)

    if not cover:
        cover_info = meta.get("cover") or {}
        cover = to_name(cover_info.get("relPath") or cover_info.get("localName"))

    return {"design": design, "summary": summary, "cover": cover}


def format_duration(seconds):
    try:
        sec = int(seconds)
    except Exception:
        return ""
    hours = sec / 3600.0
    if hours >= 1:
        return f"{hours:.1f} h"
    mins = sec / 60.0
    return f"{mins:.1f} min"


def format_date(date_str):
    try:
        if not date_str:
            return ""
        parsed = parse_datetime(date_str)
        if parsed is None:
            return str(date_str or "")
        return str(parsed.date())
    except Exception:
        return date_str or ""


def _escape_json_for_inline_script(json_text: str) -> str:
    """
    将 JSON 文本安全注入 <script>，避免 </script>（任意大小写变体）截断脚本。
    """
    if not json_text:
        return "{}"
    return (
        json_text
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )



def build_fallback_index_html(meta: dict, assets: dict = None) -> str:
    title = str(meta.get("title") or meta.get("name") or "MakerHub Archive")
    author = normalize_author(meta)
    stats = normalize_stats(meta)
    images = normalize_images(meta)
    summary_payload = meta.get("summary") if isinstance(meta.get("summary"), dict) else {}
    summary_text = str(summary_payload.get("text") or summary_payload.get("raw") or meta.get("description") or "").strip()
    hero_name = images.get("cover") or (images.get("design") or [None])[0] or (images.get("summary") or [None])[0]
    hero_html = (
        f'<img src="./images/{escape(hero_name)}" alt="{escape(title)}" style="width:100%;border-radius:16px;object-fit:cover;background:#f3f4f6;" />'
        if hero_name
        else '<div style="height:260px;border-radius:16px;background:#f3f4f6;"></div>'
    )
    gallery_files = list(dict.fromkeys((images.get("design") or []) + (images.get("summary") or [])))
    gallery_html = "".join(
        f'<img src="./images/{escape(name)}" alt="{escape(title)}" style="width:100%;border-radius:14px;background:#f3f4f6;" />'
        for name in gallery_files
    ) or '<p style="color:#6b7280;">当前没有离线图片。</p>'
    instances = meta.get("instances") if isinstance(meta.get("instances"), list) else []
    instances_html = "".join(
        f'<li><a href="./instances/{escape(str(item.get("fileName") or ""))}">{escape(str(item.get("title") or item.get("name") or item.get("fileName") or "实例文件"))}</a></li>'
        for item in instances
        if str(item.get("fileName") or "").strip()
    ) or '<li>当前没有可用 3MF 文件。</li>'
    attachments = meta.get("attachments") if isinstance(meta.get("attachments"), list) else []
    attachments_html = "".join(
        f'<li><a href="./file/{escape(str(item.get("localName") or ""))}">{escape(str(item.get("name") or item.get("localName") or "附件"))}</a></li>'
        for item in attachments
        if str(item.get("localName") or "").strip()
    )
    attachments_section = (
        f"""
        <section>
          <h2>附件</h2>
          <ul>{attachments_html}</ul>
        </section>
        """
        if attachments_html
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escape(title)} - makerhub archive</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f7fb; color: #111827; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 40px 24px 64px; }}
    .hero {{ display: grid; gap: 24px; grid-template-columns: minmax(0, 1.2fr) minmax(320px, 420px); align-items: start; }}
    .panel {{ background: #fff; border-radius: 20px; padding: 24px; box-shadow: 0 20px 60px rgba(15, 23, 42, 0.08); }}
    .meta {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 20px; }}
    .meta div {{ border-radius: 14px; background: #f8fafc; padding: 14px 16px; }}
    .gallery {{ display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); margin-top: 24px; }}
    h1, h2 {{ margin: 0 0 12px; }}
    p {{ line-height: 1.7; }}
    ul {{ margin: 0; padding-left: 18px; line-height: 1.8; }}
    a {{ color: #2563eb; text-decoration: none; }}
    @media (max-width: 900px) {{ .hero {{ grid-template-columns: 1fr; }} .meta {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <div class="panel">{hero_html}</div>
      <div class="panel">
        <p style="margin:0 0 8px;color:#2563eb;font-weight:600;">makerhub 离线归档</p>
        <h1>{escape(title)}</h1>
        <p style="margin:0;color:#6b7280;">作者：{escape(author.get("name") or "未知作者")}</p>
        <div class="meta">
          <div><strong>下载</strong><br />{escape(str(stats.get("downloads") or 0))}</div>
          <div><strong>点赞</strong><br />{escape(str(stats.get("likes") or 0))}</div>
          <div><strong>打印</strong><br />{escape(str(stats.get("prints") or 0))}</div>
          <div><strong>收藏</strong><br />{escape(str(stats.get("favorites") or 0))}</div>
        </div>
        <p style="margin-top:20px;">{escape(summary_text or "当前没有离线简介。")}</p>
      </div>
    </section>
    <section class="panel" style="margin-top:24px;">
      <h2>图片</h2>
      <div class="gallery">{gallery_html}</div>
    </section>
    <section class="panel" style="margin-top:24px;">
      <h2>3MF 文件</h2>
      <ul>{instances_html}</ul>
    </section>
    {attachments_section}
  </main>
</body>
</html>
"""


def _load_offline_template_bundle() -> tuple[Optional[dict[str, str]], list[Path]]:
    app_dir = Path(__file__).parent
    template_path = app_dir / "templates" / "model.html"
    variables_css_path = app_dir / "static" / "css" / "variables.css"
    components_css_path = app_dir / "static" / "css" / "components.css"
    model_css_path = app_dir / "static" / "css" / "model.css"
    model_js_path = app_dir / "static" / "js" / "model.js"
    required_paths = [
        template_path,
        variables_css_path,
        components_css_path,
        model_css_path,
        model_js_path,
    ]
    missing_paths = [path for path in required_paths if not path.exists()]
    if missing_paths:
        return None, missing_paths

    try:
        signature_parts = []
        for path in required_paths:
            stat = path.stat()
            signature_parts.append((path.as_posix(), stat.st_mtime_ns, stat.st_size))
        signature = tuple(signature_parts)
    except OSError:
        return None, []

    with _OFFLINE_TEMPLATE_CACHE_LOCK:
        cached_bundle = _OFFLINE_TEMPLATE_CACHE.get("bundle")
        if _OFFLINE_TEMPLATE_CACHE.get("signature") == signature and isinstance(cached_bundle, dict):
            return cached_bundle, []

    try:
        html = template_path.read_text(encoding="utf-8")
        variables_css = variables_css_path.read_text(encoding="utf-8")
        components_css = components_css_path.read_text(encoding="utf-8")
        model_css = _OFFLINE_MODEL_CSS_IMPORT_RE.sub("", model_css_path.read_text(encoding="utf-8"))
        model_js = model_js_path.read_text(encoding="utf-8")
    except OSError:
        return None, []

    html = _OFFLINE_ICON_LINK_RE.sub("", html)
    html = _OFFLINE_FONT_AWESOME_RE.sub("", html)

    html, var_count = _OFFLINE_VARIABLES_LINK_RE.subn(_OFFLINE_TEMPLATE_VARS_TOKEN, html, count=1)
    if var_count == 0:
        html, head_count = _OFFLINE_HEAD_CLOSE_RE.subn(
            f"{_OFFLINE_TEMPLATE_VARS_TOKEN}\n</head>",
            html,
            count=1,
        )
        if head_count == 0:
            html = f"{_OFFLINE_TEMPLATE_VARS_TOKEN}\n{html}"

    html, model_count = _OFFLINE_MODEL_LINK_RE.subn(_OFFLINE_TEMPLATE_MODEL_TOKEN, html, count=1)
    if model_count == 0:
        html, head_count = _OFFLINE_HEAD_CLOSE_RE.subn(
            f"{_OFFLINE_TEMPLATE_MODEL_TOKEN}\n</head>",
            html,
            count=1,
        )
        if head_count == 0:
            html = f"{_OFFLINE_TEMPLATE_MODEL_TOKEN}\n{html}"

    html, script_count = _OFFLINE_MODEL_SCRIPT_RE.subn(_OFFLINE_TEMPLATE_SCRIPT_TOKEN, html, count=1)
    if script_count == 0:
        html, body_count = _OFFLINE_BODY_CLOSE_RE.subn(
            f"{_OFFLINE_TEMPLATE_SCRIPT_TOKEN}\n</body>",
            html,
            count=1,
        )
        if body_count == 0:
            html += f"\n{_OFFLINE_TEMPLATE_SCRIPT_TOKEN}"

    bundle = {
        "html_template": html,
        "variables_inline": f"<style>\n{variables_css}\n</style>",
        "model_inline": f"<style>\n{components_css}\n{model_css}\n</style>",
        "model_js": model_js,
    }
    with _OFFLINE_TEMPLATE_CACHE_LOCK:
        _OFFLINE_TEMPLATE_CACHE["signature"] = signature
        _OFFLINE_TEMPLATE_CACHE["bundle"] = bundle
    return bundle, []


def build_index_html(meta: dict, assets: dict = None, logger=None) -> str:
    """基于 CSR 架构的离线 HTML 生成器，读取 model.html 骨架并注入数据和源码。"""
    template_bundle, missing_paths = _load_offline_template_bundle()
    if template_bundle is None:
        unavailable_signature = tuple(path.as_posix() for path in missing_paths) if missing_paths else ("read_error",)
        should_log = False
        with _OFFLINE_TEMPLATE_CACHE_LOCK:
            if _OFFLINE_TEMPLATE_CACHE.get("unavailable_signature") != unavailable_signature:
                _OFFLINE_TEMPLATE_CACHE["unavailable_signature"] = unavailable_signature
                should_log = True
        if should_log:
            if missing_paths:
                log(logger, "离线模板资源不存在，当前使用内置回退 HTML：", ", ".join(str(path) for path in missing_paths))
            else:
                log(logger, "离线模板读取失败，当前使用内置回退 HTML。")
        return build_fallback_index_html(meta, assets)

    with _OFFLINE_TEMPLATE_CACHE_LOCK:
        _OFFLINE_TEMPLATE_CACHE["unavailable_signature"] = None

    html = str(template_bundle.get("html_template") or "")
    html = html.replace(
        _OFFLINE_TEMPLATE_VARS_TOKEN,
        str(template_bundle.get("variables_inline") or ""),
        1,
    )
    html = html.replace(
        _OFFLINE_TEMPLATE_MODEL_TOKEN,
        str(template_bundle.get("model_inline") or ""),
        1,
    )

    # 注入离线 Meta 和 JS (使用 JSON 安全的直接注入)
    meta_json_str = json.dumps(meta, ensure_ascii=False)
    meta_json_str = _escape_json_for_inline_script(meta_json_str)
    injection_script = f"\n<script>\nwindow.__OFFLINE_META__ = {meta_json_str};\n</script>\n"
    js_replacement = f"{injection_script}<script>\n{template_bundle.get('model_js') or ''}\n</script>"
    if _OFFLINE_TEMPLATE_SCRIPT_TOKEN in html:
        html = html.replace(_OFFLINE_TEMPLATE_SCRIPT_TOKEN, js_replacement, 1)
    else:
        html += js_replacement

    return html


def rebuild_once(meta_path: Path, progress_callback=None, logger=None, build_offline_page: bool = False):
    rebuild_started_at = time.perf_counter()
    with meta_path.open("r", encoding="utf-8") as f:
        meta = json.load(f)

    base_name = meta.get("baseName") or meta_path.stem.replace("_meta", "")
    if meta_path.name == "meta.json" and meta_path.parent.name == base_name:
        work_dir = meta_path.parent
    else:
        work_dir = meta_path.parent / base_name
    ensure_dir(work_dir)

    emit_progress(progress_callback, 84, f"正在整理归档目录：{base_name}")
    log(logger, "归档目录整理:", base_name)

    # 1. 写/移动 meta.json 到目标目录，仅保留目标目录一份
    target_meta = work_dir / "meta.json"
    if not target_meta.exists():
        shutil.move(str(meta_path), str(target_meta))
    else:
        if meta_path.resolve() != target_meta.resolve() and meta_path.exists():
            try:
                meta_path.unlink()
            except Exception:
                pass

    # 2. 准备子目录
    images_dir = work_dir / "images"
    instances_dir = work_dir / "instances"
    files_dir = work_dir / "file"
    ensure_dir(images_dir)
    ensure_dir(instances_dir)
    ensure_dir(files_dir)
    source_root = meta_path.parent
    source_entries = _list_directory_entries(source_root)

    # 3. 移动 screenshot
    screenshot_file = None
    for p in glob_with_prefix_or_plain(source_root, base_name, ["_screenshot.*", "screenshot.*"], entries=source_entries):
        dst = work_dir / f"screenshot{p.suffix.lower()}"
        if move_or_replace(p, dst, "screenshot"):
            screenshot_file = dst
            break
    if not screenshot_file:
        existing = next(iter(work_dir.glob("screenshot.*")), None)
        if existing:
            screenshot_file = existing

    # 4. 封面图 & 作者头像 & design & summary images
    relocate_assets_started_at = time.perf_counter()
    for p in glob_with_prefix_or_plain(source_root, base_name, ["_cover.*", "cover.*"], entries=source_entries):
        dst = images_dir / f"cover{p.suffix.lower()}"
        move_or_replace(p, dst, "cover")
        break

    for p in glob_with_prefix_or_plain(source_root, base_name, ["_author_avatar.*", "author_avatar.*"], entries=source_entries):
        dst = images_dir / f"author_avatar{p.suffix.lower()}"
        move_or_replace(p, dst, "author_avatar")
        break

    for p in glob_with_prefix_or_plain(source_root, base_name, ["_design_*", "design_*"], entries=source_entries):
        new_name = strip_prefix(p.name, base_name)
        dst = images_dir / new_name
        move_or_replace(p, dst, "design 图片")

    for p in glob_with_prefix_or_plain(source_root, base_name, ["_summary_img_*", "summary_img_*"], entries=source_entries):
        new_name = strip_prefix(p.name, base_name)
        dst = images_dir / new_name
        move_or_replace(p, dst, "summary 图片")

    for p in glob_with_prefix_or_plain(source_root, base_name, ["_comment_*", "comment_*"], entries=source_entries):
        new_name = strip_prefix(p.name, base_name)
        dst = images_dir / new_name
        move_or_replace(p, dst, "comment 资源")

    # 5. 实例配图/plate 缩略图
    for p in glob_with_prefix_or_plain(source_root, base_name, ["_inst*_*"], entries=source_entries):
        new_name = p.name
        dst = images_dir / new_name
        move_or_replace(p, dst, "实例图片")
    _log_perf("rebuild.relocate_assets", relocate_assets_started_at, logger=logger)

    # 6. 下载 3MF 到 instances 目录
    instances = meta.get("instances", []) or []
    inst_files = []
    meta_changed = False
    new_instance_downloads = 0
    existing_instance_downloads = 0
    total_instance_steps = max(len(instances), 1)
    instance_download_started_at = time.perf_counter()
    existing_instance_files = {
        path.name
        for path in _list_directory_entries(instances_dir)
        if path.is_file()
    }
    reserved_instance_names: set[str] = set()
    for inst in instances:
        url = inst.get("downloadUrl")
        if not url:
            continue
        fn = choose_unique_instance_filename(
            inst,
            instances,
            instances_dir,
            inst.get("name") or "",
            reserved_names=reserved_instance_names,
            existing_files=existing_instance_files,
        )
        reserved_instance_names.add(fn)
        if str(inst.get("fileName") or "").strip() != fn:
            inst["fileName"] = fn
            meta_changed = True
        dest = instances_dir / fn

        if dest.exists():
            log("存在，跳过：", dest)
            existing_instance_downloads += 1
        else:
            with resource_slot("three_mf_download", detail=dest.name):
                _wait_before_three_mf_download(f"下载文件 {dest.name}", logger=logger)
                try:
                    download_file(
                        REBUILD_SESSION,
                        url,
                        dest,
                        max_duration=BINARY_TRANSFER_TIMEOUT_SECONDS,
                    )
                    new_instance_downloads += 1
                except Exception as exc:
                    _mark_instance_3mf_download_failed(inst, exc, logger=logger)
                    meta_changed = True
                    continue
        if inst.get("downloadState") or inst.get("downloadMessage"):
            inst["downloadState"] = ""
            inst["downloadMessage"] = ""
            meta_changed = True
        inst_files.append({
            "id": inst.get("id"),
            "title": inst.get("title") or inst.get("name") or str(inst.get("id")),
            "file": dest.name,
        })
        processed_instances = len(inst_files)
        emit_progress(
            progress_callback,
            84 + min(int(processed_instances * 10 / total_instance_steps), 10),
            f"{'正在写入 3MF 占位文件' if fake_three_mf_downloads_enabled() else '正在下载实例文件'}（{processed_instances}/{len(instances)}）",
            {"current": processed_instances, "total": len(instances)},
        )
    _log_perf(
        "rebuild.download_instances",
        instance_download_started_at,
        logger=logger,
        total=len(instances),
        downloaded=len(inst_files),
        downloaded_new=new_instance_downloads,
        existing=existing_instance_downloads,
    )

    attachments = meta.get("attachments") or []
    attachment_files = []
    if isinstance(attachments, list):
        attachment_download_started_at = time.perf_counter()
        used_names = set()
        for idx, att in enumerate(attachments, start=1):
            if not isinstance(att, dict):
                continue
            url = str(att.get("url") or "").strip()
            if not url:
                continue
            preferred = str(att.get("localName") or att.get("name") or "").strip()
            if not preferred:
                preferred = _attachment_name_from_url(url, f"attachment_{idx}.{pick_ext_from_url(url, 'bin')}")
            local_name = _dedupe_attachment_local_name(preferred, used_names)
            if str(att.get("localName") or "") != local_name:
                att["localName"] = local_name
                meta_changed = True
            rel_path = f"file/{local_name}"
            if str(att.get("relPath") or "") != rel_path:
                att["relPath"] = rel_path
                meta_changed = True
            dest = files_dir / local_name
            download_file(
                REBUILD_SESSION,
                url,
                dest,
                max_duration=BINARY_TRANSFER_TIMEOUT_SECONDS,
            )
            attachment_files.append(local_name)
            emit_progress(
                progress_callback,
                95,
                f"正在下载附件（{len(attachment_files)}/{len(attachments)}）",
                {"current": len(attachment_files), "total": len(attachments)},
            )
        _log_perf(
            "rebuild.download_attachments",
            attachment_download_started_at,
            logger=logger,
            total=len(attachments),
            downloaded=len(attachment_files),
        )

    offline = meta.get("offlineFiles")
    if not isinstance(offline, dict):
        offline = {}
        meta["offlineFiles"] = offline
    if offline.get("attachments") != attachment_files:
        offline["attachments"] = attachment_files
        meta_changed = True

    if attachment_files:
        index_payload = {
            "files": attachment_files,
            "items": [{"name": name, "size": (files_dir / name).stat().st_size if (files_dir / name).exists() else 0} for name in attachment_files],
        }
        (files_dir / "_index.json").write_text(json.dumps(index_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # 若实例文件名或附件信息发生调整，写回 meta.json，保证元数据与磁盘一致
    if meta_changed:
        target_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    if build_offline_page:
        # Historical compatibility path. The current web detail page reads meta.json
        # through the API, so normal archives skip this unused offline HTML work.
        design_files = sorted([p.name for p in images_dir.glob("design_*")])
        cover_file = next(iter(images_dir.glob("cover.*")), None)
        avatar_file = next(iter(images_dir.glob("author_avatar.*")), None)

        hero_file = screenshot_file or cover_file or (images_dir / design_files[0] if design_files else None)
        hero_rel = hero_file.relative_to(work_dir).as_posix() if hero_file else "screenshot.png"

        assets = {
            "design_files": design_files,
            "hero": f"./{hero_rel}",
            "avatar": f"./{avatar_file.relative_to(work_dir).as_posix()}" if avatar_file else None,
            "collected_date": china_now().strftime("%Y-%m-%d"),
            "instance_files": inst_files,
            "base_name": base_name,
        }

        offline_page_started_at = time.perf_counter()
        index_html = build_index_html(meta, assets, logger=logger)
        (work_dir / "index.html").write_text(index_html, encoding="utf-8")
        _log_perf("rebuild.build_offline_page", offline_page_started_at, logger=logger)

    emit_progress(progress_callback, 98, "归档目录整理完成")
    log(logger, "完成归档:", work_dir)
    _log_perf("rebuild.total", rebuild_started_at, logger=logger, base_name=base_name)
    return {
        "three_mf_downloaded": new_instance_downloads,
        "three_mf_existing": existing_instance_downloads,
    }



if __name__ == "__main__":
    log("此模块用于被导入调用，不建议直接运行。")
    sys.exit(0)


def archive_model(*args, **kwargs):
    """Compatibility facade for the MakerWorld Pipeline archive entry point."""
    from app.services.makerworld_pipeline.archive import archive_model as pipeline_archive_model

    return pipeline_archive_model(*args, **kwargs)
