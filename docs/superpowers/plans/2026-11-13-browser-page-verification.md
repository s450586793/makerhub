# 浏览器页面验证 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将前置 CF 和已有图形验证接入 MakerHub 的同页面 GET 与一次性 3MF 授权流程。

**Architecture:** 页面验证协调器统一检测、等待和有限交互；GET 模块负责浏览器上下文请求、白名单和总时限；bridge 负责页面所有权与 3MF 授权编排。Python 透传独立开关与共享验证预算。

**Tech Stack:** Python、Node.js ES modules、Puppeteer CDP、node:test、pytest。

**Spec:** `docs/superpowers/specs/2026-11-13-browser-page-verification-design.md`

## Global Constraints

- 50 秒共享验证预算；每个挑战最多 2 次交互；只读 GET 最多恢复重试一次；Download 不重复点击。
- 复用平台 profile / 代理，HTTPS 平台白名单，不注入关联 profile 旧 Cookie，不重启共享 profile。
- 测试使用本地 fixture；不触发真实下载，不增加依赖或外部解题服务。

## Review Focus

- 正常 403 和描述中的 CF 文字不得误判：页面检测与 GET 测试覆盖。
- CF 返回在响应体而非当前 DOM：同页 origin 恢复与一次重试测试覆盖。
- 前置验证耗尽预算：3MF 测试确认不启动第二个完整预算。
- 重定向向外域发送认证信息：GET 测试确认网络发出前阻断。
- 取消和失败后用户页面仍存在：target 所有权和 GET 清理测试覆盖。

### Task 1: 页面验证协调器

**Files:** `app/services/cloakbrowser_verification.mjs`；`frontend/src/lib/cloakbrowser-page-verification.test.mjs`；现有 verification 测试。

**Interfaces:** `inspectPageVerificationState(page)` 返回 `{detected, kind}`；`ensurePageVerification(page, options)` 接收 `timeoutMs / passiveWaitMs / allowInteraction / maxAttempts / signal`，返回 `{detected, completed, manual_required, kind, reason, verification}`。

- [x] 写自然通过、误判、超时、交互上限、官方可见 iframe 的失败测试并确认红。
- [x] 复用已有 adapter，实现协调器和严格 iframe 边界。
- [x] 运行对应 node:test 确认绿。

### Task 2: 同页面 GET

**Files:** 新增 `app/services/cloakbrowser_fetch.mjs`、`frontend/src/lib/cloakbrowser-fetch.test.mjs`；集成 `cloakbrowser_bridge.mjs`。

**Interfaces:** `fetchInBusinessPage(options, deps)`，options 接收 `browser / context / platform / targetUrl / headers / cookies / timeoutMs / verificationTimeoutMs / verificationMaxAttempts / allowInteraction`；返回原有 `{status_code, url, content_type, headers, text}`。

- [x] 写 GET、完整 JSON、CF 恢复、重定向、超时和清理失败测试并确认红。
- [x] 用任务页原生 fetch 实现并替换旧隐藏导航链路。
- [x] 运行注入测试及本地 Chromium fixture 确认绿。

### Task 3: 3MF 与 Python 默认值

**Files:** `cloakbrowser_bridge.mjs`、`cloakbrowser_session.py`；对应 Python 测试和新增 bridge flow 测试。

**Interfaces:** Python 传 `auto_verify_challenges / auto_verify_3mf / verification_timeout_ms / verification_max_attempts`；bridge 前后验证累计扣减预算，仅接受站点产生的授权成功响应。

- [x] 写独立开关、前置验证回退、共享时限和一次点击的失败测试并确认红。
- [x] 接入前置页面验证与剩余预算，默认打开两项开关。
- [x] 运行对应 Python / node:test 确认绿。

### Task 4: 版本、文档与验收

**Files:** `.env.example`、`compose.yaml`、`README.md`、`CHANGELOG.md`、`VERSION`、frontend package/lock、`docs/modules/archive.md`。

- [x] 更新默认值、支持范围和人工回退说明，版本为 0.21.0。
- [x] README 仅直接展示最新 3 个版本，Compose 示例保持一致。
- [x] 运行完整 pytest、frontend test/build、版本检查与 diff 检查。
- [x] 独立审查并修复发现的问题；精确核对任务文件后提交，不推送或部署。

## 验收记录

- 完整 pytest 使用独立临时 PostgreSQL：1473 passed、314 subtests passed。随后补充 CF 响应头透传，相关 Python 测试 79 passed、20 subtests passed；响应仍过滤 Cookie 和认证头。
- 启用本地 Chromium 的完整 frontend 测试：378 passed，无跳过；包含约 3 MiB 中文 JSON、跨源 CF、重定向阻断、失败清理和一次授权约束。
- frontend build、版本一致性和 `git diff --check` 通过；Compose 默认值与显式关闭、README 内嵌配置通过契约检查。
- 独立复核确认：未规范化 URL 不向外域发请求；缺少 CORS 头的 CF 可恢复；被动窗口后自然放行可零交互完成。全程未连接生产或触发真实下载，临时测试数据库已销毁。
