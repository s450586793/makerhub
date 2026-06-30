# MakerHub 架构说明

MakerHub 目前采用“模块化单体 + 后台 worker + Postgres 索引”的结构。代码仍在一个仓库和一个 FastAPI 应用里，但每块业务要通过清晰的 service/API 契约交互，避免后续 Codex 会话为了改一个按钮或一个流程被迫读完整项目。

## 运行形态

- `makerhub-app`：FastAPI API、Vue SPA 静态文件、登录鉴权、页面数据读取、轻量写操作入口。
- `makerhub-worker`：归档队列、订阅同步、源端刷新、本地整理、数据库索引重建、Three.js 封面生成等后台任务。
- `makerhub-postgres`：结构化配置、JSON 状态、业务日志、模型卡片索引等数据。
- 文件目录仍保存大文件本体：图片、3MF/STL/STEP/OBJ、PDF/Excel/附件、归档元数据文件。

## 主要目录

- `app/main.py`：FastAPI 入口、中间件、鉴权守卫、启动/关闭后台管理器。
- `app/api/`：HTTP 路由层。只做请求参数、权限、响应拼装，不应承载复杂业务。
- `app/services/`：业务模块实现。后续修改优先在这里找对应模块。
- `app/core/`：配置目录、数据库连接、JSON store、安全工具、时间工具。
- `app/schemas/`：Pydantic 配置和请求/响应模型。
- `frontend/src/`：Vue SPA、页面、组件、API client、主题和缓存。
- `tests/`：单元/集成级回归测试。
- `docs/modules/`：模块契约文档。新会话优先读对应模块文档。

## 数据分层

- Postgres 存结构化状态和索引：
  - `makerhub_json_state`：配置、鉴权 session、任务状态、订阅状态、模型 flags、更新状态、配额/限流和跨进程 marker 等 JSON 状态。
  - `makerhub_logs`：业务日志运行期主数据。
  - `archive_model_index`：模型库卡片索引，用于减少全量扫描归档目录。
  - `makerhub_metadata`：数据库 schema、索引状态等元信息。
- 文件系统存模型本体和导入入口：
  - `/app/data`：MakerWorld 归档模型、本地模型最终库、图片、模型文件、附件和历史 `meta.json`。
  - `/app/data/local`：本地整理入口、导入候选、整理中和输出目录。
  - `/app/config/config`：兼容配置目录。
  - `/app/config/state`：上传暂存、预览队列 marker、合并备份、必要的进程锁和临时文件。
  - `/app/config/logs`：兼容日志目录；新业务日志写入 Postgres。

数据库版本运行期以 Postgres 为结构化状态和业务日志源；旧 JSON/marker/log 文件不再作为数据库不可用时的运行期后备，也不再有自动导入入口。

运行状态字段、状态枚举、事件 scope 和写入频率约束见 [Runtime State Contracts](modules/state_contracts.md)。涉及 `archive_queue`、`missing_3mf`、`organize_tasks`、`subscriptions_state`、`remote_refresh_state`、`source_refresh_queue`、`source_refresh_runs` 等 JSON state 的改动，应先更新该契约，再修改写入端和前端消费者。

## 模块边界

优先按 `docs/MODULES.md` 和 `docs/modules/*.md` 切分上下文。改一个模块时，先读该模块文档，再读列出的 owner files。只有修改跨模块契约时，才同时读被调用模块文档。

约定：

- API 层调用 service facade，不直接复制 service 内部扫描、去重、抓取逻辑。
- 前端只通过 `frontend/src/lib/api.js` 和已存在页面状态读取 API，不绕过后端拼文件路径。
- 后台任务通过 `TaskStateStore`、业务日志和模块 service 更新进度，不让前端承担重计算。
- 归档、本地导入、移动端导入、local 文件夹监听最终都要落到统一的模型库结构和索引刷新逻辑。
- Cookie、Token、分享码、公网地址等敏感信息不得写入明文日志。
- 大文件只存目录，数据库只存索引、状态、引用路径和必要 metadata。

## 请求与鉴权

- 普通网页通过 session cookie 访问。
- API Token 权限由 `app/services/auth.py` 管理，目前主要权限为：
  - `mobile_import`：移动端/本地导入。
  - `archive_write`：提交归档任务。
  - `models_read`：读取模型库相关 API。
- `app/main.py` 的 `auth_guard` 决定哪些 API 可以用 Token 访问，新增 API 时要确认权限归属。
- `/api/public/*`、分享文件下载、移动端导入探针是公开/半公开入口，新增日志必须脱敏。

## 后台任务

worker 启动后会初始化以下管理器：

- `ArchiveTaskManager`：归档和 3MF 补档队列。
- `SubscriptionManager`：订阅定时同步和 Cookie 关注来源同步。
- `LocalOrganizerService`：本地整理入口扫描和整理 worker。
- `SourceLibraryManager`：来源库快照和来源卡 metadata。
- `SourceRefreshTaskManager`：源端刷新对外 manager，保留 `RemoteRefreshManager` 核心刷新引擎，并维护独立的 `source_refresh_queue` / `source_refresh_runs` 运行态投影。
- 本地 Three.js 封面 worker：处理无图本地模型的预览封面。

前端的进度条和任务卡片应读取统一状态，不要新建只存在前端内存里的长任务状态。

## Codex 工作方式

推荐每次按下面顺序开工：

1. 读 `docs/MODULES.md`，确认模块归属。
2. 读一个对应模块文档，例如 `docs/modules/local_import.md`。
3. 只读模块文档列出的 owner files 和测试。
4. 如果需要改跨模块契约，更新双方模块文档和 `docs/MODULES.md`。
5. 改完跑模块文档列出的测试；跨模块改动再跑更宽的测试。

“统领全项目”的会话负责边界、compose、数据库索引/状态契约、版本/README/发布；普通功能会话尽量只在一个模块内完成。
