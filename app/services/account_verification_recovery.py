from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from app.core.database import database_connection
from app.core.database_json_state import load_database_json_state_without_initialization, update_database_json_state
from app.core.settings import APP_VERSION
from app.core.store import JsonStore
from app.core.timezone import now_iso as china_now_iso
from app.services.archive_model_index import query_archive_model_index
from app.services.cloakbrowser_session import (
    browser_authorize_3mf_download,
    browser_prepare_3mf_download,
    cloakbrowser_configured,
    synchronize_browser_session,
)
from app.services.cookie_utils import extract_auth_token, sanitize_cookie_header
from app.services.legacy_archiver import _extract_instance_download, _normalized_auto_verification_diagnostics
from app.services.state_contracts import ACCOUNT_HEALTH_STATE_KEY, ARCHIVE_QUEUE_STATE_KEY, MISSING_3MF_STATE_KEY, THREE_MF_DAILY_QUOTA_STATE_KEY
from app.services.state_events import publish_state_event
from app.services.three_mf import is_three_mf_download_prohibited
from app.services.three_mf_quota import reserve_three_mf_download_slot


ACCOUNT_VERIFICATION_RECOVERY_STATE_KEY = "account_verification_recovery"
ACCOUNT_VERIFICATION_PREFLIGHT_STATE_KEY = "account_verification_recovery_preflight"
_PREFLIGHT_COOLDOWN_SECONDS = 15 * 60
_PREFLIGHT_REASONS = {"ready", "auth_required", "verification_required", "model_unavailable", "model_mismatch", "instances_unavailable", "preflight_failed"}
_PLATFORM_HOSTS = {"cn": "makerworld.com.cn", "global": "makerworld.com"}
_GATE_FIELDS = ("three_mf_gate", "three_mf_reason", "three_mf_detail", "model_url", "model_id", "instance_id")
_UNSAFE_STATUSES = {"daily_limit", "download_limited", "cookie_invalid", "auth_required", "manual", "paused"}


def _auto_verify_enabled() -> bool:
    return all(str(os.getenv(key, "true")).strip().lower() not in {"false", "0", "no", "off"}
               for key in ("MAKERHUB_AUTO_VERIFY_3MF", "MAKERHUB_AUTO_VERIFY_CHALLENGES"))


def _fingerprint(account: Any) -> str:
    token = extract_auth_token(account.cookie)
    identity = [APP_VERSION, str(account.browser_profile_id), hashlib.sha256(token.encode()).hexdigest()]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def _target(platform: str, item: Any, *, instance: dict[str, Any] | None = None) -> dict[str, str] | None:
    if not isinstance(item, dict) or is_three_mf_download_prohibited(item):
        return None
    if instance is not None and is_three_mf_download_prohibited(instance):
        return None
    raw_url = str(item.get("model_url") or item.get("origin_url") or item.get("url") or "").strip()
    try:
        parsed = urlsplit(raw_url)
        if (parsed.scheme != "https" or parsed.hostname != _PLATFORM_HOSTS[platform]
                or parsed.username or parsed.password or parsed.port not in {None, 443}):
            return None
    except ValueError:
        return None
    match = re.search(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?models/([1-9][0-9]*)(?:[-/]|$)", parsed.path, re.IGNORECASE)
    if not match:
        return None
    instance_data = instance if instance is not None else item
    if instance is not None:
        instance_value = instance_data.get("id") or instance_data.get("instanceId") or instance_data.get("profileId")
    else:
        instance_value = instance_data.get("instance_id") or instance_data.get("instanceId") or instance_data.get("profileId")
    instance_id = str(instance_value or "")
    if not re.fullmatch(r"[1-9][0-9]*", instance_id):
        return None
    model_id = str(item.get("model_id") or (item.get("id") if instance is not None else "") or match.group(1))
    if model_id != match.group(1):
        return None
    # 候选只保存规范模型页，去掉可能携带凭据的 query 和 fragment。
    model_url = urlunsplit(("https", _PLATFORM_HOSTS[platform], parsed.path, "", ""))
    return {"model_id": model_id, "model_url": model_url, "instance_id": instance_id}


def _missing_candidate(platform: str) -> dict[str, Any] | None:
    with database_connection() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        connection.execute("SET LOCAL statement_timeout = '5s'")
        row = connection.execute(
            """
            SELECT item
            FROM makerhub_json_state
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(value -> 'items') = 'array' THEN value -> 'items' ELSE '[]'::jsonb END
            ) AS item
            WHERE key = %s
              AND item ->> 'model_url' ~ %s
              AND COALESCE(item ->> 'instance_id', item ->> 'instanceId', item ->> 'profileId', '') ~ '^[1-9][0-9]*$'
              AND lower(COALESCE(item ->> 'status', '')) IN ('verification_required', 'cloudflare')
            LIMIT 1
            """,
            (MISSING_3MF_STATE_KEY, r"^https://" + re.escape(_PLATFORM_HOSTS[platform]) + r"(?::443)?/"),
        ).fetchone()
    return row.get("item") if isinstance(row, dict) and isinstance(row.get("item"), dict) else None


def _queue_blockers(platform: str) -> dict[str, bool]:
    with database_connection() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        connection.execute("SET LOCAL statement_timeout = '5s'")
        row = connection.execute(
            """
            WITH state AS (SELECT value FROM makerhub_json_state WHERE key = %s),
            tasks AS (
                SELECT item FROM state CROSS JOIN LATERAL jsonb_array_elements(
                    (CASE WHEN jsonb_typeof(value -> 'active') = 'array' THEN value -> 'active' ELSE '[]'::jsonb END)
                    || (CASE WHEN jsonb_typeof(value -> 'queued') = 'array' THEN value -> 'queued' ELSE '[]'::jsonb END)
                ) AS item
            )
            SELECT EXISTS(SELECT 1 FROM tasks WHERE lower(COALESCE(item ->> 'status', '')) IN ('running', 'resuming')) AS active,
                   EXISTS(SELECT 1 FROM tasks
                       WHERE lower(COALESCE(item ->> 'status', '')) = 'paused'
                         AND lower(COALESCE(item ->> 'blocked_reason', '')) = 'manual'
                         AND (COALESCE(item -> 'meta' ->> 'source', '') = %s
                              OR COALESCE(item -> 'meta' ->> 'model_url', item ->> 'url', '') ~ %s
                              OR (COALESCE(item -> 'meta' ->> 'source', '') = ''
                                  AND COALESCE(item -> 'meta' ->> 'model_url', item ->> 'url', '') = ''))) AS manual
            """,
            (ARCHIVE_QUEUE_STATE_KEY, platform, r"^https?://" + re.escape(_PLATFORM_HOSTS[platform]) + r"(?::[0-9]+)?/"),
        ).fetchone()
    return {key: bool((row or {}).get(key)) for key in ("active", "manual")}


def _indexed_candidates(platform: str, model_dirs: list[str]) -> list[dict[str, Any]]:
    if not model_dirs:
        return []
    with database_connection() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        connection.execute("SET LOCAL statement_timeout = '5s'")
        rows = connection.execute(
            """
            SELECT jsonb_build_object(
                'model_id', meta_json -> 'id', 'model_url', meta_json -> 'url',
                'threeMfDownloadAllowed', meta_json -> 'threeMfDownloadAllowed',
                'threeMfSkipReason', meta_json -> 'threeMfSkipReason',
                'paidSetting', meta_json -> 'paidSetting', 'crowdfundingInfo', meta_json -> 'crowdfundingInfo',
                'license', meta_json -> 'license', 'licenseName', meta_json -> 'licenseName',
                'licenseDescriptionInfo', meta_json -> 'licenseDescriptionInfo',
                'instances', (SELECT COALESCE(jsonb_agg(jsonb_build_object(
                    'id', instance -> 'id', 'instanceId', instance -> 'instanceId', 'profileId', instance -> 'profileId',
                    'threeMfDownloadAllowed', instance -> 'threeMfDownloadAllowed',
                    'threeMfSkipReason', instance -> 'threeMfSkipReason', 'downloadState', instance -> 'downloadState',
                    'paidSetting', instance -> 'paidSetting', 'crowdfundingInfo', instance -> 'crowdfundingInfo',
                    'license', instance -> 'license', 'licenseName', instance -> 'licenseName',
                    'licenseDescriptionInfo', instance -> 'licenseDescriptionInfo'
                ) ORDER BY position), '[]'::jsonb)
                FROM jsonb_array_elements(CASE WHEN jsonb_typeof(meta_json -> 'instances') = 'array'
                    THEN meta_json -> 'instances' ELSE '[]'::jsonb END) WITH ORDINALITY AS raw(instance, position)
                WHERE position <= 8)
            ) AS candidate
            FROM archive_model_index
            WHERE source = %s AND model_dir = ANY(%s)
            LIMIT 8
            """,
            (platform, model_dirs[:8]),
        ).fetchall()
    return [row["candidate"] for row in rows if isinstance(row, dict) and isinstance(row.get("candidate"), dict)]


def _find_target(platform: str, snapshot: dict[str, Any]) -> dict[str, str] | None:
    target = _target(platform, snapshot)
    if target:
        return target
    target = _target(platform, _missing_candidate(platform))
    if target:
        return target
    page = query_archive_model_index(source=platform, page_size=8)
    models = (page or {}).get("items", [])[:8]
    for model in models:
        if not isinstance(model, dict):
            continue
        for instance in model.get("instances", []) if isinstance(model.get("instances"), list) else []:
            if isinstance(instance, dict):
                target = _target(platform, model, instance=instance)
                if target:
                    return target
    model_dirs = [str(model["model_dir"]) for model in models if isinstance(model, dict) and model.get("model_dir")]
    for model in _indexed_candidates(platform, model_dirs):
        for instance in model.get("instances") or []:
            if isinstance(instance, dict):
                target = _target(platform, model, instance=instance)
                if target:
                    return target
    return None


def _attempts(state: dict[str, Any], platform: str) -> dict[str, Any]:
    platforms = state.get("platforms") if isinstance(state.get("platforms"), dict) else {}
    current = platforms.get(platform) if isinstance(platforms.get(platform), dict) else {}
    return current.get("attempts") if isinstance(current.get("attempts"), dict) else {}


def _preflight_checks(state: dict[str, Any], platform: str) -> dict[str, Any]:
    platforms = state.get("platforms") if isinstance(state.get("platforms"), dict) else {}
    current = platforms.get(platform) if isinstance(platforms.get(platform), dict) else {}
    return current.get("checks") if isinstance(current.get("checks"), dict) else {}


def _preflight_due(state: dict[str, Any], platform: str, fingerprint: str, now: str) -> bool:
    check = _preflight_checks(state, platform).get(fingerprint)
    if not isinstance(check, dict):
        return True
    try:
        return datetime.fromisoformat(str(check.get("retry_after"))) <= datetime.fromisoformat(now)
    except (ValueError, TypeError):
        # 无法确认旧预约是否已过期时保持关闭，不进行可能重复的浏览器操作。
        return False


def _reserve_preflight(platform: str, fingerprint: str, now: str) -> str:
    check_id = uuid.uuid4().hex
    reserved = False

    def reserve(current):
        nonlocal reserved
        if not _preflight_due(current, platform, fingerprint, now):
            return current
        checks = current.setdefault("platforms", {}).setdefault(platform, {}).setdefault("checks", {})
        checks[fingerprint] = {"check_id": check_id, "status": "preparing", "phase": "preflight",
                               "reason": "preflight_failed", "status_code": 0, "started_at": now,
                               "retry_after": (datetime.fromisoformat(now) + timedelta(seconds=_PREFLIGHT_COOLDOWN_SECONDS)).isoformat()}
        reserved = True
        return current

    update_database_json_state(ACCOUNT_VERIFICATION_PREFLIGHT_STATE_KEY, {"platforms": {}}, reserve)
    return check_id if reserved else ""


def _record_preflight(platform: str, fingerprint: str, check_id: str, *, phase: str, reason: str) -> None:
    def record(current):
        check = _preflight_checks(current, platform).get(fingerprint)
        if isinstance(check, dict) and check.get("check_id") == check_id:
            check.update(status="ready" if reason == "ready" else "deferred", phase=phase, reason=reason,
                         status_code=0, completed_at=china_now_iso())
        return current

    update_database_json_state(ACCOUNT_VERIFICATION_PREFLIGHT_STATE_KEY, {"platforms": {}}, record)


def _fresh_target(platform: str, requested: dict[str, str], preflight: Any) -> dict[str, str] | None:
    if not isinstance(preflight, dict) or preflight.get("ready") is not True or preflight.get("authenticated") is not True:
        return None
    model = preflight.get("model")
    if not isinstance(model, dict) or str(model.get("model_id") or "") != requested["model_id"]:
        return None
    targets = []
    for instance in model.get("instances", []) if isinstance(model.get("instances"), list) else []:
        if isinstance(instance, dict):
            target = _target(platform, model, instance=instance)
            if target and target["model_id"] == requested["model_id"]:
                if target["instance_id"] == requested["instance_id"]:
                    return target
                targets.append(target)
    # 仅恢复复测可以选择同模型的首个合法新配置；普通归档仍按指定配置下载。
    return targets[0] if targets else None


def _claim(platform: str, fingerprint: str, now: str) -> str:
    claimed = False
    claim_id = uuid.uuid4().hex

    def claim(current):
        nonlocal claimed
        if fingerprint in _attempts(current, platform):
            return current
        platforms = current.setdefault("platforms", {})
        attempts = platforms.setdefault(platform, {}).setdefault("attempts", {})
        attempts[fingerprint] = {"status": "claimed", "claimed_at": now, "claim_id": claim_id}
        claimed = True
        return current

    update_database_json_state(ACCOUNT_VERIFICATION_RECOVERY_STATE_KEY, {"platforms": {}}, claim)
    return claim_id if claimed else ""


def _cancel_unsent_claim(platform: str, fingerprint: str, claim_id: str) -> None:
    def cancel(current):
        attempts = _attempts(current, platform)
        attempt = attempts.get(fingerprint)
        if isinstance(attempt, dict) and attempt.get("status") == "claimed" and attempt.get("claim_id") == claim_id:
            del attempts[fingerprint]
        return current

    update_database_json_state(ACCOUNT_VERIFICATION_RECOVERY_STATE_KEY, {"platforms": {}}, cancel)


def _record_result(platform: str, fingerprint: str, status: str, *, status_code: int = 0,
                   reason: str = "authorization_failed", diagnostics: dict[str, Any] | None = None) -> None:
    def record(current):
        attempt = _attempts(current, platform).get(fingerprint)
        if isinstance(attempt, dict):
            attempt.update(status=status, phase="dispatch", reason=reason, status_code=status_code, completed_at=china_now_iso())
            if diagnostics:
                attempt["diagnostics"] = diagnostics
        return current

    update_database_json_state(ACCOUNT_VERIFICATION_RECOVERY_STATE_KEY, {"platforms": {}}, record)


def _same_login(store: JsonStore, expected: Any) -> bool:
    account = next((item for item in store.load().cookies if item.platform == expected.platform), None)
    return bool(account and account.browser_profile_id == expected.browser_profile_id
                and sanitize_cookie_header(account.cookie) == sanitize_cookie_header(expected.cookie)
                and account.browser_status != "account_mismatch")


def _sync_verified_browser(store: JsonStore, expected: Any) -> bool:
    synced = False

    def sync(latest):
        nonlocal synced
        account = next((item for item in latest.cookies if item.platform == expected.platform), None)
        if (account is None or account.browser_profile_id != expected.browser_profile_id
                or sanitize_cookie_header(account.cookie) != sanitize_cookie_header(expected.cookie)
                or account.browser_status == "account_mismatch"):
            return latest
        account.browser_status = "synced"
        account.browser_message = "已通过浏览器确认 3MF 下载授权。"
        account.browser_synced_at = china_now_iso()
        synced = True
        return latest

    store.update(sync)
    return synced


def _clear_verified_gate(platform: str, snapshot: dict[str, Any], target: dict[str, str]) -> bool:
    cleared = False

    def clear(current):
        nonlocal cleared
        latest = current.get(platform)
        if not isinstance(latest, dict) or latest.get("status") in _UNSAFE_STATUSES:
            return current
        if any(latest.get(field, "") != snapshot.get(field, "") for field in _GATE_FIELDS):
            return current
        latest.update(status="ok", reason="", source="account_verification_recovery", detail="已通过浏览器确认 3MF 下载授权。",
                      three_mf_gate="open", three_mf_reason="", three_mf_detail="", updated_at=china_now_iso(), **target)
        cleared = True
        return current

    update_database_json_state(ACCOUNT_HEALTH_STATE_KEY, {}, clear)
    if cleared:
        publish_state_event(ACCOUNT_HEALTH_STATE_KEY, "account_health.changed", {"reason": "account_verification_recovery"})
    return cleared


def _valid_download(payload: Any) -> bool:
    _name, url = _extract_instance_download(payload)
    try:
        parsed = urlsplit(url)
        signed = any(key.lower() in {"signature", "sign", "x-amz-signature", "x-oss-signature"}
                     and value for key, value in parse_qsl(parsed.query))
        return bool(parsed.scheme == "https" and parsed.hostname and parsed.path not in {"", "/"}
                    and not parsed.username and not parsed.password
                    and "/design-service/instance/" not in parsed.path and "/models/" not in parsed.path
                    and (parsed.path.lower().endswith(".3mf") or signed))
    except (TypeError, ValueError, AttributeError):
        return False


def _authorization_diagnostics(response: dict[str, Any]) -> dict[str, Any]:
    payload = response.get("payload") if isinstance(response.get("payload"), dict) else {}
    diagnostics = {
        "page_verification_response": payload.get("code") == "MAKERHUB_PAGE_VERIFICATION",
        "captcha_present": any(isinstance(payload.get(key), str) and bool(payload[key].strip())
                               for key in ("captchaId", "captcha_id", "captchaID")),
        "signed_url_present": _valid_download(payload),
    }
    # 缺失的 verification 不代表验证失败；只持久化既有白名单归一化结果。
    if isinstance(response.get("verification"), dict) and response["verification"]:
        verification = _normalized_auto_verification_diagnostics(response["verification"])
        diagnostics["verification"] = {"attempted": verification["attempted"], "completed": verification["completed"],
                                       **verification["fields"]}
    return diagnostics


def run_account_verification_recovery_once(
    *, store: JsonStore | None = None, can_probe: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"attempted": 0, "failed": 0, "recovered_platforms": [], "items": []}
    if not _auto_verify_enabled() or not cloakbrowser_configured():
        return result
    config_store = store or JsonStore()
    config = config_store.load()
    snapshots = load_database_json_state_without_initialization(ACCOUNT_HEALTH_STATE_KEY, {})
    state = load_database_json_state_without_initialization(ACCOUNT_VERIFICATION_RECOVERY_STATE_KEY, {"platforms": {}})
    preflight_state = load_database_json_state_without_initialization(ACCOUNT_VERIFICATION_PREFLIGHT_STATE_KEY, {"platforms": {}})
    quota = load_database_json_state_without_initialization(THREE_MF_DAILY_QUOTA_STATE_KEY, {"items": {}})
    now = china_now_iso()
    for platform in ("cn", "global"):
        snapshot = snapshots.get(platform, {})
        account = next((item for item in config.cookies if item.platform == platform), None)
        if (snapshot.get("three_mf_gate") != "verification_required" or snapshot.get("status") in _UNSAFE_STATUSES
                or not account or not account.browser_profile_id or not extract_auth_token(account.cookie)
                or account.browser_status == "account_mismatch"):
            continue
        fingerprint = _fingerprint(account)
        if fingerprint in _attempts(state, platform):
            continue
        if not _preflight_due(preflight_state, platform, fingerprint, now):
            continue
        limit = int(getattr(config.three_mf_limits, f"{platform}_daily_limit"))
        daily = (quota.get("items") or {}).get(platform, {})
        if limit > 0 and daily.get("date") == now[:10] and int(daily.get("used") or 0) >= limit:
            continue
        try:
            if can_probe is not None and can_probe() is not True:
                continue
            blockers = _queue_blockers(platform)
            if blockers.get("active") or blockers.get("manual"):
                continue
            target = _find_target(platform, snapshot)
            if not target:
                continue
        except Exception:
            # 读取或预约失败时不能发送可能消耗下载额度的请求。
            continue
        phase = "preflight"
        reason = "preflight_failed"
        check_id = ""
        try:
            check_id = _reserve_preflight(platform, fingerprint, now)
            if not check_id:
                continue
            preflight = browser_prepare_3mf_download(platform, profile_id=account.browser_profile_id, model_url=target["model_url"])
            if preflight.get("authenticated") is False and preflight.get("reason") == "auth_required":
                phase = "synchronize"
                reason = "auth_required"
                if (_auto_verify_enabled() and cloakbrowser_configured() and _same_login(config_store, account)
                        and not any(_queue_blockers(platform).values())
                        and (can_probe is None or can_probe() is True)):
                    synchronize_browser_session(platform, account.cookie, profile_id=account.browser_profile_id,
                                                proxy_config=config.proxy, stop_when_done=False)
                    phase = "preflight"
                    preflight = browser_prepare_3mf_download(platform, profile_id=account.browser_profile_id, model_url=target["model_url"])
            fresh_target = _fresh_target(platform, target, preflight)
            if fresh_target:
                target = fresh_target
                reason = "ready"
            else:
                raw_reason = preflight.get("reason")
                reason = raw_reason if isinstance(raw_reason, str) and raw_reason in _PREFLIGHT_REASONS and raw_reason != "ready" else "model_unavailable"
            _record_preflight(platform, fingerprint, check_id, phase=phase, reason=reason)
        except Exception:
            reason = "auth_required" if phase == "synchronize" else "preflight_failed"
            if check_id:
                try:
                    _record_preflight(platform, fingerprint, check_id, phase=phase, reason=reason)
                except Exception:
                    pass
        if reason != "ready":
            result["items"].append({"platform": platform, "status": "deferred", "phase": phase, "reason": reason, "status_code": 0})
            continue
        dispatch_prepared = False

        def before_dispatch() -> bool:
            nonlocal dispatch_prepared
            if dispatch_prepared:
                return True
            if can_probe is not None and can_probe() is not True:
                return False
            if not _auto_verify_enabled() or not cloakbrowser_configured() or not _same_login(config_store, account):
                return False
            latest = load_database_json_state_without_initialization(ACCOUNT_HEALTH_STATE_KEY, {}).get(platform, {})
            if (latest.get("status") in _UNSAFE_STATUSES
                    or any(latest.get(field, "") != snapshot.get(field, "") for field in _GATE_FIELDS)
                    or any(_queue_blockers(platform).values())):
                return False
            claim_id = _claim(platform, fingerprint, china_now_iso())
            if not claim_id:
                return False
            try:
                latest_limit = int(getattr(config_store.load().three_mf_limits, f"{platform}_daily_limit"))
                quota_result = reserve_three_mf_download_slot(source=platform, url=target["model_url"], limit=latest_limit, **target)
                if not quota_result.get("allowed", False):
                    _cancel_unsent_claim(platform, fingerprint, claim_id)
                    return False
            except Exception:
                _cancel_unsent_claim(platform, fingerprint, claim_id)
                raise
            dispatch_prepared = True
            result["attempted"] += 1
            return True

        status = "failed"
        status_code = 0
        reason = "authorization_failed"
        diagnostics = {}
        try:
            api_url = f"https://{_PLATFORM_HOSTS[platform]}/api/v1/design-service/instance/{target['instance_id']}/f3mf?type=download&fileType=3mf"
            response = browser_authorize_3mf_download(platform, api_url, profile_id=account.browser_profile_id,
                                                     model_url=target["model_url"], instance_id=target["instance_id"],
                                                     verification_probe=True, before_dispatch=before_dispatch)
            diagnostics = _authorization_diagnostics(response)
            status_code = int(response.get("status_code") or 0)
            payload = response.get("payload") if isinstance(response.get("payload"), dict) else {}
            if status_code == 401:
                reason = "auth_required"
            elif status_code == 429:
                reason = "rate_limited"
            elif status_code == 418:
                reason = "verification_required"
            elif status_code == 409 and payload.get("code") in {"MAKERHUB_TARGET_UNAVAILABLE", "MAKERHUB_TARGET_MISMATCH"}:
                reason = "target_unavailable" if payload["code"] == "MAKERHUB_TARGET_UNAVAILABLE" else "target_mismatch"
            elif 200 <= status_code < 300:
                reason = "state_changed" if _valid_download(payload) else "invalid_download"
            if (dispatch_prepared and 200 <= status_code < 300 and _valid_download(response.get("payload"))
                    and _same_login(config_store, account) and not any(_queue_blockers(platform).values())
                    and _sync_verified_browser(config_store, account) and _same_login(config_store, account)
                    and _clear_verified_gate(platform, snapshot, target)):
                status = "recovered"
                reason = "authorized"
                result["recovered_platforms"].append(platform)
        except Exception:
            # 不持久化异常文本、响应内容或直链，避免暴露认证信息。
            status = "failed"
        if not dispatch_prepared:
            continue
        if status != "recovered":
            result["failed"] += 1
        item = {"platform": platform, "status": status, "phase": "dispatch", "reason": reason, "status_code": status_code}
        if diagnostics:
            item["diagnostics"] = diagnostics
        result["items"].append(item)
        try:
            _record_result(platform, fingerprint, status, status_code=status_code, reason=reason, diagnostics=diagnostics)
        except Exception:
            # 预约已持久化，即使结果写入失败，后续轮询也不能重复授权。
            pass
    return result
