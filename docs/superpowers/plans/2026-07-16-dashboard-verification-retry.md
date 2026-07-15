# 首页浏览器验证重试 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在首页的“需要浏览器确认”源端状态卡中提供“已验证”操作，使用户完成官网验证后可以立即恢复同平台受验证阻塞的 `3MF` 归档任务。

**Architecture:** 后端 `snapshot_to_source_card()` 是状态卡动作的唯一契约来源。它仅在 operational state 为 `verification_required` 时下发“打开官网”和 API “已验证”动作；首页继续通过现有 `dashboardStatusActions()` 与 `runStatusAction()` 渲染和提交动作，既不按中文文案推断，也不改变既有的验证确认接口与后台队列逻辑。

**Tech Stack:** Python 3.11、FastAPI、Vue 3、Node built-in `node:test`、pytest、Vite。

## Global Constraints

- “已验证”仅可用于 `verification_required`，不得用于 `cookie_invalid`、`daily_limit`、`network_error`、`checking` 或 `ok`。
- API 动作为 `POST /api/tasks/missing-3mf/verification-verified`，请求体必须是 `{ "platform": "cn" | "global" }`。
- 不更改 `retry_verified_missing_3mf()`：该接口已认证、恢复账号 gate 并在后台调用同平台验证类 `3MF` 重试。
- 不新增依赖，不增加确认弹窗，不修改 Cookie 或指纹浏览器流程。
- 此用户可见功能发布为 patch `v0.11.13`，同步更新 `VERSION`、前端包版本、`README.md` 和 `CHANGELOG.md`。
- 只暂存本计划产生的文件；绝不触碰 `videos/makerhub-intro/output/`。

---

## File Structure

- Modify `app/services/account_health.py`
  - 在状态卡序列化边界为验证阻塞状态下发显式 `actions` 契约。
- Modify `tests/test_account_health.py`
  - 覆盖国内站和国际站的精确动作 payload，并确认其他状态无 API 重试动作。
- Modify `frontend/src/lib/dashboardStatus.test.mjs`
  - 锁定前端对后端显式“已验证”动作的无损规范化。
- Modify `VERSION`、`frontend/package.json`、`frontend/package-lock.json`
  - 将发布版本统一升级为 `0.11.13`。
- Modify `README.md`、`CHANGELOG.md`
  - 记录用户可见行为并保留 README 只显示最近三条更新的约定。

### Task 1: 为验证阻塞状态下发精确动作契约

**Files:**
- Modify: `tests/test_account_health.py:196-214`
- Modify: `app/services/account_health.py:338-362`

**Interfaces:**
- Consumes: `operational_status_payload(platform, snapshot)` 返回的 `state`。
- Produces: `snapshot_to_source_card(platform, snapshot)` 的可选 `actions: list[dict[str, Any]]`。
- Produces for verification: `[{"kind": "external", "label": "打开官网", "href": platform_url}, {"kind": "api", "label": "已验证", "endpoint": "/api/tasks/missing-3mf/verification-verified", "method": "POST", "body": {"platform": normalized_platform}}]`。
- Preserves: 所有卡片原有的 `url`、`action_label`、诊断字段和非验证状态回退动作。

- [ ] **Step 1: 将验证状态卡测试改为精确的后端契约断言**

将 `tests/test_account_health.py` 中的 `test_snapshot_to_source_card_keeps_verification_status_without_manual_actions` 重命名为 `test_snapshot_to_source_card_exposes_verified_retry_actions`，保留既有 `state`、`status`、`detail`、`action_label` 和 `url` 断言，并把末尾断言替换为：

```python
        self.assertEqual(card["actions"], [
            {
                "kind": "external",
                "label": "打开官网",
                "href": "https://makerworld.com",
            },
            {
                "kind": "api",
                "label": "已验证",
                "endpoint": "/api/tasks/missing-3mf/verification-verified",
                "method": "POST",
                "body": {"platform": "global"},
            },
        ])
```

在 `test_snapshot_to_source_card_returns_ok_card` 保留 `self.assertNotIn("actions", card)`。再向 `test_snapshot_to_source_card_uses_planned_status_copy` 的每个子测试追加：

```python
                self.assertNotIn("actions", card)
```

在同一测试类新增国内站平台参数断言：

```python
    def test_snapshot_to_source_card_uses_cn_platform_for_verified_retry(self):
        card = account_health.snapshot_to_source_card(
            "cn",
            {"platform": "cn", "status": "verification_required"},
        )

        self.assertEqual(card["actions"][0]["href"], "https://makerworld.com.cn")
        self.assertEqual(card["actions"][1]["body"], {"platform": "cn"})
```

再新增 `three_mf_gate="unknown"`、`three_mf_reason="cookie_updated"` 的卡片断言，确认其 `state` 为 `checking` 且没有 `actions`。这些断言覆盖可归档、Cookie 失效、每日限额、网络异常、未知和检测中状态不会误显示“已验证”。

- [ ] **Step 2: 运行测试，确认当前后端尚未提供该动作**

Run:

```bash
.venv/bin/python -m pytest tests/test_account_health.py -q
```

Expected: FAIL，验证状态卡缺少 `actions` 键。

- [ ] **Step 3: 在状态卡序列化函数中最小化下发动作**

在 `app/services/account_health.py` 的 `snapshot_to_source_card()` 中，保留现有 `card` 字典不变，并在 `return card` 前添加：

```python
    if operational["state"] == "verification_required":
        card["actions"] = [
            {
                "kind": "external",
                "label": "打开官网",
                "href": platform_url,
            },
            {
                "kind": "api",
                "label": "已验证",
                "endpoint": "/api/tasks/missing-3mf/verification-verified",
                "method": "POST",
                "body": {"platform": normalized_platform},
            },
        ]
```

不要在 `dashboardStatus.js` 或前端按 `state` 推断该操作；保留普通卡片通过 `url` 和 `action_label` 生成单一外链操作的兼容行为。

- [ ] **Step 4: 运行后端目标回归**

Run:

```bash
.venv/bin/python -m pytest tests/test_account_health.py tests/test_runtime_diagnostics.py tests/test_web_routes.py -q
```

Expected: PASS，既有验证确认接口仍标记账号可用并提交后台重试。

- [ ] **Step 5: 提交状态卡契约**

```bash
git add app/services/account_health.py tests/test_account_health.py
git commit -m "feat: 添加验证完成归档重试入口"
```

### Task 2: 锁定首页对显式“已验证”动作的消费

**Files:**
- Modify: `frontend/src/lib/dashboardStatus.test.mjs:95-125`

**Interfaces:**
- Consumes: 后端状态卡的 `actions` 数组。
- Relies on existing behavior: `dashboardStatusActions(item)` 保留 `kind`、`label`、`endpoint`、`method` 和 `body`，`DashboardPage.vue` 的 `runStatusAction()` 已处理 API 动作的禁用、反馈和刷新。
- Produces: 前端回归保护，确保传入的验证确认动作未经推断或改写。

- [ ] **Step 1: 将通用显式动作测试替换为验证确认契约**

在 `frontend/src/lib/dashboardStatus.test.mjs` 将 `dashboard status card uses explicit backend actions before inferred recovery actions` 重命名为 `verification status card keeps explicit verified retry action from backend`，把第二个动作和预期值都替换为：

```js
      {
        kind: "api",
        label: "已验证",
        endpoint: "/api/tasks/missing-3mf/verification-verified",
        method: "POST",
        body: { platform: "global" },
      },
```

保留前一个 `external`“打开官网”动作与完整数组断言。不要删除 `cn verification status card does not infer verified retry action`：它证明没有后端 `actions` 时前端不会凭状态文本制造重试操作。

- [ ] **Step 2: 运行前端动作测试**

Run:

```bash
npm --prefix frontend test -- dashboardStatus.test.mjs
```

Expected: PASS。当前通用动作转换器已经支持 `body` 和 API endpoint，此步骤锁定真实后端协议而不新增前端分支。

- [ ] **Step 3: 提交前端契约测试**

```bash
git add frontend/src/lib/dashboardStatus.test.mjs
git commit -m "test: 覆盖首页验证完成重试动作"
```

### Task 3: 发布 v0.11.13 并执行完整验证

**Files:**
- Modify: `VERSION:1`
- Modify: `frontend/package.json:4`
- Modify: `frontend/package-lock.json:3,9`
- Modify: `README.md:7,193-212`
- Modify: `CHANGELOG.md:3`

**Interfaces:**
- Produces: 镜像、服务端 `APP_VERSION` 和前端包的统一版本 `0.11.13`。
- Documents: 仅在浏览器验证拦截时可以通过首页“已验证”恢复同平台验证类 `3MF` 归档任务。

- [ ] **Step 1: 升级版本文件**

将以下文件的版本从 `0.11.12` 统一改为 `0.11.13`：

```text
VERSION
frontend/package.json
frontend/package-lock.json
```

`frontend/package-lock.json` 只更新根 package 的两个版本字段（第 3 行和 `packages[""]` 的 `version`），不得改动依赖解析结果。

- [ ] **Step 2: 更新 README 与完整更新日志**

在 `README.md`：

```markdown
> 当前版本：`v0.11.13`
```

并在“更新记录”首位加入：

```markdown
### 2026-07-16 · v0.11.13

- 首页在“需要浏览器确认”时提供“已验证”操作；完成官网验证后可立即恢复同平台受验证阻塞的 `3MF` 归档任务。
```

保持 README 直接可见区只有 `v0.11.13`、`v0.11.12`、`v0.11.11` 三条，将 `v0.11.10` 移进已存在的 `<details>` 历史记录内。

在 `CHANGELOG.md` 首个版本条目前插入相同的 `v0.11.13` 标题与说明，保留全部历史版本。

- [ ] **Step 3: 运行完整测试与生产构建**

Run:

```bash
.venv/bin/python -m pytest tests/test_account_health.py tests/test_runtime_diagnostics.py tests/test_web_routes.py -q
npm --prefix frontend test
npm --prefix frontend run build
git diff --check
git status --short
```

Expected: 所有 Python、Node 测试和 Vite 生产构建通过；`git diff --check` 无输出；状态中除本任务文件外最多只有原有的 `?? videos/makerhub-intro/output/`。

- [ ] **Step 4: 提交发布变更**

```bash
git add VERSION frontend/package.json frontend/package-lock.json README.md CHANGELOG.md
git commit -m "chore: 发布 v0.11.13"
git status --short
```

Expected: 只保留未跟踪的 `videos/makerhub-intro/output/`，不推送远端、不创建 Git tag；只有用户明确要求“推送”后才进行发布推送与 tag 操作。

## Plan Self-Review

- [x] 设计目标“仅验证状态显示两个动作、点击后台重试、没有二次确认”由 Task 1 的动作契约和既有 `DashboardPage.vue` API 执行器覆盖。
- [x] 非目标“不得用于 Cookie 失效、限额、网络异常或检测中”由 Task 1 的 `actions` 缺失断言覆盖；国内站与国际站的请求体也分别覆盖。
- [x] 既有验证确认接口未被重写，Task 1 的 `test_runtime_diagnostics.py` 回归确保它仍恢复 gate 并提交后台任务。
- [x] 前端不按中文文案推断行为由 Task 2 保留的无显式动作测试和精确 API contract 覆盖。
- [x] 用户可见版本、README 最近三条和完整 CHANGELOG 由 Task 3 覆盖。
- [x] 已检查所有接口名、动作字段、路径和请求体：`snapshot_to_source_card`、`dashboardStatusActions`、`/api/tasks/missing-3mf/verification-verified` 与 `{ platform }` 一致。
- [x] 已检查计划中不存在 `TODO`、`TBD`、`implement later`、`fill in details` 或“Write tests for the above”占位语。
