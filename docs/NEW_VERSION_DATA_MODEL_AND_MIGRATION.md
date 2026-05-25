# MakerHub 新版本数据模型与迁移按钮设计

本文用于固定 MakerHub 下一阶段的架构方向：新版本按 Web 前端、后端 API、后台 Worker、Postgres 数据库和文件资产目录分层。旧版本历史数据通过设置页里的迁移按钮导入，不要求用户手工搬文件，也不把模型大文件塞进数据库。新版本不再生成或维护模型目录下的 `meta.json`；模型结构化元数据直接写入数据库。

## `meta.json` 在新版本里的定位

每个模型目录里的 `meta.json` 是旧版本留下的模型本地元数据文件，例如标题、作者、来源地址、图片、实例文件、附件、评论缓存、源端删除标记等。旧版本大量功能直接读写它，所以它过去接近“模型主数据”。

新版本的目标不是继续保留 `meta.json` 兼容副本，而是把它降级为一次性迁移输入：

- 老用户升级时，迁移按钮扫描旧 `/app/archive/**/meta.json`，把里面的结构化字段导入 Postgres。
- 迁移成功后，运行期不再从 `meta.json` 读取模型元数据。
- 新归档、新本地导入、新编辑、新源端刷新都直接写数据库，不再写新的 `meta.json`。
- 旧 `meta.json` 可以暂时留在磁盘上作为历史文件，但新代码不依赖它，也不把它当恢复源。

因此，新规则更简单：Postgres 是模型结构化元数据的唯一运行期主源；`meta.json` 只属于旧版本迁移阶段。

## 目标架构

- Web 前端：Vue SPA，只负责展示、交互、进度轮询和提交操作。
- 后端 API：FastAPI，负责鉴权、参数校验、轻量查询、迁移按钮入口和状态读取。
- Worker：负责耗时任务，包括历史数据迁移、模型索引重建、归档、订阅同步、本地整理和缺失信息补全。
- Postgres：保存结构化状态、索引、日志、迁移记录和跨进程协同状态。
- 文件系统：保存模型本体、图片、附件、导入暂存目录和生成文件，不保存新模型元数据 JSON。

## 数据归属

| 数据 | 新版本主位置 | 文件是否保留 | 说明 |
| --- | --- | --- | --- |
| 全局配置、Cookie、线上账号、代理、主题、资源限制 | Postgres `makerhub_json_state:app_config` | 旧 `config.json` 只做迁移输入 | 运行期不再依赖旧配置文件 |
| 登录 session、API Token、权限 | Postgres JSON state 或后续独立表 | 旧 state 文件只做迁移输入 | 后续多 App 容器时必须数据库一致 |
| 归档队列、缺失 3MF、整理任务、源端刷新状态 | Postgres `makerhub_json_state` | 旧 state 文件只做迁移输入 | App 和 Worker 共享同一状态 |
| 订阅配置、订阅扫描状态、来源库 metadata | Postgres `makerhub_json_state` | 旧 state 文件只做迁移输入 | 来源库页面不应前端拼数据 |
| 业务日志 | Postgres `makerhub_logs` | 历史 `/app/logs/*.log` 只做迁移输入；可继续镜像写文件 | 页面日志从数据库读 |
| 模型卡片、来源、作者、封面、时间、搜索字段 | Postgres `archive_model_index` 或后续模型表 | 老 `meta.json` 只做迁移输入 | 模型库列表直接读数据库 |
| 模型详情完整结构 | Postgres JSONB 字段或后续详情表 | 老 `meta.json` 只做迁移输入 | 新归档和新编辑直接入库 |
| 模型文件、图片、附件、3MF/STL/STEP/OBJ/PDF/Excel | 文件系统 `/app/archive` | 是 | 不写入数据库 |
| 本地导入入口、整理中目录、临时上传目录 | 文件系统 `/app/local` 和临时目录 | 是 | 数据库只记录进度和结果 |
| 迁移运行记录、失败项、重试状态 | Postgres，建议新增迁移运行表 | 可选导出日志 | 用于按钮进度、失败重试和审计 |

## 当前已有基础

当前代码已经具备一部分目标形态：

- `makerhub_metadata`：保存数据库 schema 版本、迁移标记和索引状态。
- `makerhub_json_state`：保存结构化 JSON 状态。
- `makerhub_logs`：保存结构化业务日志。
- `archive_model_index`：当前已保存模型卡片索引、`model_json`、`meta_json` 和旧 `meta.json` 文件签名；新版本应把它演进为模型元数据主表或拆出正式模型表。
- `migrate_json_files_to_database()`：把旧配置、任务状态、session、订阅、marker 等 JSON/marker 状态导入数据库。
- `migrate_log_files_to_database()`：把历史日志导入数据库。
- `rebuild_archive_model_database_index()`：扫描历史模型 `meta.json` 并写入 `archive_model_index`。
- 设置页已有“数据库索引与历史信息补全”区域，但按钮文案和迁移语义可以继续收敛。

## 推荐的新表或状态

短期可以继续复用 `makerhub_json_state` 和 `archive_model_index`，但新版本目标应明确：新模型元数据直接入库，不再写回 `meta.json`。为了让迁移按钮更稳，建议新增迁移运行记录，而不是只把状态塞进一个 JSON blob。

### 模型主数据表方向

可以先扩展 `archive_model_index`，也可以新增更明确的 `makerhub_models` / `makerhub_model_details`。无论采用哪种实现，目标字段应覆盖旧 `meta.json` 的结构化内容：

| 字段/数据 | 用途 |
| --- | --- |
| `model_dir` | 文件资产目录引用，不再代表元数据文件路径 |
| `source`、`source_model_id`、`origin_url` | 来源和源端身份 |
| `title`、`description`、`author`、`tags` | 模型展示和搜索 |
| `cover_asset_path`、`image_asset_paths` | 图片文件路径引用 |
| `files` | 3MF/STL/STEP/OBJ 等文件清单和配置关系 |
| `attachments` | 附件元数据和文件路径引用 |
| `comments`、`rating`、`stats` | 详情页和源端刷新数据 |
| `flags` | 收藏、已打印、本地删除、源端删除等状态 |
| `raw_source_payload` | 必要时保存源端原始结构，方便后续字段演进 |

文件本体仍在 `/app/archive`，数据库只保存路径和结构化信息。

### `makerhub_migration_runs`

记录一次迁移动作。

| 字段 | 用途 |
| --- | --- |
| `id` | 迁移运行 ID |
| `kind` | `initial_import`、`rebuild_database`、`retry_failed`、`verify` |
| `status` | `queued`、`running`、`completed`、`completed_with_errors`、`failed`、`cancelled` |
| `phase` | 当前阶段，如 `preflight`、`json_state`、`logs`、`model_index`、`verify` |
| `force` | 是否强制重建 |
| `total`、`processed`、`updated`、`skipped`、`failed` | 总进度 |
| `summary` | JSONB 摘要 |
| `started_at`、`finished_at`、`updated_at` | 时间戳 |
| `last_error` | 最近错误，必须脱敏 |

### `makerhub_migration_items`

记录单个迁移项，支持失败后只重试失败项。

| 字段 | 用途 |
| --- | --- |
| `run_id` | 对应 `makerhub_migration_runs.id` |
| `item_type` | `json_state`、`log_file`、`legacy_model_meta`、`marker` |
| `source_path` | 旧文件路径或模型目录 |
| `target_key` | 数据库 key、`model_dir`、模型表主键或日志文件名 |
| `status` | `pending`、`updated`、`skipped`、`failed` |
| `attempt_count` | 重试次数 |
| `error` | 单项错误，必须脱敏 |
| `updated_at` | 最近处理时间 |

这两张表不替代现有迁移逻辑，只是把“进度、失败、重试”从临时状态提升为稳定审计数据。

## 迁移按钮设计

设置页建议把现有区域收敛成“数据库迁移与历史数据”。

### 按钮状态

| 场景 | 主按钮文案 | 其他操作 |
| --- | --- | --- |
| 未配置数据库 | `需要先升级 compose` | 展示 App / Worker / Postgres compose 提示 |
| 数据库可用但未迁移 | `迁移历史数据` | `刷新状态` |
| 正在迁移 | `迁移中...` | 禁用主按钮，允许刷新 |
| 迁移完成 | `校验数据库` | `刷新状态` |
| 部分失败 | `重试失败项` | `重新迁移历史数据` 放到高级确认 |
| 强制重新迁移确认后 | `重新迁移中...` | 禁用重复提交 |

### 页面展示

页面至少展示四块状态：

- 数据库连接：未配置、驱动缺失、可用、schema 版本。
- 迁移阶段：预检、运行状态、日志、模型索引、校验、完成。
- 进度：已处理 / 总数、写入数量、跳过数量、失败数量。
- 失败摘要：显示前 50 条失败项，包含模型目录或文件名、阶段、错误原因、是否可重试。

不要在页面塞太多解释性长文。详细规则放在文档，页面只提供明确状态和动作。

## 迁移流程

### 1. 预检

- 检查 `MAKERHUB_DATABASE_URL` 是否配置。
- 检查 Postgres 驱动和连接可用。
- 初始化核心表和索引表。
- 检查 Worker 是否在运行，或至少能被 App 提交后台任务。
- 检查 `/app/archive`、`/app/config`、`/app/state`、`/app/logs` 是否可读。

预检失败时不进入迁移，页面显示明确原因。

### 2. 导入旧 JSON/marker 状态

调用现有 `migrate_json_files_to_database(force=...)`，导入：

- `app_config`
- `archive_queue`
- `missing_3mf`
- `organize_tasks`
- `model_flags`
- `subscriptions_state`
- `remote_refresh_state`
- `three_mf_limit_guard`
- `cookie_source_sync_state`
- `cookie_source_inventory`
- `source_library_metadata`
- `model_shares`
- `archive_repair_status`
- `archive_profile_backfill_status`
- `system_update`
- `auth_sessions`
- `three_mf_daily_quota`
- `archive_snapshot_marker`
- `local_preview_queue_marker`
- `bambu_studio_download_secret`

规则：默认不覆盖数据库里已存在的 key；只有用户选择强制重建时才覆盖。

### 3. 导入历史日志

调用现有 `migrate_log_files_to_database()`。

规则：

- 按日志行导入，重复行通过 hash 去重。
- 单个日志文件设置上限，避免历史巨量日志拖死迁移。
- 敏感字段必须走现有日志脱敏规则。

### 4. 导入历史模型数据

扫描 `/app/archive/**/meta.json`：

- 读取并标准化模型。
- 写入或更新数据库模型主数据，包括卡片、详情、文件清单、图片引用、附件引用、评论/评分/源端状态等结构化字段。
- 保存旧 `meta.json` 的导入签名到迁移记录，用于判断这份旧文件是否已经导入过。
- 失败项进入 `makerhub_migration_items` 或当前状态里的失败列表。

规则：任何单个模型失败都不能中断全库迁移。

### 5. 校验和标记

- 对比旧 `meta.json` 数量与数据库中已导入的模型数量。
- 如果失败数为 0，写入完成标记。
- 如果存在失败项，状态为 `completed_with_errors`，允许用户重试失败项。
- 刷新模型库快照，让前端读取数据库主数据。

### 6. 可选缺失信息补全

数据库迁移完成后，可以继续执行历史信息补全，例如缺失评论字段、缺失媒体、缺少详情结构的模型。它应该是第二阶段任务，不应阻塞“数据库迁移完成”。

## 重试和回滚原则

- 迁移必须幂等：同一项重复执行应更新或跳过，不能制造重复数据。
- 默认重试只处理失败项。
- 强制重新迁移只重写数据库里的结构化元数据，不删除模型文件。
- 任何迁移失败都不能删除旧 `meta.json`、图片、附件或模型文件。
- 数据库不可用时明确报错，不静默退回旧文件运行期。
- 用户看到“完成”时，应表示结构化运行期数据已可从数据库读取。

## 实施分期

### 第 1 阶段：文案和状态收敛

- 把设置页区域命名收敛为“数据库迁移与历史数据”。
- 主按钮根据状态显示 `迁移历史数据`、`校验数据库`、`重试失败项`。
- 保留现有 API 和 Worker 流程。

### 第 2 阶段：模型元数据直接入库

- 新归档、新本地导入、新编辑、新源端刷新不再写 `meta.json`。
- 后端模型读取从数据库主数据读取，不再把 `meta.json` 作为运行期 fallback。
- 文件系统只保存文件资产，数据库保存模型详情结构。

### 第 3 阶段：迁移运行记录

- 新增 `makerhub_migration_runs` 和 `makerhub_migration_items`。
- `migrate_json_files_to_database()`、`migrate_log_files_to_database()`、`rebuild_archive_model_database_index()` 写入运行记录。
- 设置页从运行记录读取失败项和最近一次结果。

### 第 4 阶段：模型表增强

- 扩展 `archive_model_index` 或新增正式模型表，保存高频查找字段：
  - MakerWorld model id
  - design/profile id
  - origin URL
  - source kind
  - author/profile id
  - 文件 hash
  - config fingerprint
  - 本地导入来源
- 消除重复检测、本地导入和来源聚合里的全量 `meta.json` 扫描。

### 第 5 阶段：旧文件退场策略

- 迁移成功后不主动删除旧 `meta.json`，避免用户误以为模型文件被破坏。
- 后续可以提供“清理旧元数据文件”高级工具，但必须先确认数据库导入完成并给出备份提示。
- 新版本代码不得再创建新的 `meta.json`。

## 验收标准

- 新用户空库启动时能初始化数据库，不需要历史迁移。
- 老用户升级后点击迁移按钮，能看到阶段、进度、失败数和完成状态。
- 历史模型文件、图片、附件不移动、不删除。
- 历史 `meta.json` 能导入数据库模型主数据。
- 新归档和新本地导入不生成 `meta.json`。
- 模型库、详情页、来源聚合、搜索和编辑都从数据库读取结构化元数据。
- 失败项可重试，重试不会重复插入。
- 重新迁移历史数据后模型库能恢复到和旧文件目录一致。
- 数据库未配置时页面提示升级 compose，而不是继续写旧 state 文件。
- 相关测试至少覆盖 JSON 状态迁移、日志迁移、模型索引迁移、失败项重试和设置页状态展示。
