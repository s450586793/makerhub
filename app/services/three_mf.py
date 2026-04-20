import hashlib
import html
import json
import re
import threading
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse


_INSPECT_CACHE_LOCK = threading.RLock()
_INSPECT_CACHE: dict[str, dict[str, Any]] = {}
_THREE_MF_SUFFIX = ".3mf"
_HASH_CHUNK_SIZE_BYTES = 512 * 1024
_HASH_PAUSE_EVERY_BYTES = 4 * 1024 * 1024
_THREE_MF_FAILURE_PRIORITY = {
    "download_limited": 60,
    "verification_required": 50,
    "cloudflare": 45,
    "auth_required": 40,
    "http_error": 30,
    "not_found": 20,
    "missing": 10,
    "available": 0,
}
_THREE_MF_LEGACY_MESSAGES = {
    "download_limited": {
        "已达到 MakerWorld 每日下载上限，今日暂停自动重试。",
        "You've reached your daily download limit.",
    },
    "auth_required": {
        "下载 3MF 需要有效登录 Cookie，请检查 Cookie 是否过期。",
        "下载 3MF 需要有效登录态，请检查 Cookie / token 是否过期。",
        "Please log in to download models.",
    },
    "cloudflare": {
        "下载 3MF 时触发了 Cloudflare 校验，请更新 Cookie 或调整代理。",
    },
    "not_found": {
        "源端没有返回该打印配置的 3MF 下载地址。",
    },
    "missing": {
        "未获取到 3MF 下载地址。",
    },
}


def _normalize_loose_identity_text(value: Any) -> str:
    text = html.unescape(str(value or "")).strip().lower()
    if not text:
        return ""
    return re.sub(r"[\s\-_:/|\\.,，。;；'\"`~!！?？()\[\]{}<>《》【】（）、+]+", "", text)


def _unique_non_empty(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def normalize_makerworld_source(source: Any = "", url: Any = "") -> str:
    source_raw = str(source or "").strip().lower()
    if source_raw in {"mw_global", "global", "makerworld_global"}:
        return "global"
    if source_raw in {"mw_cn", "cn", "makerworld_cn"}:
        return "cn"

    host = urlparse(str(url or "")).netloc.lower()
    if not host:
        return ""
    if "makerworld.com.cn" in host or "api.bambulab.cn" in host:
        return "cn"
    if ("makerworld.com" in host and "makerworld.com.cn" not in host) or "api.bambulab.com" in host:
        return "global"
    return ""


def _default_three_mf_failure_message(state: str, source: str) -> str:
    if state == "download_limited":
        if source == "cn":
            return "国区返回了每日下载上限，今日暂停自动重试。"
        if source == "global":
            return "国际区返回了每日下载上限，今日暂停自动重试。"
        return "已达到 MakerWorld 每日下载上限，今日暂停自动重试。"
    if state == "verification_required":
        if source == "global":
            return "国际区下载 3MF 时触发站点验证；请先在浏览器完成验证，再更新 global Cookie / token，必要时补充 cf_clearance。"
        if source == "cn":
            return "国区下载 3MF 时触发站点验证；请先在浏览器完成验证，再更新国内站 Cookie / token。"
        return "下载 3MF 时触发站点验证；请更新 Cookie / token，必要时调整代理。"
    if state == "cloudflare":
        if source == "global":
            return "国际区下载 3MF 时触发站点验证或 Cloudflare 校验；请先在浏览器完成验证，再更新 global Cookie / token，必要时补充 cf_clearance。"
        if source == "cn":
            return "国区下载 3MF 时触发站点验证；请先在浏览器完成验证，再更新国内站 Cookie / token。"
        return "下载 3MF 时触发站点验证或 Cloudflare 校验；请更新 Cookie / token，必要时调整代理。"
    if state == "auth_required":
        if source == "global":
            return "国际区下载 3MF 需要有效登录态；如果最近出现验证页，请更新 global Cookie / token，必要时补充 cf_clearance。"
        if source == "cn":
            return "国区下载 3MF 需要有效登录态；请更新国内站 Cookie / token。"
        return "下载 3MF 需要有效登录态，请检查 Cookie / token 是否过期。"
    if state == "not_found":
        return "源端没有返回该打印配置的 3MF 下载地址。"
    if state == "http_error":
        return "下载 3MF 失败，请稍后重试。"
    if state == "missing":
        return "未获取到 3MF 下载地址。"
    return ""


def describe_three_mf_failure(
    state: Any,
    message: Any = "",
    *,
    source: Any = "",
    url: Any = "",
    limit_message: str = "",
) -> str:
    normalized_state = str(state or "").strip()
    normalized_source = normalize_makerworld_source(source=source, url=url)
    normalized_message = str(message or "").strip()
    normalized_limit = str(limit_message or "").strip()

    if normalized_state == "download_limited" and normalized_limit:
        return normalized_limit

    legacy_messages = _THREE_MF_LEGACY_MESSAGES.get(normalized_state, set())
    if normalized_message and normalized_message not in legacy_messages:
        return normalized_message

    default_message = _default_three_mf_failure_message(normalized_state, normalized_source)
    if default_message:
        return default_message
    if normalized_message:
        return normalized_message
    return "等待重新下载"


def three_mf_failure_priority(state: Any) -> int:
    return _THREE_MF_FAILURE_PRIORITY.get(str(state or "").strip(), 0)


def merge_three_mf_failure(current: Optional[dict[str, Any]], candidate: Optional[dict[str, Any]]) -> dict[str, Any]:
    current_item = dict(current or {})
    candidate_item = dict(candidate or {})
    if not candidate_item:
        return current_item
    if not current_item:
        return candidate_item

    current_score = three_mf_failure_priority(current_item.get("state"))
    candidate_score = three_mf_failure_priority(candidate_item.get("state"))
    if candidate_score > current_score:
        return candidate_item
    if candidate_score < current_score:
        return current_item

    current_message = str(current_item.get("message") or "").strip()
    candidate_message = str(candidate_item.get("message") or "").strip()
    if len(candidate_message) > len(current_message):
        return candidate_item
    return current_item


def parse_3mf_metadata(source_path: Path) -> dict[str, str]:
    metadata: dict[str, str] = {}
    try:
        with zipfile.ZipFile(source_path) as archive:
            model_member = ""
            for name in archive.namelist():
                if str(name or "").lower() == "3d/3dmodel.model":
                    model_member = name
                    break
            if not model_member:
                return metadata

            root = ET.fromstring(archive.read(model_member))
            for node in root.findall(".//{*}metadata"):
                key = str(node.attrib.get("name") or "").strip()
                if not key:
                    continue
                value = node.text or node.attrib.get("value") or ""
                metadata[key] = html.unescape(str(value or "")).strip()
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, ET.ParseError, KeyError):
        return {}
    return metadata


def _zip_manifest_signature(source_path: Path) -> str:
    try:
        with zipfile.ZipFile(source_path) as archive:
            names = sorted(name for name in archive.namelist() if not name.endswith("/"))
            manifest = []
            for name in names:
                info = archive.getinfo(name)
                manifest.append(
                    {
                        "name": name,
                        "crc": int(info.CRC),
                        "size": int(info.file_size),
                    }
                )
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, KeyError):
        return ""

    if not manifest:
        return ""
    digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return f"zipsha256:{digest}"


def _sha256_file(source_path: Path) -> str:
    hasher = hashlib.sha256()
    consumed = 0
    try:
        with source_path.open("rb") as handle:
            while True:
                chunk = handle.read(_HASH_CHUNK_SIZE_BYTES)
                if not chunk:
                    break
                hasher.update(chunk)
                consumed += len(chunk)
                if consumed >= _HASH_PAUSE_EVERY_BYTES:
                    consumed = 0
    except OSError:
        return ""
    return hasher.hexdigest()


def inspect_3mf_file(source_path: Path) -> dict[str, Any]:
    try:
        stat = source_path.stat()
    except OSError:
        return {
            "source_title": source_path.stem.strip() or source_path.name,
            "model_title": source_path.stem.strip() or source_path.name,
            "profile_title": source_path.stem.strip() or source_path.name,
            "designer": "",
            "design_model_id": "",
            "design_profile_id": "",
            "file_hash": "",
            "metadata": {},
            "config_fingerprint": "",
        }

    cache_key = source_path.resolve().as_posix()
    signature = (stat.st_mtime_ns, stat.st_size)
    with _INSPECT_CACHE_LOCK:
        cached = _INSPECT_CACHE.get(cache_key)
        if cached and cached.get("signature") == signature:
            payload = cached.get("payload")
            if isinstance(payload, dict):
                return dict(payload)

    metadata = parse_3mf_metadata(source_path)
    design_model_id = str(metadata.get("DesignModelId") or "").strip()
    design_profile_id = str(metadata.get("DesignProfileId") or "").strip()
    file_hash = ""
    if not design_profile_id:
        file_hash = _zip_manifest_signature(source_path) or _sha256_file(source_path)

    model_title = str(metadata.get("Title") or source_path.stem).strip() or source_path.stem
    profile_title = str(metadata.get("ProfileTitle") or model_title).strip() or model_title
    designer = str(metadata.get("Designer") or metadata.get("ProfileUserName") or "").strip()
    config_fingerprint = f"design_profile:{design_profile_id}" if design_profile_id else (file_hash if ":" in file_hash else f"sha256:{file_hash}" if file_hash else "")
    payload = {
        "source_title": source_path.stem.strip() or source_path.name,
        "model_title": model_title,
        "profile_title": profile_title,
        "designer": designer,
        "design_model_id": design_model_id,
        "design_profile_id": design_profile_id,
        "file_hash": file_hash,
        "metadata": metadata,
        "config_fingerprint": config_fingerprint,
    }
    with _INSPECT_CACHE_LOCK:
        _INSPECT_CACHE[cache_key] = {
            "signature": signature,
            "payload": payload,
        }
    return dict(payload)


def build_instance_file_inventory(model_root: Path) -> list[dict[str, Any]]:
    instances_dir = model_root / "instances"
    if not instances_dir.exists():
        return []

    inventory: list[dict[str, Any]] = []
    for path in sorted(instances_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() != _THREE_MF_SUFFIX:
            continue
        loose_stem = _normalize_loose_identity_text(path.stem)
        inventory.append(
            {
                "path": path,
                "name": path.name,
                "stem": path.stem,
                "loose_stem": loose_stem,
                "numeric_tokens": set(re.findall(r"\d+", path.stem)),
                "analysis": None,
                "title_keys": set(),
                "design_profile_id": "",
                "config_fingerprint": "",
            }
        )
    return inventory


def _ensure_record_analysis(record: dict[str, Any]) -> dict[str, Any]:
    analysis = record.get("analysis")
    if isinstance(analysis, dict):
        return analysis

    path = record.get("path")
    if not isinstance(path, Path):
        analysis = {}
    else:
        analysis = inspect_3mf_file(path)

    title_keys = {
        _normalize_loose_identity_text(analysis.get("source_title")),
        _normalize_loose_identity_text(analysis.get("model_title")),
        _normalize_loose_identity_text(analysis.get("profile_title")),
    }
    record["analysis"] = analysis
    record["title_keys"] = {item for item in title_keys if item}
    record["design_profile_id"] = str(analysis.get("design_profile_id") or "").strip()
    record["config_fingerprint"] = str(analysis.get("config_fingerprint") or "").strip()
    design_profile_id = str(record.get("design_profile_id") or "").strip()
    if design_profile_id:
        record.setdefault("numeric_tokens", set()).add(design_profile_id)
    return analysis


def _instance_identity_values(instance: dict[str, Any]) -> list[str]:
    return _unique_non_empty(
        [
            instance.get("profileId"),
            instance.get("instanceId"),
            instance.get("id"),
        ]
    )


def _instance_config_fingerprints(instance: dict[str, Any]) -> list[str]:
    local_import = instance.get("localImport") if isinstance(instance.get("localImport"), dict) else {}
    candidates = [
        instance.get("configFingerprint"),
        local_import.get("configFingerprint"),
    ]
    return _unique_non_empty(candidates)


def _instance_title_keys(instance: dict[str, Any]) -> list[str]:
    names: list[Any] = [
        instance.get("name"),
        instance.get("title"),
        instance.get("profileName"),
    ]
    file_name = Path(str(instance.get("fileName") or "")).stem
    if file_name:
        names.append(file_name)
    return _unique_non_empty([_normalize_loose_identity_text(item) for item in names])


def _make_match(record: dict[str, Any], *, confidence: str, reason: str) -> dict[str, Any]:
    _ensure_record_analysis(record)
    path = record.get("path")
    return {
        "path": path,
        "file_name": path.name if isinstance(path, Path) else "",
        "confidence": confidence,
        "reason": reason,
        "design_profile_id": str(record.get("design_profile_id") or "").strip(),
        "config_fingerprint": str(record.get("config_fingerprint") or "").strip(),
    }


def _filter_available_records(inventory: list[dict[str, Any]], used_paths: set[Path]) -> list[dict[str, Any]]:
    return [record for record in inventory if isinstance(record.get("path"), Path) and record["path"] not in used_paths]


def _match_by_design_profile_id(
    instance: dict[str, Any],
    inventory: list[dict[str, Any]],
    used_paths: set[Path],
) -> Optional[dict[str, Any]]:
    identity_values = _instance_identity_values(instance)
    if not identity_values:
        return None
    matches: list[dict[str, Any]] = []
    for record in _filter_available_records(inventory, used_paths):
        _ensure_record_analysis(record)
        if str(record.get("design_profile_id") or "").strip() in identity_values:
            matches.append(record)
    if len(matches) == 1:
        return _make_match(matches[0], confidence="strong", reason="design_profile_id")
    return None


def _match_by_config_fingerprint(
    instance: dict[str, Any],
    inventory: list[dict[str, Any]],
    used_paths: set[Path],
) -> Optional[dict[str, Any]]:
    config_fingerprints = _instance_config_fingerprints(instance)
    if not config_fingerprints:
        return None
    matches: list[dict[str, Any]] = []
    for record in _filter_available_records(inventory, used_paths):
        _ensure_record_analysis(record)
        if str(record.get("config_fingerprint") or "").strip() in config_fingerprints:
            matches.append(record)
    if len(matches) == 1:
        return _make_match(matches[0], confidence="strong", reason="config_fingerprint")
    return None


def _match_by_filename_token(
    instance: dict[str, Any],
    inventory: list[dict[str, Any]],
    used_paths: set[Path],
) -> Optional[dict[str, Any]]:
    identity_values = _instance_identity_values(instance)
    if not identity_values:
        return None

    matches: list[dict[str, Any]] = []
    for record in _filter_available_records(inventory, used_paths):
        numeric_tokens = record.get("numeric_tokens")
        if not isinstance(numeric_tokens, set):
            numeric_tokens = set()
            record["numeric_tokens"] = numeric_tokens
        if any(value in numeric_tokens for value in identity_values):
            matches.append(record)
    if len(matches) == 1:
        return _make_match(matches[0], confidence="strong", reason="file_name_token")
    return None


def _match_by_title(
    instance: dict[str, Any],
    inventory: list[dict[str, Any]],
    used_paths: set[Path],
) -> Optional[dict[str, Any]]:
    title_keys = _instance_title_keys(instance)
    if not title_keys:
        return None

    exact_matches: list[dict[str, Any]] = []
    partial_matches: list[dict[str, Any]] = []
    for record in _filter_available_records(inventory, used_paths):
        _ensure_record_analysis(record)
        loose_stem = str(record.get("loose_stem") or "")
        title_index = record.get("title_keys") if isinstance(record.get("title_keys"), set) else set()
        for title_key in title_keys:
            if not title_key:
                continue
            if title_key == loose_stem or title_key in title_index:
                exact_matches.append(record)
                break
            if len(title_key) >= 4 and (title_key in loose_stem or loose_stem in title_key):
                partial_matches.append(record)
                break
            if any(len(index_key) >= 4 and (title_key in index_key or index_key in title_key) for index_key in title_index):
                partial_matches.append(record)
                break

    if len(exact_matches) == 1:
        return _make_match(exact_matches[0], confidence="strong", reason="title_exact")
    if len(partial_matches) == 1:
        return _make_match(partial_matches[0], confidence="medium", reason="title_partial")
    return None


def resolve_model_instance_files(
    meta: dict[str, Any],
    model_root: Path,
    *,
    use_weak_fallback: bool = True,
) -> dict[str, Any]:
    inventory = build_instance_file_inventory(model_root)
    instances = meta.get("instances") if isinstance(meta.get("instances"), list) else []
    matches: dict[int, dict[str, Any]] = {}
    used_paths: set[Path] = set()

    exact_by_name: dict[str, dict[str, Any]] = {}
    for record in inventory:
        name = str(record.get("name") or "").lower()
        if name and name not in exact_by_name:
            exact_by_name[name] = record

    for index, instance in enumerate(instances):
        if not isinstance(instance, dict):
            continue
        file_name = Path(str(instance.get("fileName") or "")).name
        record = exact_by_name.get(file_name.lower()) if file_name else None
        if not record:
            continue
        path = record.get("path")
        if not isinstance(path, Path) or path in used_paths:
            continue
        matches[index] = _make_match(record, confidence="strong", reason="exact_file_name")
        used_paths.add(path)

    matchers = (
        _match_by_design_profile_id,
        _match_by_config_fingerprint,
        _match_by_filename_token,
        _match_by_title,
    )
    for matcher in matchers:
        progress = True
        while progress:
            progress = False
            for index, instance in enumerate(instances):
                if index in matches or not isinstance(instance, dict):
                    continue
                match = matcher(instance, inventory, used_paths)
                if not match:
                    continue
                path = match.get("path")
                if not isinstance(path, Path) or path in used_paths:
                    continue
                matches[index] = match
                used_paths.add(path)
                progress = True

    unresolved_indexes = [index for index, instance in enumerate(instances) if isinstance(instance, dict) and index not in matches]
    remaining_records = [record for record in inventory if isinstance(record.get("path"), Path) and record["path"] not in used_paths]
    if use_weak_fallback and unresolved_indexes and len(unresolved_indexes) == len(remaining_records):
        sorted_indexes = sorted(
            unresolved_indexes,
            key=lambda index: (
                _safe_int((instances[index] or {}).get("profileId")),
                _safe_int((instances[index] or {}).get("id")),
                str((instances[index] or {}).get("title") or (instances[index] or {}).get("name") or ""),
            ),
        )
        sorted_records = sorted(remaining_records, key=lambda item: str(item.get("name") or "").lower())
        for index, record in zip(sorted_indexes, sorted_records):
            path = record.get("path")
            if not isinstance(path, Path):
                continue
            matches[index] = _make_match(record, confidence="weak", reason="remaining_file_pairing")
            used_paths.add(path)

    unmatched_indexes = [index for index, instance in enumerate(instances) if isinstance(instance, dict) and index not in matches]
    unmatched_files = [
        record.get("path")
        for record in inventory
        if isinstance(record.get("path"), Path) and record["path"] not in used_paths
    ]
    return {
        "matches": matches,
        "unmatched_instance_indexes": unmatched_indexes,
        "unmatched_files": unmatched_files,
        "inventory_count": len(inventory),
    }
