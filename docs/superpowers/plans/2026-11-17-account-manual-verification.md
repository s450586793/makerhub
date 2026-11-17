# 首页人工验证弹窗 Implementation Plan

> **For agentic workers:** Use the assigned frontend, backend and browser bridge ownership; do not modify another worker's files.

**Goal:** 首页两站验证入口直接显示可人工操作的原浏览器验证页面。

**Architecture:** Vue modal → authenticated Python session worker → persistent Node CDP page. Reuse the current profile, target checks, quota and pacing; clear the Gate only after a real successful authorization response.

**Tech Stack:** Vue 3、FastAPI、Python thread/queue、Puppeteer CDP。

**Spec:** `docs/superpowers/specs/2026-11-17-account-manual-verification-design.md`

## Global Constraints

- 10 分钟过期，单活跃会话，当前登录会话所有权。
- 不注入旧 Cookie、不重启共享 profile、不关闭其他页面。
- Cookie、下载直链和 CDP 凭据仅后端使用。
- 保持 light / dark / auto 主题，使用现有 token。

## Review Focus

- 关闭期间 create 晚到仍需取消，不能遗留锁和页面。
- 验证失败后再次答题仍保持响应监听。
- 账号/profile 或手动暂停变化后不能恢复任务。
- 鼠标与 touch 拖拽结束必须释放按键状态。
- 超时和 bridge 故障必须释放锁，但不能停止 profile。

## Task 1: Browser bridge（root）

- [x] 编写 URL / input / authorization guard 行为测试，确认缺少实现时失败。
- [x] 新增 `app/services/cloakbrowser_manual.mjs`，复用原下载按钮和响应解析器，提供 JSON-lines init/frame/input/authorize/close。
- [x] 执行 Node 定向测试及本地 Chromium fixture。

## Task 2: Python API 与业务状态（backend worker）

- [x] 添加会话所有权、过期、预约、CAS 与资源清理测试。
- [x] 实现 `account_manual_verification.py`、独立 router，首页 action 指向弹窗验证。
- [x] 执行对应 Python 回归，保留既有普通浏览器登录契约。

## Task 3: 首页弹窗（frontend worker）

- [x] 添加缩放坐标、串行请求、关闭/晚到结果测试。
- [x] 新增 `AccountVerificationDialog.vue` 与控制器，接入 Dashboard。
- [x] 执行 frontend tests/build，并检查深浅主题与窄屏。

## Task 4: 集成（root）

- [x] 独立审查接口与状态成功路径，修复发现。
- [x] 业务 minor 版本与更新说明同步；当前请求未要求推送，保留本地。
- [x] 明确报告测试范围与真实外站人工验收边界。

## 验证结果

- Python 相关回归：105 项通过，包含真实路由的管理员会话、API Token 拒绝与 CSRF 检查。
- Frontend 完整回归：480 项通过，无失败；7 项 Chromium 测试在默认环境跳过，指定本地 Chromium 后另行全部通过。
- 本地 Chromium fixture：7 项通过，覆盖错误答案后重试、输入、授权预约、新窗口拦截及初始化期间退出清理。
- Frontend 构建与 v0.22.0 版本一致性检查通过；深色、浅色与手机放大平移已检查。
- 真实外站验证码尚需部署后由用户手工验收；本次仅本地实现与提交，不推送、不部署。
