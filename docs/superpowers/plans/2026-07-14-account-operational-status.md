# 账号归档状态收敛 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让首页与线上账号设置页以同一份“能否归档下载”状态展示账号，并隐藏健康账号无关的浏览器关联提示。

**Architecture:** 在 `app.services.account_health` 将账号探针和 `three_mf_gate` 归一为可公开的 operational payload。完整配置接口携带该 payload；设置页只消费它来决定标签、说明和主操作。首页源端状态卡复用同一状态映射，来源同步统计与浏览器会话只保留为辅助/按需信息。

**Tech Stack:** Python 3.11、FastAPI、Pydantic 配置 payload、Vue 3、Node Test、pytest。

## Global Constraints

- 主状态只回答“能否归档下载”，优先级高于账号资料和来源同步结果。
- `cookie_invalid` / `auth_required` 映射为“需要重新登录”；`verification_required` / `cloudflare` 映射为“需要浏览器确认”。
- 账号可归档时不显示“浏览器未关联”，不删除浏览器登录、同步或 Worker gate。
- 轻量配置接口不得读取或返回账号健康快照。
- 所有用户可见改动发布为 patch 版本，更新 `VERSION`、前端包版本、README 和 CHANGELOG。
- 只暂存本计划产生的文件；不得触碰 `videos/makerhub-intro/output/`。

---

### Task 1: 后端统一归档可用性映射

**Files:**
- Modify: `app/services/account_health.py:23-65, 266-303`
- Test: `tests/test_account_health.py:118-201`

**Interfaces:**
- Produces: `operational_status_payload(platform: Any, snapshot: dict[str, Any] | None = None) -> dict[str, str]`。
- Produces payload shape: `{"state", "label", "tone", "message", "action"}`，其中 `action` 为 `"none"`、`"login"`、`"browser"` 或 `"test"`。
- Consumed by: `snapshot_to_source_card()` 和完整配置接口。

- [ ] **Step 1: 写失败的账号健康映射测试**

在 `tests/test_account_health.py` 增加参数化断言，覆盖 gate 优先于账户探针：

```python
def test_operational_status_uses_three_mf_gate_before_account_probe(self):
    payload = account_health.operational_status_payload(
        "cn",
        {"status": "ok", "three_mf_gate": "cookie_invalid"},
    )

    self.assertEqual(payload, {
        "state": "cookie_invalid",
        "label": "需要重新登录",
        "tone": "danger",
        "message": "国内站 3MF 下载需要重新登录。",
        "action": "login",
    })
```

增加 `verification_required`、`daily_limit`、`unknown`、`ok` 的断言，分别期待“需要浏览器确认”/`browser`、“今日下载受限”/`none`、“状态待确认”/`test`、“可归档”/`none`。

同时更新既有 `snapshot_to_source_card()` 断言：`ok` 的 `status` 为“可归档”，`verification_required` 为“需要浏览器确认”，`cookie_invalid` 为“需要重新登录”。

- [ ] **Step 2: 运行测试，确认映射函数尚不存在**

Run: `.venv/bin/python -m pytest tests/test_account_health.py -q`

Expected: FAIL，提示 `operational_status_payload` 不存在。

- [ ] **Step 3: 实现映射并让首页卡片复用**

在 `app/services/account_health.py` 定义：

```python
OPERATIONAL_STATUS_META = {
    "ok": ("可归档", "ok", "none"),
    "cookie_invalid": ("需要重新登录", "danger", "login"),
    "verification_required": ("需要浏览器确认", "warning", "browser"),
    "daily_limit": ("今日下载受限", "warning", "none"),
    "network_error": ("状态待确认", "warning", "test"),
    "unknown": ("状态待确认", "neutral", "test"),
}

def operational_status_payload(platform: Any, snapshot: dict[str, Any] | None = None) -> dict[str, str]:
    normalized_platform = normalize_account_platform(platform)
    current = _normalize_snapshot(normalized_platform, snapshot or get_account_health(normalized_platform))
    state = current["three_mf_gate"] if current["three_mf_gate"] != "open" else current["status"]
    label, tone, action = OPERATIONAL_STATUS_META.get(state, OPERATIONAL_STATUS_META["unknown"])
    title = PLATFORM_TITLES[normalized_platform]
    messages = {
        "ok": f"{title} 3MF 下载可用。",
        "cookie_invalid": f"{title} 3MF 下载需要重新登录。",
        "verification_required": f"{title} 需要在浏览器完成验证后继续归档。",
        "daily_limit": f"{title} 今日下载受限。",
    }
    return {"state": state, "label": label, "tone": tone, "message": messages.get(state, f"{title} 下载状态待确认，请测试。"), "action": action}
```

将 `snapshot_to_source_card()` 改为调用此函数，并把返回的 `label`、`tone`、`message` 写入 `status`、`tone`、`detail`；保留既有 `state`、`three_mf_gate`、URL 和诊断字段以维持 Worker/首页契约。

- [ ] **Step 4: 运行账号健康与源端状态测试**

Run: `.venv/bin/python -m pytest tests/test_account_health.py tests/test_source_health.py -q`

Expected: PASS。

- [ ] **Step 5: 提交后端映射**

```bash
git add app/services/account_health.py tests/test_account_health.py tests/test_source_health.py
git commit -m "fix: 统一账号归档可用状态"
```

### Task 2: 向完整配置接口公开 operational payload

**Files:**
- Modify: `app/api/config.py:71-75, 2753-2806`
- Test: `tests/test_config_payloads.py:13-37`

**Interfaces:**
- Consumes: `load_account_health()` 与 `operational_status_payload()`。
- Produces: 完整 `GET /api/config` payload 的 `account_health`，值为按 `cn` / `global` 分组的 operational payload。
- Preserves: `_public_config_light_payload()` 不包含 `account_health`。

- [ ] **Step 1: 写失败的完整/轻量 payload 测试**

在 `tests/test_config_payloads.py` 添加：

```python
def test_public_config_payload_exposes_operational_account_health_only_in_full_payload():
    health = {"cn": {"status": "ok", "three_mf_gate": "cookie_invalid"}, "global": {"status": "ok"}}
    with (
        patch.object(config_api, "load_account_health", return_value=health),
        patch.object(config_api, "cookie_source_inventory_payload", return_value={"platforms": {}}),
        patch.object(config_api, "cookie_source_sync_state_payload", return_value={}),
        patch.object(config_api, "database_status", return_value={"available": True}),
        patch.object(config_api.task_state_store, "load_remote_refresh_state", return_value={}),
    ):
        payload = config_api._public_config_payload(AppConfig())

    assert payload["account_health"]["cn"]["label"] == "需要重新登录"
    assert payload["account_health"]["cn"]["action"] == "login"
    assert payload["account_health"]["global"]["label"] == "可归档"
```

在既有 light payload 测试中 patch `load_account_health` 为抛错，并断言该函数没有被调用、响应没有 `account_health`。

- [ ] **Step 2: 运行测试，确认 full payload 未公开状态**

Run: `.venv/bin/python -m pytest tests/test_config_payloads.py -q`

Expected: FAIL，提示缺少 `account_health`。

- [ ] **Step 3: 最小化扩展完整 payload**

在 `app/api/config.py` 导入：

```python
from app.services.account_health import load_account_health, operational_status_payload
```

在 `_public_config_payload()` 中一次读取快照，并加入：

```python
health = load_account_health()
return {
    **_public_config_base_payload(config),
    "account_health": {
        platform: operational_status_payload(platform, health.get(platform))
        for platform in ("cn", "global")
    },
    # existing full payload fields
}
```

不要改 `_public_config_light_payload()`。

- [ ] **Step 4: 运行配置接口测试**

Run: `.venv/bin/python -m pytest tests/test_config_payloads.py tests/test_config_cookies.py -q`

Expected: PASS。

- [ ] **Step 5: 提交配置契约**

```bash
git add app/api/config.py tests/test_config_payloads.py
git commit -m "feat: 在配置接口提供归档状态"
```

### Task 3: 设置页只显示主状态和相关操作

**Files:**
- Modify: `frontend/src/lib/accountStatus.js:1-80`
- Modify: `frontend/src/lib/accountStatus.test.mjs:1-70`
- Modify: `frontend/src/lib/browserSession.js:1-41`
- Modify: `frontend/src/pages/SettingsPage.vue:29-112, 1077-1127`
- Test: `frontend/src/lib/browserSession.test.mjs`
- Test: `frontend/src/lib/pageRefreshShape.test.mjs:160-164`

**Interfaces:**
- Consumes: `config.account_health[platform]` shape from Task 2.
- Produces: `accountOperationalView(operational)` with `{label, statusClass, message, action}`.
- Produces: `shouldShowBrowserSession(item, operational)` for the conditional browser row and browser actions.

- [ ] **Step 1: 写失败的纯前端状态测试**

将 `frontend/src/lib/accountStatus.test.mjs` 改为直接传入 operational payload；添加：

```javascript
test("cookie invalid remains a relogin state after source sync succeeds", () => {
  const view = accountOperationalView({
    state: "cookie_invalid",
    label: "需要重新登录",
    tone: "danger",
    message: "国内站 3MF 下载需要重新登录。",
    action: "login",
  });

  assert.deepEqual(view, {
    label: "需要重新登录",
    statusClass: "is-expired",
    message: "国内站 3MF 下载需要重新登录。",
    action: "login",
  });
});
```

在新的 `frontend/src/lib/browserSession.test.mjs` 添加：

```javascript
test("unlinked browser is hidden for an archive-ready account", () => {
  assert.equal(shouldShowBrowserSession({}, { action: "none" }), false);
});

test("browser state is shown when verification requires browser recovery", () => {
  assert.equal(shouldShowBrowserSession({}, { action: "browser" }), true);
});
```

- [ ] **Step 2: 运行测试，确认新 helper 缺失**

Run: `npm --prefix frontend test`

Expected: FAIL，提示 `accountOperationalView` 与 `shouldShowBrowserSession` 未导出。

- [ ] **Step 3: 替换前端启发式状态与默认浏览器提示**

将 `accountStatus.js` 中根据来源资料隐藏 `http_error` 的 `displayStatus()`、`hasProfileEvidence()` 和 `hasSourceEvidence()` 删除，替换为：

```javascript
export function accountOperationalView(operational = {}) {
  const tone = String(operational.tone || "neutral").trim();
  return {
    label: String(operational.label || "状态待确认").trim(),
    statusClass: tone === "danger" ? "is-expired" : tone === "warning" ? "is-warning" : "",
    message: String(operational.message || "账号下载状态待确认，请测试。").trim(),
    action: String(operational.action || "test").trim(),
  };
}
```

在 `browserSession.js` 新增：

```javascript
export function shouldShowBrowserSession(item = {}, operational = {}) {
  const action = String(operational.action || "").trim();
  const status = String(item.browser_status || "").trim();
  return action === "browser" || ["syncing", "launching", "waiting", "action_required", "account_mismatch"].includes(status);
}
```

在 `SettingsPage.vue` 中：

- 从 `config.account_health` 取对应 platform 的 operational payload，并用 `accountOperationalView()` 填充 `statusLabel`、`statusClass`、`message`、`primaryAction`。
- 删除 `statusContext`、`accountStatusLabel()`、`accountStatusClass()` 和 `accountMessageText()` 调用。
- 将浏览器状态行改为 `v-if="item.showBrowserSession"`。
- 仅在 `item.showBrowserSession` 时渲染“打开浏览器”和“从浏览器同步”。
- `primaryAction === "login"` 时把“重新登录”设为 `button-primary`；`primaryAction === "browser"` 时把“打开浏览器”设为 `button-primary`；其它操作保持次按钮。
- 保留资料同步时间、来源统计、“测试”、“同步”和“删除”。

- [ ] **Step 4: 运行前端测试和生产构建**

Run: `npm --prefix frontend test && npm --prefix frontend run build`

Expected: 所有 Node 测试通过，Vite build 成功。

将 `frontend/src/lib/pageRefreshShape.test.mjs` 的静态形状断言从 `accountStatusLabel(mergedItem, statusContext)` 更新为 `accountOperationalView(operational)`，并断言设置页读取 `config.value?.account_health || {}`。

- [ ] **Step 5: 提交设置页收敛**

```bash
git add frontend/src/lib/accountStatus.js frontend/src/lib/accountStatus.test.mjs frontend/src/lib/browserSession.js frontend/src/lib/browserSession.test.mjs frontend/src/lib/pageRefreshShape.test.mjs frontend/src/pages/SettingsPage.vue
git commit -m "fix: 收敛线上账号归档状态"
```

### Task 4: 发布版本与全量验证

**Files:**
- Modify: `VERSION`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `README.md:7, 191-210`
- Modify: `CHANGELOG.md:1-8`
- Modify: `tests/test_release_contract.py:208-227`

**Interfaces:**
- Produces: `v0.11.5` 发布元数据，README 仅展开最新三条记录。

- [ ] **Step 1: 写失败的发布元数据断言**

在 `tests/test_release_contract.py` 将版本契约更新为 `0.11.5`，并断言可见 README 版本依次为：

```python
["0.11.5", "0.11.4", "0.11.3"]
```

- [ ] **Step 2: 运行版本契约，确认其失败**

Run: `.venv/bin/python -m unittest tests.test_release_contract.ReleaseDocumentationContractTest`

Expected: FAIL，当前仓库仍为 `0.11.4`。

- [ ] **Step 3: 更新发布文件**

将根目录和前端 package 版本都改为 `0.11.5`。在 README、CHANGELOG 顶部加入“账号状态以 `3MF` 归档可用性为准；Cookie、验证和浏览器恢复入口不再与来源同步状态冲突”。将 `v0.11.2` 及以前记录移动到 README 折叠历史，保持页面只显示三条。

- [ ] **Step 4: 执行全量质量门禁**

Run:

```bash
git diff --check
python scripts/check_release_version.py
.venv/bin/python -m pytest -q
npm --prefix frontend test
npm --prefix frontend run build
```

Expected: 全部命令以 0 退出；pytest、Node tests 和 Vite build 均无失败。

- [ ] **Step 5: 提交发布修复**

```bash
git add VERSION frontend/package.json frontend/package-lock.json README.md CHANGELOG.md tests/test_release_contract.py
git commit -m "fix: 收敛账号归档状态展示"
```
