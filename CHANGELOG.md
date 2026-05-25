# 更新说明

## 2026-05-25 · v0.7.0

V0.7.0 是 MakerHub 的数据库化架构版本。这个版本把运行期结构化状态从分散的 JSON/marker/log 文件逐步迁移到 Postgres，同时保留模型文件、图片、附件和历史 `meta.json` 在原归档目录中，方便老用户平滑升级。

### 架构与部署

- 默认 Compose 升级为 `makerhub-app`、`makerhub-worker`、`makerhub-postgres` 三容器。
- 新增 `MAKERHUB_DATABASE_URL` 运行配置，App 和 Worker 共用同一个 Postgres。
- `docker.sock` 挂载默认开启，设置页可直接执行网页一键更新。
- `depends_on` 和 Postgres `healthcheck` 默认注释保留，高级部署需要时可自行打开。
- 默认 compose 直接写入示例数据库密码，不再要求单独创建 `.env`。
- 容器目录收敛为 `/app/config/{config,logs,state}` 与 `/app/data/{archive,local}`，默认 compose 只需映射 `/app/config`、`/app/data` 和 Postgres 数据目录。
- GitHub Actions Docker 发布会继续推送 `latest` 和 `sha` 标签，并增加根目录 `VERSION` 对应的版本标签。

### 数据库化

- 新增 `makerhub_metadata`、`makerhub_json_state`、`makerhub_logs` 等核心表。
- 新增 `archive_model_index` 模型卡片索引，用于模型库、订阅库、来源库和本地库快速读取。
- 配置、Cookie / Token、登录 session、订阅状态、来源库 metadata、分享记录、归档队列、缺失 `3MF`、本地整理任务、源端刷新状态、系统更新状态、配额/限流状态和业务日志迁移到 Postgres。
- 首次连接数据库时会自动导入旧 JSON 状态、历史日志和模型卡片索引。
- 旧 `/app/config/config.json` 会作为兼容迁移输入读取，新版本运行期配置写入 `/app/config/config/config.json` 或数据库。
- 设置页保留“数据库索引与历史信息补全”区域，可手动重建历史模型索引。

### MakerWorld 状态与账号

- 首页 MW 状态拆分为 `账号` 与 `3MF 下载` 检查项。
- 下载验证、每日上限、Cookie 失效、接口受限、Cloudflare 和普通连接异常会按实际问题分别显示。
- 普通 HTML/登录页响应不再直接当成“需要验证”，只有包含验证标记时才提示验证。
- Cookie 请求头补充 Bearer token 兼容，减少认证接口误报。
- 设置页线上账号、Cookie 测试和关注来源同步文案更贴近实际状态。

### 订阅、来源库与任务

- Cookie 保存后的关注作者、默认收藏夹和关注合集状态进入数据库。
- 来源库 metadata、来源快照和订阅扫描状态改为数据库优先。
- 归档队列、缺失 `3MF`、本地整理、源端刷新和模型 flags 改为跨 App / Worker 共享的数据库状态。
- Worker 启动时会执行数据库迁移、模型索引初始化和必要的历史信息补全。

### 系统更新

- 旧 compose 缺少 `MAKERHUB_DATABASE_URL`、Postgres 服务或仍使用旧分散目录挂载时，网页一键更新会阻止继续，并提示先升级 compose。
- App / Worker 同镜像更新流程继续保留，避免只更新一个容器导致版本不一致。
- 更新状态、目标版本和失败原因写入数据库状态，重启后仍可在设置页查看。

### 文档

- 重写 README，补齐功能介绍、架构说明、Docker Compose 安装、旧版升级和更新说明。
- 新增架构与模块文档：`docs/ARCHITECTURE.md`、`docs/MODULES.md`、`docs/modules/*`。
- 新增 `docs/NEW_VERSION_DATA_MODEL_AND_MIGRATION.md`，固定新版本数据模型和迁移按钮方向。

### 升级注意

- 升级到 V0.7.0 前，请先把 compose 改成 App / Worker / Postgres 三容器。
- 默认数据库密码写在 compose 里，正式使用前建议替换为自己的纯英文数字密码。
- 旧宿主机归档根目录需要按新布局整理为 `data/archive` 和 `data/local` 两个子目录，再映射到容器 `/app/data`。
- 模型文件、图片、附件和历史 `meta.json` 不会被迁移过程删除。
- 如果设置页提示“需改 compose”，请先手动更新 compose，再执行网页一键更新。

## 2026-05-22 · v0.6.128

- 网页一键更新新增 compose 升级保护：检测到旧部署缺少 `MAKERHUB_DATABASE_URL` 时，会阻止继续更新。
- 设置页会在旧 compose 下显示“需改 compose”，并给出 App / Worker / Postgres 三容器示例。

## 2026-05-22 · v0.6.127

- 国际区默认收藏夹地址统一为 `makerworld.com/zh/@账号/collections/models`，旧 `/en/` 订阅会自动合并到同一个来源。
- 来源库和订阅库来源卡新增 payload 缓存，缓存过期时先返回旧卡片并后台刷新。
- MakerWorld API / Scrapling 候选路径收敛到当前可用的 Bambu API 地址，减少无效候选造成的 warning。

## 2026-05-22 · v0.6.126

- 默认收藏夹来源卡头像改为使用 Cookie 账号头像。
- 收藏夹卡片头像位置不再用模型封面兜底，模型图只保留在下方预览区。
- `@账号/collections/models` 默认收藏夹来源统一归类为收藏夹，避免 metadata key 跑到合集分组。
