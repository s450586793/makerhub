<p align="center">
  <img src="app/static/img/makerhub-logo.png" width="160" alt="MakerHub logo">
</p>

# MakerHub

> 当前版本：`v0.7.0`
>
> MakerHub 基于 [mw_archive_py](https://github.com/sonicmingit/mw_archive_py) 的抓取思路二次重构而来，感谢原作者 [sonicmingit](https://github.com/sonicmingit) 的开源分享。

MakerHub 是一个面向个人 NAS、DSM、Unraid、Portainer 和自托管服务器的 MakerWorld 本地归档系统。它不是公开模型站，而是把你关注、收藏、下载和本地积累的模型统一整理成自己的 MakerWorld 私有资料库。

你可以归档单个模型，也可以批量归档作者页、收藏夹、合集；可以创建订阅定时同步新模型；也可以把手机、网页或本地文件夹里的 `3MF`、`STL`、`STEP`、`OBJ`、压缩包和附件导入 MakerHub。V0.7.0 开始推荐使用 App / Worker / Postgres 三容器部署：App 负责页面和 API，Worker 负责后台抓取、整理和迁移，Postgres 保存结构化配置、任务状态、业务日志和模型卡片索引。

## V0.7.0 重点

- 数据库化运行状态：配置、Cookie / Token、订阅、来源库 metadata、任务状态、分享记录、限流状态、系统更新状态和业务日志进入 Postgres。
- 模型索引入库：归档模型卡片索引写入 `archive_model_index`，模型库、订阅库和来源库读取更稳定。
- 三容器部署：默认 Compose 调整为 `makerhub-app`、`makerhub-worker`、`makerhub-postgres`。
- 历史数据迁移：首次连接数据库后自动迁移旧 JSON 状态、历史日志和模型索引；设置页保留手动重建数据库索引与历史信息补全入口。
- 在线账号状态更清楚：首页会分开显示 `账号` 与 `3MF 下载` 状态，避免把下载验证、每日上限或接口受限误报成账号本身不可用。
- 系统更新更安全：旧 compose 缺少 Postgres 配置时会阻止网页一键更新，并提示先升级 compose。
- 文档重整：补齐架构说明、模块边界、Compose 安装、升级说明和 V0.7.0 更新记录。

## 功能介绍

- 首页工作台：集中展示模型数量、缺失 `3MF`、归档任务、订阅、源端刷新、本地整理、系统状态和 MW 源站状态。
- MakerWorld 归档：支持单模型、作者页、收藏夹、合集批量归档，保存模型信息、图片、附件、评论、打印配置和 `3MF`。
- 订阅与来源库：可定时同步作者、收藏夹、合集；保存 Cookie 之后可同步关注作者、默认收藏夹和关注合集。
- 模型库与详情页：支持搜索、筛选、收藏、已打印、软删除、源端删除标记、评论回复树、附件管理和文件下载。
- 缺失 `3MF` 管理：识别下载失败原因，区分验证、Cookie 失效、Cloudflare、每日上限和源端缺失，并支持重试。
- 本地导入整理：支持 Web 上传、iOS 快捷指令上传和 `/app/data/local` 文件夹监听，自动解析、去重、合并配置并生成本地模型。
- 分享与移动端导入：支持生成分享码、接收分享模型、移动端 Token 上传和 iOS 快捷指令。
- 在线账号与 Cookie：支持国内 / 国际 MakerWorld Cookie、Bambu 在线账号登录辅助、认证探针和代理配置。
- 用户与安全：支持单用户登录、API Token 权限、移动端导入 Token 和公网部署下的基础访问控制。
- 系统更新中心：查看当前版本、GitHub 最新版本、更新记录和更新状态；可信内网可选挂载 Docker socket 启用网页一键更新。
- 日志与诊断：归档、订阅、源端刷新、本地整理、缺失 `3MF`、系统更新等业务日志集中查看，并对敏感信息脱敏。

## 架构

推荐生产部署由三类服务组成：

- `makerhub-app`：FastAPI API、Vue SPA、登录鉴权、轻量写操作和页面数据读取。
- `makerhub-worker`：归档队列、订阅同步、源端刷新、本地整理、数据库迁移、模型索引和封面生成。
- `makerhub-postgres`：结构化配置、JSON 状态、业务日志、模型卡片索引和迁移标记。

数据边界：

- Postgres 保存结构化状态、运行日志、模型卡片索引和跨进程协同状态。
- 文件系统保存模型本体、图片、附件、导入入口、历史 `meta.json` 和本地临时文件。
- 旧版 JSON、marker、日志和 `meta.json` 会作为迁移输入保留；V0.7.0 不会删除用户已有模型文件。

更多内部说明见：

- [架构说明](docs/ARCHITECTURE.md)
- [模块索引](docs/MODULES.md)
- [新版本数据模型与迁移按钮设计](docs/NEW_VERSION_DATA_MODEL_AND_MIGRATION.md)

## Docker Compose 安装

### 1. 准备目录

按自己的 NAS 路径创建目录。下面以 DSM 路径为例：

```bash
mkdir -p /volume4/docker/docker/makerhub/{config,postgres}
mkdir -p "/volume2/entertainment/3D打印/makerhub"/{archive,local}
```

目录含义：

- `/app/config/config`：运行配置和旧配置迁移输入。
- `/app/config/state`：旧任务状态、marker、系统状态迁移输入、上传暂存、预览队列 marker 和少量运行临时状态。
- `/app/config/logs`：旧日志迁移输入；新业务日志写入 Postgres。
- `/app/data/archive`：归档模型、图片、附件、模型文件和历史 `meta.json`。
- `/app/data/local`：本地导入和整理入口。
- `/var/lib/postgresql/data`：Postgres 数据库目录。

默认 compose 只需要映射 `/app/config`、`/app/data` 和 Postgres 数据目录，容器内部会自己创建上面的子目录。

### 2. 使用完整 `compose.yaml`

默认密码直接写在 compose 里。正式使用前建议把下面所有 `makerhub_password_123456` 改成自己的纯英文数字密码，避免使用 `@`、`:`、`/`、`#` 这类需要 URL 转义的字符。

```yaml
services:
  makerhub-app:
    image: ghcr.io/s450586793/makerhub:latest
    container_name: makerhub-app
    ports:
      - "9042:8000"
    environment:
      MAKERHUB_ENTRYPOINT: app
      MAKERHUB_PROCESS_ROLE: app
      MAKERHUB_BACKGROUND_TASKS: "false"
      MAKERHUB_WORKER_CONTAINER_NAME: makerhub-worker
      MAKERHUB_WEB_WORKERS: "1"
      MAKERHUB_DATABASE_URL: postgresql://makerhub:makerhub_password_123456@makerhub-postgres:5432/makerhub
    volumes:
      - /volume4/docker/docker/makerhub/config:/app/config
      - /volume2/entertainment/3D打印/makerhub:/app/data
      - /var/run/docker.sock:/var/run/docker.sock
    # 高级可选：如果希望 App 等 Postgres 健康后再启动，取消下面三行注释，并同时打开 Postgres 的 healthcheck。
    # depends_on:
    #   makerhub-postgres:
    #     condition: service_healthy
    restart: unless-stopped

  makerhub-worker:
    image: ghcr.io/s450586793/makerhub:latest
    container_name: makerhub-worker
    environment:
      MAKERHUB_ENTRYPOINT: worker
      MAKERHUB_PROCESS_ROLE: worker
      MAKERHUB_BACKGROUND_TASKS: "true"
      MAKERHUB_WORKER_CONCURRENCY: "2"
      MAKERHUB_HEAVY_JOB_NICE: "10"
      MAKERHUB_DATABASE_URL: postgresql://makerhub:makerhub_password_123456@makerhub-postgres:5432/makerhub
    volumes:
      - /volume4/docker/docker/makerhub/config:/app/config
      - /volume2/entertainment/3D打印/makerhub:/app/data
    # 高级可选：如果希望 Worker 等 Postgres 健康后再启动，取消下面三行注释，并同时打开 Postgres 的 healthcheck。
    # depends_on:
    #   makerhub-postgres:
    #     condition: service_healthy
    restart: unless-stopped

  makerhub-postgres:
    image: postgres:16-alpine
    container_name: makerhub-postgres
    environment:
      POSTGRES_DB: makerhub
      POSTGRES_USER: makerhub
      POSTGRES_PASSWORD: makerhub_password_123456
    volumes:
      - /volume4/docker/docker/makerhub/postgres:/var/lib/postgresql/data
    # 高级可选：需要配合上面的 depends_on 使用时再打开。
    # healthcheck:
    #   test: ["CMD-SHELL", "pg_isready -U makerhub -d makerhub"]
    #   interval: 10s
    #   timeout: 5s
    #   retries: 10
    restart: unless-stopped
```

### 3. 启动

```bash
docker compose up -d
```

默认访问地址：

```text
http://你的服务器IP:9042
```

默认登录账号密码为 `admin` / `admin`。首次登录后请先到设置页修改密码，再添加 MakerWorld 国内或国际账号。

首次启动数据库版本后，Worker 会自动迁移旧配置、Cookie / Token、订阅、任务状态、来源库 metadata、分享记录、更新状态、历史业务日志和模型卡片索引。迁移不会移动或删除模型文件。

`/app/logs`、`/app/state`、`/app/archive`、`/app/local` 默认不再单独映射。新业务日志写入 Postgres；旧日志如果要迁移，放在宿主机 `/volume4/docker/docker/makerhub/config/logs`，容器内就是 `/app/config/logs`。

## 从旧版升级

如果你之前是单容器 `makerhub`，或旧的 `makerhub-api` / `makerhub-web` 双容器，先停掉旧容器释放端口，再按上面的三容器 compose 启动：

```bash
docker rm -f makerhub || true
docker rm -f makerhub-api makerhub-web || true
docker compose pull makerhub-app makerhub-worker makerhub-postgres
docker compose up -d
```

如果设置页提示“需改 compose”，说明当前容器缺少 `MAKERHUB_DATABASE_URL`、`makerhub-postgres`，或仍使用旧 `/app/archive`、`/app/local` 分散挂载。请先升级 compose，再使用网页一键更新。

旧部署如果原来把模型直接放在宿主机 `/volume2/entertainment/3D打印/makerhub` 根目录下，新布局建议把历史模型目录移动到 `/volume2/entertainment/3D打印/makerhub/archive/`，本地导入入口保留在 `/volume2/entertainment/3D打印/makerhub/local/`。这样容器内路径就分别是 `/app/data/archive` 和 `/app/data/local`。

手动更新命令：

```bash
docker compose pull makerhub-app makerhub-worker makerhub-postgres
docker compose up -d
```

默认 compose 已挂载 Docker socket，因此设置页可以直接执行网页一键更新。这个挂载只建议在可信内网环境使用。

## 本地开发

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
npm --prefix frontend install
npm --prefix frontend run build
uvicorn app.main:app --reload
```

本地开发默认写入仓库下的 `runtime/` 目录。需要完整模拟 Compose/Postgres 时可以直接使用仓库内的 `compose.yaml`，并把其中的宿主机挂载路径改成本机路径。

## iOS 快捷指令

- [下载「推送到 MakerHub」快捷指令](https://raw.githubusercontent.com/s450586793/makerhub/main/docs/%E6%8E%A8%E9%80%81%E5%88%B0%20MakerHub.shortcut)
- 使用前修改快捷指令顶部两个文本动作：`MakerHubToken` 和 `MakerHubBaseUrl`。
- 详细配置见 [MakerHub iOS 快捷指令文档](docs/ios-makerhub-shortcut.md)。

## 更新记录

### 2026-05-25 · v0.7.0

- 版本号升级到 `v0.7.0`，发布为数据库化架构版本。
- 默认部署升级为 App / Worker / Postgres 三容器，Compose 增加 `makerhub-postgres` 和 `MAKERHUB_DATABASE_URL`；`depends_on` / `healthcheck` 作为高级可选注释保留。
- 配置、Cookie / Token、订阅、来源库 metadata、任务状态、分享记录、系统更新状态、业务日志和模型卡片索引迁移到 Postgres。
- 设置页新增数据库索引与历史信息补全状态，可手动重建历史模型索引并查看迁移/补全进度。
- 首页 MW 状态拆分为 `账号` 与 `3MF 下载` 检查项，下载验证、每日上限、接口受限和 Cookie 问题会分别显示。
- 在线账号、Cookie 认证探针和 MakerWorld/Bambu API 请求链路优化，减少普通 HTML/登录页被误判为“需要验证”。
- 系统更新加入 compose 升级保护，旧部署缺少数据库配置时会提示先改 compose，避免网页更新后容器不可用。
- 容器目录收敛为 `/app/config/{config,logs,state}` 与 `/app/data/{archive,local}`，默认 compose 只需映射 `/app/config`、`/app/data` 和 Postgres 数据目录。
- 重写 README、补充功能介绍、Compose 安装方式和 V0.7.0 更新说明。

### 2026-05-22 · v0.6.128

- 网页一键更新新增 compose 升级保护：检测到旧部署缺少 `MAKERHUB_DATABASE_URL` 时，会阻止继续更新。
- 设置页会在旧 compose 下显示“需改 compose”，并给出 App / Worker / Postgres 三容器示例。

### 2026-05-22 · v0.6.127

- 国际区默认收藏夹地址统一为 `makerworld.com/zh/@账号/collections/models`，旧 `/en/` 订阅会自动合并到同一个来源。
- 来源库和订阅库来源卡新增 payload 缓存，缓存过期时先返回旧卡片并后台刷新。
- MakerWorld API / Scrapling 候选路径收敛到当前可用的 Bambu API 地址，减少无效候选造成的 warning。

### 2026-05-22 · v0.6.126

- 默认收藏夹来源卡头像改为使用 Cookie 账号头像。
- 收藏夹卡片头像位置不再用模型封面兜底，模型图只保留在下方预览区。
- `@账号/collections/models` 默认收藏夹来源统一归类为收藏夹，避免 metadata key 跑到合集分组。

更多 V0.7.0 细节见 [CHANGELOG.md](CHANGELOG.md)。
