# MW 官方验证码弹窗

用户已确认：CF 沿用现有自动处理，人工入口只展示 MW 图形验证码，取消整页远程浏览器。

## 设计

- 使用真实 MW 418 响应提供的 GeeTest v4 配置，固定官方 SDK，在隔离 iframe 中由用户解题。
- create/status/complete/delete 会话绑定管理员登录、平台、账号和单个下载目标；公共响应只包含状态和经校验的验证码配置。
- 初始化及提交各自短时占用现有 CB 操作锁；拿到验证码后立即释放页签、Node 进程及操作锁，等待期间只保存内存会话和互斥 lease。
- proof 严格校验五个官方字段，后端生成标准 Base64 header，仅续交原目标。重复回调不重复请求、不重复预约额度。
- 保留 CF 自动流程、节奏和额度限制、账号检查、暂停保护、过期及取消。只有真实下载授权响应通过校验并成功 CAS 清除原 gate 才恢复任务。
- 不支持整页人工浏览器回退；CF 未自动通过、缺少合法目标、登录失效时给明确错误并保留暂停。

## API

`POST /api/config/online-accounts/{platform}/verification` 创建。

`GET /api/config/online-accounts/{platform}/verification/{session_id}` 查询。

`POST /api/config/online-accounts/{platform}/verification/{session_id}/complete` 提交 `{challenge_id, proof}`。

`DELETE /api/config/online-accounts/{platform}/verification/{session_id}` 取消。

公开状态：`preparing/challenge/verifying/completed/failed/cancelled/expired`。
公开字段：`session_id/platform/status/message/expires_at/completed/challenge`。
challenge 为 null 或 `{challenge_id,captcha_id,api_servers?,static_servers?}`。

## 实施与验证

1. [x] 官方配置/票据校验和短请求 helper；已有 pacing 增加只保留原 gate 的选项。
2. [x] 重构会话 manager 与 API，删除长期截图桥接。
3. [x] 官方 SDK iframe、轻量 controller 和紧凑弹窗，删除坐标/文字/画面轮询。
4. [x] 黑盒回归：身份、CSRF、额度、取消、迟到提交、重复回调、CAS、真实授权判断。
5. [x] 前端 SDK 失败/超时/生命周期、真实 Chromium 深浅主题与小屏；全套测试、构建和版本校验。
6. [ ] 更新版本说明，按已授权验证功能的发布流程发布并部署，核对健康、心跳和公网行为。

真实人工解题后的跨源票据接受情况，需要最终人工完成一次；SDK 装载、mock 回归和服务端健康不能替代该证据。

已验证：完整后端 1732 passed、13 skipped（本地未配置 PostgreSQL）、539 subtests；最终取消等待及 manager 定向 145 passed、32 subtests。真实官方 SDK 在隔离的 Chromium 窗口中加载题图，等效 320px 手机宽度未溢出，未代替用户解题或提交票据。

前端最终回归 527 passed、0 failed、1 skipped（已单独验证的官方 SDK 网络测试）；生产构建、`git diff --check` 和 `v0.23.0` 版本校验通过。所有验证 API 请求均有 15 秒上限；关闭会取消在途请求，迟到清理保护新会话复用 ID，不自动重发创建或票据。
