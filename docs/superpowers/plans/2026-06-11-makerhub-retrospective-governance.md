# MakerHub Retrospective Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the governance spec so homepage source status is driven by current account health snapshots, historical failures stay isolated, and diagnostics can explain online status mismatches.

**Architecture:** Add a focused account-health state service backed by Postgres JSON state, then route homepage source cards and archive retry outcomes through that service. Keep existing `source_health.py`, `archive_worker.py`, `runtime_diagnostics.py`, and `TaskStateStore` patterns, but stop using historical `missing_3mf` or raw logs as current homepage state.

**Tech Stack:** Python 3, FastAPI service modules, Postgres-backed JSON state via `app.core.database_json_state`, `unittest`, existing MakerHub task state and source health services.

---

## File Structure

Create:

- `app/services/account_health.py`：current account health snapshot service. Owns allowed states, normalization, read/write helpers, and conversion to homepage card checks.
- `tests/test_account_health.py`：unit tests for snapshot normalization, write-source restrictions, stale handling, and status text mapping.

Modify:

- `app/services/state_contracts.py`：add `ACCOUNT_HEALTH_STATE_KEY = "account_health"` and include it where diagnostics should see JSON state keys.
- `app/services/task_state.py`：register the account health JSON state path only if file-path compatibility is needed; otherwise leave storage to `database_json_state` directly from `account_health.py`.
- `app/services/source_health.py`：make `build_source_health_cards()` read account health snapshots, keep homepage output compatible, and keep `missing_3mf_items` ignored for source-card status.
- `tests/test_source_health.py`：replace temporary “always normal” expectations with snapshot-driven tests: snapshot verification shows `需要验证`, historical missing 3MF does not override `ok`, stale snapshot falls back to normal/checking behavior.
- `app/services/archive_worker.py`：write account health snapshots after successful archive / missing 3MF retry and after classified 3MF failures.
- `tests/test_missing_3mf.py`：assert retry success and classified failures update account health through the service boundary.
- `app/services/runtime_diagnostics.py`：include account health snapshots and current-task-only summaries in diagnostics.
- `tests/test_runtime_diagnostics.py`：assert diagnostics include account health and do not infer status from old missing 3MF failures.
- `docs/modules/state_contracts.md`：document the new account health state ownership.

Do not touch:

- `videos/makerhub-intro/output/`，这是既有未跟踪目录。
- Browser automation / embedded verification code paths.
- UI redesign files unless an existing status label requires a tiny compatibility adjustment.

---

### Task 1: Add Account Health Snapshot Service

**Files:**

- Create: `app/services/account_health.py`
- Modify: `app/services/state_contracts.py`
- Test: `tests/test_account_health.py`

- [ ] **Step 1: Write failing tests for snapshot defaults and normalization**

Create `tests/test_account_health.py` with these tests:

```python
import unittest
from unittest.mock import patch

from app.services import account_health


class AccountHealthTest(unittest.TestCase):
    def setUp(self):
        self.state = {}

        def load_state(key, default):
            return self.state.get(key, default)

        def save_state(key, payload):
            self.state[key] = payload
            return payload

        self.load_patch = patch.object(account_health, "load_database_json_state", side_effect=load_state)
        self.save_patch = patch.object(account_health, "save_database_json_state", side_effect=save_state)
        self.load_patch.start()
        self.save_patch.start()

    def tearDown(self):
        self.load_patch.stop()
        self.save_patch.stop()

    def test_default_snapshots_are_unknown_per_platform(self):
        payload = account_health.load_account_health()

        self.assertEqual(payload["cn"]["status"], "unknown")
        self.assertEqual(payload["global"]["status"], "unknown")
        self.assertEqual(payload["cn"]["source"], "system")
        self.assertEqual(payload["global"]["source"], "system")

    def test_update_normalizes_platform_and_status(self):
        snapshot = account_health.update_account_health(
            "mw_global",
            status="cloudflare",
            reason="captcha page",
            source="archive_download",
            detail="MakerWorld returned a challenge page",
            model_url="https://makerworld.com/zh/models/123",
        )

        self.assertEqual(snapshot["platform"], "global")
        self.assertEqual(snapshot["status"], "verification_required")
        self.assertEqual(snapshot["reason"], "captcha page")
        self.assertEqual(snapshot["source"], "archive_download")
        self.assertEqual(snapshot["model_url"], "https://makerworld.com/zh/models/123")
        self.assertTrue(snapshot["updated_at"])

    def test_unknown_status_is_preserved_as_unknown(self):
        snapshot = account_health.update_account_health(
            "cn",
            status="unexpected_html",
            reason="unclassified response",
            source="diagnostic_probe",
        )

        self.assertEqual(snapshot["status"], "unknown")
        self.assertEqual(snapshot["reason"], "unclassified response")

    def test_homepage_card_fields_from_ok_snapshot(self):
        account_health.update_account_health("cn", status="ok", source="archive_download")

        card = account_health.snapshot_to_source_card("cn", account_health.get_account_health("cn"))

        self.assertEqual(card["state"], "ok")
        self.assertEqual(card["status"], "正常")
        self.assertEqual(card["tone"], "ok")
        self.assertEqual(card["action_label"], "打开官网")
        self.assertEqual(card["url"], "https://makerworld.com.cn")

    def test_homepage_card_fields_from_verification_snapshot(self):
        account_health.update_account_health(
            "global",
            status="verification_required",
            reason="download_probe",
            source="diagnostic_probe",
            detail="线上 Cookie 触发验证页。",
        )

        card = account_health.snapshot_to_source_card("global", account_health.get_account_health("global"))

        self.assertEqual(card["state"], "verification_required")
        self.assertEqual(card["status"], "需要验证")
        self.assertEqual(card["tone"], "danger")
        self.assertEqual(card["detail"], "线上 Cookie 触发验证页。")
        self.assertEqual(card["action_label"], "打开官网")
        self.assertEqual(card["url"], "https://makerworld.com")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and confirm they fail**

Run:

```bash
.venv/bin/python -m unittest tests.test_account_health
```

Expected: FAIL with `ImportError` or missing `account_health` functions.

- [ ] **Step 3: Add state contract constant**

Modify `app/services/state_contracts.py`:

```python
ACCOUNT_HEALTH_STATE_KEY = "account_health"
```

Add it near the other JSON state keys and include it in `DASHBOARD_EVENT_SCOPES` only if state events are published for account health in Step 4. If no state events are published yet, do not add it to dashboard scopes.

- [ ] **Step 4: Implement `app/services/account_health.py`**

Create `app/services/account_health.py`:

```python
from __future__ import annotations

from typing import Any

from app.core.database_json_state import load_database_json_state, save_database_json_state
from app.core.timezone import now_iso as china_now_iso
from app.services.state_contracts import ACCOUNT_HEALTH_STATE_KEY
from app.services.three_mf import normalize_makerworld_source

ACCOUNT_HEALTH_PLATFORMS = ("cn", "global")
ACCOUNT_HEALTH_STATUSES = frozenset(
    {
        "ok",
        "verification_required",
        "daily_limit",
        "cookie_invalid",
        "network_error",
        "unknown",
    }
)
_STATUS_ALIASES = {
    "cloudflare": "verification_required",
    "auth_required": "cookie_invalid",
    "download_limited": "daily_limit",
    "missing_cookie": "cookie_invalid",
    "http_error": "network_error",
}
_PLATFORM_ORIGINS = {
    "cn": "https://makerworld.com.cn",
    "global": "https://makerworld.com",
}
_PLATFORM_LABELS = {
    "cn": "国内站",
    "global": "国际站",
}
_STATUS_TEXT = {
    "ok": "正常",
    "verification_required": "需要验证",
    "daily_limit": "到达每日上限",
    "cookie_invalid": "Cookie 异常",
    "network_error": "网络异常",
    "unknown": "未检测",
}
_STATUS_TONE = {
    "ok": "ok",
    "verification_required": "danger",
    "daily_limit": "warning",
    "cookie_invalid": "danger",
    "network_error": "warning",
    "unknown": "neutral",
}


def normalize_account_platform(platform: Any = "", url: Any = "") -> str:
    normalized = normalize_makerworld_source(str(platform or ""), str(url or ""))
    if normalized in ACCOUNT_HEALTH_PLATFORMS:
        return normalized
    raw = str(platform or "").strip().lower()
    if raw in {"global", "intl", "international", "mw_global"}:
        return "global"
    return "cn"


def normalize_account_health_status(status: Any) -> str:
    raw = str(status or "").strip().lower()
    raw = _STATUS_ALIASES.get(raw, raw)
    if raw in ACCOUNT_HEALTH_STATUSES:
        return raw
    return "unknown"


def _default_snapshot(platform: str) -> dict[str, Any]:
    normalized_platform = normalize_account_platform(platform)
    return {
        "platform": normalized_platform,
        "status": "unknown",
        "reason": "",
        "detail": "",
        "source": "system",
        "model_url": "",
        "model_id": "",
        "instance_id": "",
        "updated_at": "",
    }


def _normalize_snapshot(platform: str, payload: Any) -> dict[str, Any]:
    snapshot = _default_snapshot(platform)
    if isinstance(payload, dict):
        snapshot.update(
            {
                "status": normalize_account_health_status(payload.get("status")),
                "reason": str(payload.get("reason") or "").strip(),
                "detail": str(payload.get("detail") or "").strip(),
                "source": str(payload.get("source") or "system").strip() or "system",
                "model_url": str(payload.get("model_url") or "").strip(),
                "model_id": str(payload.get("model_id") or "").strip(),
                "instance_id": str(payload.get("instance_id") or "").strip(),
                "updated_at": str(payload.get("updated_at") or "").strip(),
            }
        )
    return snapshot


def load_account_health() -> dict[str, dict[str, Any]]:
    raw = load_database_json_state(ACCOUNT_HEALTH_STATE_KEY, {})
    raw = raw if isinstance(raw, dict) else {}
    return {
        platform: _normalize_snapshot(platform, raw.get(platform))
        for platform in ACCOUNT_HEALTH_PLATFORMS
    }


def save_account_health(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    normalized = {
        platform: _normalize_snapshot(platform, (payload or {}).get(platform))
        for platform in ACCOUNT_HEALTH_PLATFORMS
    }
    return save_database_json_state(ACCOUNT_HEALTH_STATE_KEY, normalized)


def get_account_health(platform: str) -> dict[str, Any]:
    normalized_platform = normalize_account_platform(platform)
    return load_account_health()[normalized_platform]


def update_account_health(
    platform: Any,
    *,
    status: Any,
    reason: str = "",
    source: str = "system",
    detail: str = "",
    model_url: str = "",
    model_id: str = "",
    instance_id: str = "",
) -> dict[str, Any]:
    normalized_platform = normalize_account_platform(platform, model_url)
    payload = load_account_health()
    snapshot = _normalize_snapshot(
        normalized_platform,
        {
            "platform": normalized_platform,
            "status": normalize_account_health_status(status),
            "reason": reason,
            "detail": detail,
            "source": source,
            "model_url": model_url,
            "model_id": model_id,
            "instance_id": instance_id,
            "updated_at": china_now_iso(),
        },
    )
    payload[normalized_platform] = snapshot
    save_account_health(payload)
    return snapshot


def mark_account_ok(platform: Any, *, source: str, model_url: str = "", model_id: str = "", instance_id: str = "") -> dict[str, Any]:
    return update_account_health(
        platform,
        status="ok",
        reason="current_action_succeeded",
        source=source,
        detail="",
        model_url=model_url,
        model_id=model_id,
        instance_id=instance_id,
    )


def snapshot_to_source_card(platform: str, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized_platform = normalize_account_platform(platform)
    current = _normalize_snapshot(normalized_platform, snapshot or get_account_health(normalized_platform))
    status = current["status"]
    return {
        "key": normalized_platform,
        "title": _PLATFORM_LABELS.get(normalized_platform, normalized_platform),
        "status": _STATUS_TEXT.get(status, "未检测"),
        "detail": current.get("detail") or "",
        "tone": _STATUS_TONE.get(status, "neutral"),
        "state": status,
        "checks": [],
        "url": _PLATFORM_ORIGINS.get(normalized_platform, ""),
        "action_label": "打开官网",
        "updated_at": current.get("updated_at") or "",
        "reason": current.get("reason") or "",
        "source": current.get("source") or "system",
    }
```

- [ ] **Step 5: Run account health tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_account_health
```

Expected: PASS.

- [ ] **Step 6: Commit Task 1**

```bash
git add app/services/account_health.py app/services/state_contracts.py tests/test_account_health.py
git commit -m "feat: 增加账号健康快照服务"
```

---

### Task 2: Make Homepage Source Cards Read Account Health Only

**Files:**

- Modify: `app/services/source_health.py`
- Modify: `tests/test_source_health.py`

- [ ] **Step 1: Add failing tests for snapshot-driven homepage cards**

Add these tests to `SourceHealthCardsTest` in `tests/test_source_health.py`:

```python
    def test_homepage_status_reads_current_account_health_snapshot(self):
        from app.services import account_health

        state = {}

        def load_state(key, default):
            return state.get(key, default)

        def save_state(key, payload):
            state[key] = payload
            return payload

        with patch.object(account_health, "load_database_json_state", side_effect=load_state), \
                patch.object(account_health, "save_database_json_state", side_effect=save_state):
            account_health.update_account_health(
                "cn",
                status="verification_required",
                reason="download_probe",
                source="diagnostic_probe",
                detail="线上 Cookie 触发验证页。",
            )
            cards = source_health.build_source_health_cards(SimpleNamespace(cookies=[], proxy=None), [])

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "verification_required")
        self.assertEqual(card_map["cn"]["status"], "需要验证")
        self.assertEqual(card_map["cn"]["detail"], "线上 Cookie 触发验证页。")
        self.assertEqual(card_map["global"]["state"], "unknown")

    def test_historical_missing_3mf_does_not_override_account_health_ok_snapshot(self):
        from app.services import account_health

        state = {}

        def load_state(key, default):
            return state.get(key, default)

        def save_state(key, payload):
            state[key] = payload
            return payload

        with patch.object(account_health, "load_database_json_state", side_effect=load_state), \
                patch.object(account_health, "save_database_json_state", side_effect=save_state):
            account_health.update_account_health("cn", status="ok", source="archive_download")
            cards = source_health.build_source_health_cards(
                SimpleNamespace(cookies=[], proxy=None),
                [
                    {
                        "status": "verification_required",
                        "message": "历史验证失败",
                        "model_url": "https://makerworld.com.cn/zh/models/123",
                    }
                ],
            )

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "ok")
        self.assertEqual(card_map["cn"]["status"], "正常")
        self.assertEqual(card_map["cn"]["checks"], [])
```

Also add this import at the top:

```python
from unittest.mock import patch
```

- [ ] **Step 2: Run tests and confirm failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_source_health
```

Expected: FAIL because `build_source_health_cards()` still returns hard-coded normal cards.

- [ ] **Step 3: Wire `source_health.py` to account health**

In `app/services/source_health.py`, add import:

```python
from app.services.account_health import load_account_health, snapshot_to_source_card
```

Replace `build_source_health_cards()` body with:

```python
def build_source_health_cards(
    config: Any,
    missing_3mf_items: list[dict[str, Any]] | None = None,
    *,
    remote_refresh_state: dict[str, Any] | None = None,
    prefer_cached: bool = False,
) -> list[dict[str, Any]]:
    snapshots = load_account_health()
    platforms = ("cn", "global")

    def build_card(platform: str) -> dict[str, Any]:
        return snapshot_to_source_card(platform, snapshots.get(platform))

    with ThreadPoolExecutor(max_workers=len(platforms)) as executor:
        results = list(executor.map(build_card, platforms))
    return results
```

Keep the parameters for API compatibility. Do not read `missing_3mf_items` or `remote_refresh_state` in this function.

- [ ] **Step 4: Update older tests that expected probe results to be ignored**

Existing tests such as `test_account_probe_verification_does_not_change_homepage_status` should be renamed or adjusted so the assertion is about missing 3MF and web probes not being read directly. If a test patches `_probe_platform_status` and expects homepage to ignore it, keep that assertion, because source cards now read account health snapshots only.

When account health default is `unknown`, update affected expected states from `ok` to `unknown` only for cases where no snapshot is set. Keep tests that explicitly write an `ok` snapshot expecting `ok`.

- [ ] **Step 5: Run source health tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_account_health tests.test_source_health
```

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```bash
git add app/services/source_health.py tests/test_source_health.py
git commit -m "fix: 首页源站状态改读账号健康快照"
```

---

### Task 3: Update Account Health From Archive and Missing 3MF Outcomes

**Files:**

- Modify: `app/services/archive_worker.py`
- Modify: `tests/test_missing_3mf.py`

- [ ] **Step 1: Write failing tests for retry success and classified failure writes**

Add imports to `tests/test_missing_3mf.py` if missing:

```python
from unittest.mock import patch
```

Add this test near existing `retry_missing_3mf` tests:

```python
    def test_missing_3mf_retry_success_marks_account_ok(self):
        manager = ArchiveTaskManager()
        health_updates = []
        manager.store = SimpleNamespace(load=lambda: SimpleNamespace(cookies=[]))
        manager.task_store = SimpleNamespace(
            update_missing_3mf_status=lambda **_payload: None,
            replace_missing_3mf_for_model=lambda *_args, **_kwargs: None,
            remove_recent_failures_for_model=lambda *_args, **_kwargs: None,
            update_active_task=lambda *_args, **_kwargs: None,
            complete_archive_task=lambda *_args, **_kwargs: None,
        )

        with patch.object(archive_worker_module, "_select_cookie", return_value="cookie"), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                patch.object(archive_worker_module, "run_archive_model_job", return_value={
                    "model_id": "2193050",
                    "base_name": "Demo",
                    "work_dir": "",
                    "missing_3mf": [],
                }), \
                patch.object(archive_worker_module, "mark_account_ok", side_effect=lambda *args, **kwargs: health_updates.append((args, kwargs)) or {}), \
                patch.object(archive_worker_module, "upsert_archive_snapshot_model", return_value=False), \
                patch.object(archive_worker_module, "invalidate_archive_snapshot"), \
                patch.object(archive_worker_module, "_log_archive"):
            manager._run_single_task(
                "task-1",
                "https://makerworld.com/zh/models/2193050",
                {"missing_3mf_retry": True, "source": "global"},
            )

        self.assertEqual(health_updates[0][0][0], "global")
        self.assertEqual(health_updates[0][1]["source"], "missing_3mf_retry")
```

Add this test for classified failure:

```python
    def test_archive_download_verification_failure_updates_account_health(self):
        manager = ArchiveTaskManager()
        health_updates = []
        manager.store = SimpleNamespace(load=lambda: SimpleNamespace(cookies=[]))
        manager.task_store = SimpleNamespace(
            replace_missing_3mf_for_model=lambda *_args, **_kwargs: None,
            remove_recent_failures_for_model=lambda *_args, **_kwargs: None,
            update_active_task=lambda *_args, **_kwargs: None,
            complete_archive_task=lambda *_args, **_kwargs: None,
        )

        with patch.object(archive_worker_module, "_select_cookie", return_value="cookie"), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                patch.object(archive_worker_module, "run_archive_model_job", return_value={
                    "model_id": "2193050",
                    "base_name": "Demo",
                    "work_dir": "",
                    "missing_3mf": [
                        {
                            "id": "profile-1",
                            "title": "Profile",
                            "downloadState": "verification_required",
                            "downloadMessage": "需要验证",
                        }
                    ],
                }), \
                patch.object(archive_worker_module, "update_account_health", side_effect=lambda *args, **kwargs: health_updates.append((args, kwargs)) or {}), \
                patch.object(archive_worker_module, "_log_archive"):
            manager._run_single_task("task-1", "https://makerworld.com.cn/zh/models/2193050", {})

        self.assertEqual(health_updates[0][0][0], "cn")
        self.assertEqual(health_updates[0][1]["status"], "verification_required")
        self.assertEqual(health_updates[0][1]["source"], "archive_download")
```

- [ ] **Step 2: Run tests and confirm failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_missing_3mf
```

Expected: FAIL because `archive_worker.py` has not imported or called account health helpers.

- [ ] **Step 3: Import account health helpers in `archive_worker.py`**

Add:

```python
from app.services.account_health import mark_account_ok, update_account_health
```

- [ ] **Step 4: Update `_run_single_task()` success path**

After `self.task_store.replace_missing_3mf_for_model(resolved_model_id, missing_items)` and before recent-failure cleanup, add:

```python
        source_platform = normalize_makerworld_source(meta.get("source"), url)
        if not profile_metadata_only and not missing_items:
            mark_account_ok(
                source_platform,
                source="missing_3mf_retry" if missing_3mf_retry else "archive_download",
                model_url=normalize_source_url(url),
                model_id=resolved_model_id,
            )
```

- [ ] **Step 5: Update `_run_single_task()` classified failure path**

After `missing_items` is built and before `replace_missing_3mf_for_model()`, add:

```python
        source_platform = normalize_makerworld_source(meta.get("source"), url)
        for missing_item in missing_items:
            state = str(missing_item.get("status") or "").strip()
            if state in {"verification_required", "cloudflare", "auth_required", "download_limited"}:
                update_account_health(
                    source_platform,
                    status=state,
                    reason="three_mf_download_failed",
                    source="archive_download",
                    detail=str(missing_item.get("message") or ""),
                    model_url=normalize_source_url(url),
                    model_id=resolved_model_id,
                    instance_id=str(missing_item.get("instance_id") or ""),
                )
                break
```

`account_health.normalize_account_health_status()` maps `cloudflare`, `auth_required`, and `download_limited` to the public account states.

- [ ] **Step 6: Run archive-related tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_missing_3mf tests.test_archive_worker_batch_retry tests.test_source_health tests.test_account_health
```

Expected: PASS.

- [ ] **Step 7: Commit Task 3**

```bash
git add app/services/archive_worker.py tests/test_missing_3mf.py
git commit -m "fix: 归档结果同步账号健康状态"
```

---

### Task 4: Add Current-State Diagnostics for Account Health

**Files:**

- Modify: `app/services/runtime_diagnostics.py`
- Modify: `tests/test_runtime_diagnostics.py`
- Modify: `docs/modules/state_contracts.md`

- [ ] **Step 1: Write failing diagnostics test**

Add this test to `RuntimeDiagnosticsTest`:

```python
    def test_runtime_diagnostics_includes_account_health_snapshots(self):
        snapshots = {
            "cn": {
                "platform": "cn",
                "status": "ok",
                "reason": "current_action_succeeded",
                "source": "archive_download",
                "updated_at": "2026-06-11T10:00:00+08:00",
            },
            "global": {
                "platform": "global",
                "status": "verification_required",
                "reason": "download_probe",
                "source": "diagnostic_probe",
                "updated_at": "2026-06-11T10:01:00+08:00",
            },
        }

        with patch.object(runtime_diagnostics, "database_status", return_value={"available": False}), \
                patch.object(runtime_diagnostics, "load_account_health", return_value=snapshots):
            payload = runtime_diagnostics.build_runtime_diagnostics()

        self.assertEqual(payload["account_health"]["cn"]["status"], "ok")
        self.assertEqual(payload["account_health"]["global"]["status"], "verification_required")
        self.assertNotIn("missing_3mf", payload["account_health"]["cn"])
```

- [ ] **Step 2: Run diagnostics tests and confirm failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_runtime_diagnostics
```

Expected: FAIL because diagnostics does not expose account health.

- [ ] **Step 3: Implement diagnostics field**

In `app/services/runtime_diagnostics.py`, import:

```python
from app.services.account_health import load_account_health
```

In `build_runtime_diagnostics()`, add this field to the initial payload:

```python
        "account_health": load_account_health(),
```

The field must be populated even when the database aggregate section is unavailable, because JSON state fallback may still return default snapshots.

- [ ] **Step 4: Document state ownership**

Update `docs/modules/state_contracts.md` by adding a row for account health. Use this content near the JSON state table:

```markdown
| `account_health` | `app.services.account_health` | archive download, missing 3MF retry, diagnostic probe | dashboard source cards, runtime diagnostics | current account/source usability only; never derived from historical `missing_3mf` or raw logs |
```

Also add a short note:

```markdown
`account_health` is the only state that may drive homepage source status. Historical `missing_3mf`, task failures, and diagnostic logs are evidence for troubleshooting, not current homepage status.
```

- [ ] **Step 5: Run diagnostics and docs-adjacent tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_runtime_diagnostics tests.test_account_health tests.test_source_health
```

Expected: PASS.

- [ ] **Step 6: Commit Task 4**

```bash
git add app/services/runtime_diagnostics.py tests/test_runtime_diagnostics.py docs/modules/state_contracts.md
git commit -m "docs: 固化账号健康状态诊断边界"
```

---

### Task 5: Add Guard Tests for Flow Boundaries and Release Readiness

**Files:**

- Modify: `tests/test_source_health.py`
- Modify: `tests/test_runtime_diagnostics.py`
- Modify: `docs/superpowers/specs/2026-06-11-makerhub-retrospective-governance-design.md`

- [ ] **Step 1: Add regression test for old logs / current homepage separation**

Add this test to `tests/test_source_health.py`:

```python
    def test_source_cards_do_not_read_remote_refresh_or_missing_history_when_snapshot_ok(self):
        from app.services import account_health

        state = {}

        def load_state(key, default):
            return state.get(key, default)

        def save_state(key, payload):
            state[key] = payload
            return payload

        with patch.object(account_health, "load_database_json_state", side_effect=load_state), \
                patch.object(account_health, "save_database_json_state", side_effect=save_state):
            account_health.update_account_health("cn", status="ok", source="archive_download")
            cards = source_health.build_source_health_cards(
                SimpleNamespace(cookies=[], proxy=None),
                [{"status": "verification_required", "model_url": "https://makerworld.com.cn/zh/models/1"}],
                remote_refresh_state={
                    "recent_items": [
                        {
                            "status": "failed",
                            "message": "Cloudflare 验证失败",
                            "url": "https://makerworld.com.cn/zh/models/2",
                        }
                    ]
                },
            )

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "ok")
        self.assertEqual(card_map["cn"]["status"], "正常")
```

- [ ] **Step 2: Add diagnostics test for current task focus**

If `runtime_diagnostics._archive_queue_diagnostics()` already returns only active / queued / recent failures counts, add a test that account health remains separate from archive queue:

```python
    def test_runtime_diagnostics_keeps_account_health_separate_from_archive_failures(self):
        queue = {
            "active": [],
            "queued": [],
            "recent_failures": [
                {"id": "old-failure", "status": "failed", "title": "Old verification failure"}
            ],
        }
        snapshots = {
            "cn": {"platform": "cn", "status": "ok", "source": "archive_download"},
            "global": {"platform": "global", "status": "unknown", "source": "system"},
        }

        with patch.object(runtime_diagnostics, "database_status", return_value={"available": False}), \
                patch.object(runtime_diagnostics.task_state_store, "load_archive_queue", return_value=queue), \
                patch.object(runtime_diagnostics, "load_account_health", return_value=snapshots):
            payload = runtime_diagnostics.build_runtime_diagnostics()

        self.assertEqual(payload["archive_queue"]["failed_count"], 1)
        self.assertEqual(payload["account_health"]["cn"]["status"], "ok")
```

- [ ] **Step 3: Run targeted regression suite**

Run:

```bash
.venv/bin/python -m unittest tests.test_account_health tests.test_source_health tests.test_missing_3mf tests.test_runtime_diagnostics tests.test_source_library tests.test_config_cookies
```

Expected: PASS.

- [ ] **Step 4: Update spec implementation status**

Append this section to `docs/superpowers/specs/2026-06-11-makerhub-retrospective-governance-design.md`:

```markdown
## Implementation Tracking

Implementation is tracked by `docs/superpowers/plans/2026-06-11-makerhub-retrospective-governance.md`.

Required regression suite before release:

```bash
.venv/bin/python -m unittest tests.test_account_health tests.test_source_health tests.test_missing_3mf tests.test_runtime_diagnostics tests.test_source_library tests.test_config_cookies
```
```

- [ ] **Step 5: Commit Task 5**

```bash
git add tests/test_source_health.py tests/test_runtime_diagnostics.py docs/superpowers/specs/2026-06-11-makerhub-retrospective-governance-design.md
git commit -m "test: 增加账号健康边界回归"
```

---

## Final Verification

- [ ] **Step 1: Run full targeted suite**

```bash
.venv/bin/python -m unittest tests.test_account_health tests.test_source_health tests.test_missing_3mf tests.test_runtime_diagnostics tests.test_source_library tests.test_config_cookies tests.test_archive_worker_batch_retry
```

Expected: all tests PASS.

- [ ] **Step 2: Check working tree**

```bash
git status --short
```

Expected: only pre-existing `?? videos/makerhub-intro/output/` may remain untracked.

- [ ] **Step 3: Review diff summary**

```bash
git diff --stat HEAD~5..HEAD
```

Expected: only account health service, source health, archive worker, diagnostics, tests, and docs touched.

- [ ] **Step 4: Do not push unless user asks**

This plan does not include version bump or push. If the user asks to push after implementation, bump patch version, update changelog / README latest release notes, run the regression suite again, then push.
