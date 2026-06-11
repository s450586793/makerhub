# MakerHub 模块索引

这个索引用于快速判断一个需求属于哪个模块。后续 Codex 会话优先读取对应模块文档，只有跨模块契约变化时再扩展上下文。

| 模块 | 文档 | 负责范围 | 主要 owner files | 常用验证 |
| --- | --- | --- | --- | --- |
| Core / 配置 / 鉴权 / 数据库 / 日志 | [core.md](modules/core.md) | 配置、账号、Cookie、Token、权限、Postgres、JSON 状态、业务日志 | `app/core/*`, `app/services/auth.py`, `app/services/business_logs.py`, `app/services/database_migration.py`, `app/api/auth.py`, `app/api/config.py` 配置段 | `test_auth_guard.py`, `test_config_cookies.py`, `test_database_json_state.py`, `test_business_logs.py` |
| 模型库 / 详情 / 附件 | [model_catalog.md](modules/model_catalog.md) | 模型列表、详情页、评论展示、附件、flags、下载入口、索引读取 | `app/services/catalog.py`, `app/services/archive_model_index.py`, `app/services/model_attachments.py`, `frontend/src/pages/ModelsPage.vue`, `frontend/src/pages/ModelDetailPage.vue` | `test_archive_model_index.py`, `test_model_attachments.py`, `test_model_downloads.py`, `test_comment_replies.py` |
| 本地导入 / 本地整理 | [local_import.md](modules/local_import.md) | Web/iOS/local 文件夹导入、zip/rar/文件夹整理、去重、编辑本地模型、Three.js 封面 | `local_import_upload.py`, `local_organizer.py`, `local_model_edit.py`, `local_preview_worker.py`, `OrganizerPage.vue` | `test_local_import_upload.py`, `test_local_organizer.py`, `test_local_model_edit.py`, `test_mobile_import.py` |
| 归档 / MakerWorld / 下载 / 3MF | [archive.md](modules/archive.md) | 单模型归档、作者/收藏夹/合集批量发现、Scrapling、3MF 下载、缺失 3MF | `archive_worker.py`, `legacy_archiver.py`, `process_jobs.py`, `batch_discovery.py`, `scrapling_fetch.py`, `three_mf.py` | `test_batch_discovery.py`, `test_scrapling_fetch.py`, `test_missing_3mf.py`, `test_three_mf_quota.py` |
| 订阅 / 来源库 | [subscriptions.md](modules/subscriptions.md) | 订阅 CRUD、定时同步、关注作者/默认收藏夹/关注合集导入、来源卡、来源快照 | `subscriptions.py`, `source_library.py`, `batch_discovery.py`, `source_health.py`, subscription pages | `test_subscriptions.py`, `test_source_library.py`, `test_source_health.py` |
| 源端刷新 | [remote_refresh.md](modules/remote_refresh.md) | 已归档模型的评论、附件、实例、源端删除状态刷新 | `source_refresh.py`, `remote_refresh.py`, `app/api/remote_refresh_routes.py`, `frontend/src/pages/RemoteRefreshPage.vue` | `test_source_refresh.py`, `test_remote_refresh.py`, `test_process_jobs.py` |
| 分享 / 移动端导入 | [sharing_mobile.md](modules/sharing_mobile.md) | 分享码、接收分享、iOS 快捷指令上传、移动端 Token | share endpoints in `app/api/config.py`, `local_import_upload.py`, `ShareDialog.vue`, shortcut docs | `test_share_receive_security.py`, `test_mobile_import.py`, `test_upload_limits.py` |
| 任务 / Worker / 资源控制 | [tasks_worker.md](modules/tasks_worker.md) | 归档队列、整理进度、最近失败、worker 主循环、线程/资源限制 | `task_state.py`, `worker.py`, `resource_limiter.py`, `request_threads.py`, `TasksPage.vue` | `test_task_state.py`, `test_resource_limiter.py`, `test_request_threads.py` |
| Runtime State Contracts | [state_contracts.md](modules/state_contracts.md) | Postgres-backed JSON state keys、状态枚举、事件 scope、字段语义、写入频率约束 | `app/services/state_contracts.py`, `app/services/task_state.py`, `app/services/state_events.py`, state-consuming pages | `test_state_contracts.py`, `test_task_state.py`, `test_database_json_state.py` |
| 前端 / 设置页 / 设计系统 | [frontend_settings.md](modules/frontend_settings.md) | Vue shell、页面导航、设置页、主题、线上账号、代理、通用 UI | `frontend/src/*`, `frontend/src/pages/SettingsPage.vue`, `frontend/src/style.css`, `AGENTS.md` | `npm --prefix frontend run build` |
| 部署 / 更新 | [deployment_update.md](modules/deployment_update.md) | Docker compose、app/worker/postgres、网页更新、升级保护、镜像清理 | `compose.yaml`, `Dockerfile`, `docker/entrypoint.sh`, `app/services/self_update.py`, `README.md` 部署段 | `test_self_update.py`, `test_github_changelog.py` |

## 跨模块接口速查

- 模型列表/详情统一从 `app/services/catalog.py` 读取；不要在页面 API 中重新扫描归档目录。
- 任务状态统一走 `TaskStateStore`；不要为新长任务单独写一个前端状态源。
- 改 `archive_queue`、`missing_3mf`、`organize_tasks`、`subscriptions_state`、`remote_refresh_state`、`source_refresh_queue`、`source_refresh_runs` 等状态字段前，先看 [Runtime State Contracts](modules/state_contracts.md)。
- 本地导入、移动端导入、local 文件夹监听都应复用本地整理/导入链路；不要再写一套 zip/STL/3MF 分类逻辑。
- 归档和订阅批量发现都依赖 `batch_discovery.py`；MakerWorld 接口变化应优先修这里。
- 来源库页面应通过 `source_library.py` 聚合模型、订阅状态和来源 metadata；不要让前端逐模型拼来源卡。
- Cookie、Token、分享码、公网地址、下载签名等敏感值统一由 Core/鉴权/分享模块处理，日志必须脱敏。
- 数据库迁移、compose 形态、README 更新和版本号属于部署模块；只有用户明确要求才推送 GitHub，发布/推送前要同步版本号和更新说明。

## 新需求归属判断

- “页面很慢、模型卡片、详情页展示、评论不显示”：先看模型库模块。
- “上传、zip、rar、文件夹、iOS 快捷指令、本地编辑”：先看本地导入模块；涉及分享码再看分享模块。
- “订阅库 error、关注作者、收藏夹数量、来源卡头像”：先看订阅/来源库模块。
- “归档失败、Cookie 失效、3MF 下载、MakerWorld 验证”：先看归档模块，再看 Core 的 Cookie/代理。
- “源端删除、刷新评论/附件/配置”：先看源端刷新模块。
- “CPU 100%、后台卡住、进度条不一致”：先看任务/Worker 模块，并检查对应业务模块。
- “设置页、Token 页面、线上账号、代理、深色模式”：先看前端/设置页和 Core。
- “web 更新、compose、Postgres、镜像”：先看部署/更新模块。
