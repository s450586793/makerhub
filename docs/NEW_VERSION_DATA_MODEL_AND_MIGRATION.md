# MakerHub 数据模型与数据库索引重建说明

本文记录当前 MakerHub 的数据归属和后续模型元数据入库方向。旧版“JSON 状态 / 历史日志文件导入 Postgres”的过渡迁移层已经移除，设置页不再保留旧的数据库索引与历史缺失信息补全入口。

## 当前数据归属

| 数据 | 运行期主位置 | 文件系统角色 | 说明 |
| --- | --- | --- | --- |
| 全局配置、Cookie、线上账号、代理、主题、资源限制 | Postgres `makerhub_json_state:app_config` | `/app/config/config` 仅兼容挂载 | 运行期不再从旧 `config.json` 回填 |
| 登录 session、API Token、权限 | Postgres JSON state | 无运行期文件后备 | 多容器部署必须数据库一致 |
| 归档队列、缺失 3MF、整理任务、源端刷新状态 | Postgres `makerhub_json_state` | `/app/config/state` 只放锁、暂存、manifest、备份和临时文件 | App 和 Worker 共享同一状态 |
| 订阅配置、订阅扫描状态、来源库 metadata | Postgres `makerhub_json_state` | 来源快照文件按需保存在状态/快照目录 | 来源库页面通过后端聚合 |
| 业务日志 | Postgres `makerhub_logs` | `/app/config/logs` 仅兼容挂载 | 页面日志从数据库读 |
| 模型卡片、来源、作者、封面、时间、搜索字段 | Postgres `archive_model_index` | `meta.json` 仍可作为历史归档目录里的索引扫描来源 | 模型库列表优先读数据库索引 |
| 模型文件、图片、附件、3MF/STL/STEP/OBJ/PDF/Excel | 文件系统 `/app/data` | 主存储 | 数据库只保存引用路径和结构化索引 |
| 本地导入入口、整理中目录、临时上传目录 | 文件系统 `/app/data/local` 和 `/app/config/state` | 主存储 | 数据库只记录进度和结果 |

## 已删除的旧过渡逻辑

- 不再提供旧 JSON/log 文件导入 service。
- 数据库索引重建不再导入旧 JSON 状态或旧日志文件。
- `JsonStore` 在数据库模式下不再读取旧 `config.json` 回填 Cookie。
- `TaskStateStore` 对未登记的状态路径会报错，不再静默写回 JSON 文件。
- Runtime Engine 不再保留旧运行状态迁移 marker。

这些规则的目的很直接：Postgres 是运行期结构化状态和业务日志的唯一来源；旧文件不能再次覆盖线上数据库状态。

## 数据库索引重建

数据库索引重建执行的是模型索引重建，不是旧状态迁移：

1. 检查 Postgres 配置、驱动和连接可用性。
2. 遍历 `/app/data/**/meta.json`，标准化为模型卡片数据。
3. 写入或更新 `archive_model_index`。
4. 写入 bootstrap marker 和进度状态。
5. 可选继续扫描缺失详情、媒体或评论回复字段的历史模型，并提交后台补全任务。

重建过程不会移动或删除模型文件、图片、附件和历史 `meta.json`。

## 后续模型元数据入库方向

短期继续复用 `archive_model_index` 存卡片索引。后续如果推进完整模型元数据入库，可以扩展 `archive_model_index` 或新增更明确的 `makerhub_models` / `makerhub_model_details` 表。

目标字段应覆盖：

| 字段/数据 | 用途 |
| --- | --- |
| `model_dir` | 文件资产目录引用 |
| `source`、`source_model_id`、`origin_url` | 来源和源端身份 |
| `title`、`description`、`author`、`tags` | 模型展示和搜索 |
| `cover_asset_path`、`image_asset_paths` | 图片文件路径引用 |
| `files` | 3MF/STL/STEP/OBJ 等文件清单和配置关系 |
| `attachments` | 附件元数据和文件路径引用 |
| `comments`、`rating`、`stats` | 详情页和源端刷新数据 |
| `flags` | 收藏、已打印、本地删除、源端删除等状态 |
| `raw_source_payload` | 必要时保存源端原始结构，方便后续字段演进 |

文件本体仍在 `/app/data`，数据库只保存路径和结构化信息。

## 修改约束

- 新运行状态必须写入 Postgres，不能新增旧 JSON state 文件分支。
- 新业务日志必须写入 `makerhub_logs`，不能新增旧 log 文件写入分支。
- 数据库不可用时要明确报错，不能静默退回旧文件运行期。
- 模型资产仍保存在文件系统；图片、附件和模型文件只在数据库中保存引用。
- 历史 `meta.json` 的读取只能用于索引重建、兼容详情和后续有计划的模型元数据导入，不得重新变成运行期状态来源。
