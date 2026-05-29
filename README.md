<p align="center">
  <img src="app/static/img/makerhub-logo.png" width="160" alt="MakerHub logo">
</p>

# MakerHub

> 当前版本：`v0.8.6`
>
> MakerHub 基于 [mw_archive_py](https://github.com/sonicmingit/mw_archive_py) 的抓取思路二次重构而来，感谢原作者 [sonicmingit](https://github.com/sonicmingit) 的开源分享。

MakerHub 是一个面向个人 NAS、DSM、Unraid、Portainer 和自托管服务器的 MakerWorld 本地归档系统。它不是公开模型站，而是把你关注、收藏、下载和本地积累的模型统一整理成自己的 MakerWorld 私有资料库。

你可以归档单个模型，也可以批量归档作者页、收藏夹、合集；可以创建订阅定时同步新模型；也可以把手机、网页或本地文件夹里的 `3MF`、`STL`、`STEP`、`OBJ`、压缩包和附件导入 MakerHub。V0.7.0 开始推荐使用 App / Worker / Postgres 三容器部署：App 负责页面和 API，Worker 负责后台抓取、整理和迁移，Postgres 保存结构化配置、任务状态、业务日志和模型卡片索引。

## 当前重点

- 数据库化运行状态：配置、Cookie / Token、订阅、来源库 metadata、任务状态、分享记录、限流状态、系统更新状态和业务日志进入 Postgres。
- 模型索引入库：归档模型卡片索引写入 `archive_model_index`，模型库、订阅库和来源库读取更稳定。
- 三容器部署：默认 Compose 调整为 `makerhub-app`、`makerhub-worker`、`makerhub-postgres`。
- 历史数据迁移：首次连接数据库后自动迁移旧 JSON 状态、历史日志和模型索引；设置页保留手动重建数据库索引与历史信息补全入口。
- 内置浏览器验证：`3MF` 下载遇到 MakerWorld 验证时，可从首页或任务页进入验证页面，完成后由 Worker 自动继续下载。
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
mkdir -p /volume4/docker/docker/makerhub/{config,logs,state,postgres}
mkdir -p "/volume2/entertainment/3D打印/makerhub/local"
```

目录含义：

- `/app/config/config`：运行配置和旧配置迁移输入。
- `/app/config/state`：旧任务状态、marker、系统状态迁移输入、上传暂存、预览队列 marker 和少量运行临时状态。
- `/app/config/logs`：旧日志迁移输入；新业务日志写入 Postgres。
- `/app/data`：归档模型、图片、附件、模型文件和历史 `meta.json`。旧 DSM 模型目录可以继续直接放在这里。
- `/app/data/local`：本地导入和整理入口。
- `/var/lib/postgresql/data`：Postgres 数据库目录。

默认 compose 会把宿主机 `/volume4/docker/docker/makerhub` 映射到容器 `/app/config`，这样旧版平级的 `config`、`logs`、`state` 会直接对应到 `/app/config/config`、`/app/config/logs`、`/app/config/state`。

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
      - /volume4/docker/docker/makerhub:/app/config
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
      - /volume4/docker/docker/makerhub:/app/config
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

`/app/logs`、`/app/state`、`/app/archive`、`/app/local` 默认不再单独映射。新业务日志写入 Postgres；旧日志和旧状态保留在宿主机 `/volume4/docker/docker/makerhub/logs`、`/volume4/docker/docker/makerhub/state` 时，容器内会分别显示为 `/app/config/logs`、`/app/config/state` 并参与迁移。

## 从旧版升级

如果你之前是单容器 `makerhub`，或旧的 `makerhub-api` / `makerhub-web` 双容器，先停掉旧容器释放端口，再按上面的三容器 compose 启动：

```bash
docker rm -f makerhub || true
docker rm -f makerhub-api makerhub-web || true
docker compose pull makerhub-app makerhub-worker makerhub-postgres
docker compose up -d
```

如果设置页提示“需改 compose”，说明当前容器缺少 `MAKERHUB_DATABASE_URL`、`makerhub-postgres`，或仍使用旧 `/app/archive`、`/app/local` 分散挂载。请先升级 compose，再使用网页一键更新。

旧部署如果原来把模型直接放在宿主机 `/volume2/entertainment/3D打印/makerhub` 根目录下，不需要移动历史模型目录。继续把这个目录映射到容器 `/app/data` 即可；本地导入入口保留在宿主机 `/volume2/entertainment/3D打印/makerhub/local/`，容器内就是 `/app/data/local`。

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

### 2026-05-29 · v0.8.6

- 修复浏览器验证轻量弹窗直达 `/browser-verification/:sessionId` 时后端返回 404 JSON 的问题。
- 后端 Web 路由现在会把验证弹窗地址交给前端 SPA，并对该路径禁用缓存，避免旧页面残留。
- 增加登录态直达验证弹窗的回归测试。

### 2026-05-29 · v0.8.5

- 3MF 浏览器验证页改为独立轻量窗口，不再嵌在主工作台侧边栏布局里。
- 首页和归档任务入口会优先打开验证专用弹窗；弹窗被拦截时才回退到当前页跳转。
- Worker 打开验证时优先进入缺失项的 `/f3mf` 下载接口，不再加载完整模型详情页；缺少接口地址时会直接报错，不启动浏览器。
- 降低验证浏览器 viewport 和截图刷新频率，并增加轻量 Chromium 启动参数，减少验证任务占用。

### 2026-05-29 · v0.8.4

- 修复浏览器验证会话复用同一 Chromium profile 时可能互相阻塞的问题，同平台已有验证窗口时会复用当前会话。
- 修复数据库索引重建进入历史迁移阶段时设置页长期显示 `索引中 0/0` 的问题，索引任务会先写入可见进度。
- 清理订阅库里历史遗留的 `@user_数字` 作者订阅错误项，避免这些源站 404 链接反复同步失败。
- 增加浏览器验证 profile 复用、索引进度写入和历史订阅清理回归测试。

<details>
<summary>历史更新记录</summary>

### 2026-05-29 · v0.8.3

- 修复 Worker 在数据库索引/历史信息补全长任务中同步阻塞主循环，导致新建浏览器验证会话一直停在“等待 worker 打开验证浏览器”的问题。
- 浏览器验证输入现在只在远程画面已运行时接收，避免排队态鼠标事件持续写入会话状态。
- 新增 worker 接收浏览器验证会话的业务日志，后续线上排查可以直接区分“会话已创建”和“worker 已接管”。
- 增加浏览器验证 worker 轮询、排队态输入保护和 profile backfill 异步执行回归测试。

### 2026-05-29 · v0.8.2

- 修复线上账号关注作者自动导入订阅时保留 MakerWorld `/en/` 作者页链接的问题，统一保存为 `/zh/@作者/upload`。
- 修复从 MakerWorld 分享入口导入的作者订阅保留 `appSharePlatform=copy` 等分享参数的问题，订阅库显示和访问使用干净短链接。
- 已保存的旧作者订阅链接会在订阅列表/状态初始化时自动写回规范化地址，不需要手动删除重建。
- 增加 URL 规范化、关注作者自动导入和旧订阅自修回归测试。

### 2026-05-28 · v0.8.1

- 修复订阅同步后每个订阅都会全量重建来源卡快照的问题，降低 Worker CPU 占用并减少订阅库预览长期灰块。
- 来源卡快照刷新支持按当前订阅限域执行，占位快照不再作为真实预览图返回。
- 首页源站状态卡主体不再整卡打开 MakerWorld；历史缺失 `3MF` 失败会引导进入任务页，官网入口只保留在明确的动作文字上。
- 修复 App 配置多个 Web worker 时，一键更新重启状态和旧镜像清理可能被多个 worker 重复写入的问题。
- 增加自更新、订阅来源卡、来源健康卡和首页状态卡回归测试。

### 2026-05-28 · v0.8.0

- 新增内置浏览器验证流程，下载 `3MF` 遇到 MakerWorld 验证时可从首页或任务页进入验证页面，完成后由 Worker 继续原下载流程。
- 验证证明只在内存中以一次性 proof id 传递，不写入数据库、状态文件、API 响应或前端页面。
- Worker 镜像默认安装浏览器运行依赖，现有 App / Worker / Postgres 架构内即可完成验证，不需要新增容器。
- 模型卡片和详情链接补充短链接展示与复制能力，归档索引增加对应字段和回归测试。

### 2026-05-27 · v0.7.11

- 修复批量归档父任务在缺失 3MF 重试队列中反复恢复、导致页面显示 0 个运行中 / 多个排队中的问题。
- 批量任务刷新时优先识别仍在运行或排队的子任务，再判断模型是否已归档，避免已归档模型的补 `3MF` 任务被误判为完成。
- 增加回归测试，覆盖已归档模型仍有缺失 `3MF` 子任务排队的场景。

### 2026-05-27 · v0.7.10

- 修复手动重试缺失 `3MF` 时只清除暂停标记、没有重置 MakerHub 当日自动下载计数，导致已达自动下载上限后重试看起来无反应的问题。
- 首页源站状态不再把账号正常时的历史缺失 `3MF` 验证失败误报为当前国际区账号需要验证，改为提示历史失败待重试。
- 任务页和设置页文案区分 MakerHub 自动下载保护额度与 MakerWorld 账号手动下载限制，并给“全部重试”增加提交中状态。

### 2026-05-27 · v0.7.9

- 修复源端刷新页收到每条状态事件都立即请求 `/api/remote-refresh`，导致 Web App 和 Postgres 在源端刷新期间 CPU 升高的问题。
- 源端刷新页改为合并状态事件并按运行态节流刷新，运行中最多约 5 秒刷新一次，结束/空闲状态约 1.2 秒内刷新。
- 修复网页一键更新复用旧容器配置时可能把 Worker 的 Docker 命令保留为 `app` 的问题，替换容器会按角色固定启动命令与运行环境。

### 2026-05-27 · v0.7.8

- 修复 Docker 发布流水线在 GitHub codeload 短暂失败时卡在 `docker/setup-qemu-action`，导致 GHCR 没有产出新版本镜像的问题。
- 发布流程移除当前未使用的 QEMU 初始化步骤，保留 Buildx 构建和 GHCR 推送，降低网页更新拉不到新镜像的概率。
- 重新触发镜像发布，让归档索引后台重建修复可以通过网页更新获取。

### 2026-05-26 · v0.7.7

- 修复归档模型数据库索引过期时，Web 容器会在请求路径回退扫描全量 `meta.json` 导致 CPU 升高的问题。
- Web 发现索引与归档文件不一致时，现在只提交 worker 后台重建信号，并继续使用现有数据库快照返回页面数据。
- Worker 新增“只重建数据库索引”模式，索引修复不会顺带触发历史信息补全扫描。

### 2026-05-26 · v0.7.6

- 修复网页一键更新在群晖 Container Manager 中把 App / Worker 显示为 `*-replacement-*` 容器名的问题。
- 更新流程改为先把旧容器改为备份名，再用正式容器名直接创建新容器，避免群晖记录临时创建名。
- 新容器启动后会复核 Docker 容器名称，名称异常时会触发失败回滚，避免更新状态误报成功。

### 2026-05-26 · v0.7.5

- 修复关注作者同步会把 MakerWorld 内部 `uid` 拼成 `@user_数字/upload` 作者订阅的问题，避免生成无法打开的订阅链接。
- 关注来源同步现在只导入解析到真实公开 handle 的作者页；仅有内部 uid 的关注项会跳过，不再误加入订阅库。
- 下次账号关注同步会自动清理此前由账号同步导入的无效 `@user_数字` 作者订阅，手工创建的订阅不会被误删。
- 订阅同步状态会记录跳过数量和清理数量，方便排查关注数与实际订阅数不一致。

### 2026-05-26 · v0.7.4

- 前端运行状态刷新改为后端状态事件驱动，归档、订阅扫描、来源刷新、本地整理和更新状态完成后主动通知页面刷新。
- 移除首页、任务页、订阅页、来源库、整理页和设置系统页里的固定时间轮询，系统更新与信息补全改为打开系统设置页和收到状态事件时查询。
- 新增 `/api/events/state` 统一状态事件流，并保留归档完成事件兼容旧入口。
- 修复模型库分组路由参数为空时仍请求 `/api/source-library/sources//` 的无效接口问题。

### 2026-05-26 · v0.7.3

- 默认 Compose 将宿主机 MakerHub 配置父目录映射到 `/app/config`，App 和 Worker 共享旧版 `config`、`logs`、`state` 平级目录，方便老用户直接升级。
- 数据库迁移会在数据库仍为空默认状态时，从旧 `/app/state` 补回模型删除标记、订阅状态等运行数据，恢复本地删除和源端删除列表。
- 设置页抓取模式文案改为自动模式、兼容模式、增强模式，避免暴露内部抓取优先级。
- 网页一键更新提示中的 Compose 示例同步为父目录挂载。

### 2026-05-25 · v0.7.2

- 归档模型根目录统一为 `/app/data`，旧 DSM 模型目录不需要再手动移动到 `archive/` 子目录。
- 兼容旧镜像继承的 `MAKERHUB_ARCHIVE_DIR=/app/data/archive` 环境变量，启动时会自动按 `/app/data` 识别模型库。
- 默认本地整理入口 `/app/data/local` 可位于模型库根目录内，避免默认配置被误判为无效。

### 2026-05-25 · v0.7.1

- 修复老用户按新 compose 映射 `/app/data` 后，历史模型仍在 `/app/data` 根目录导致模型库、订阅库和本地库为空的问题。
- 如果旧镜像仍按 `/app/data/archive` 读取，但历史模型仍在 `/app/data` 根目录，会自动兼容旧归档根并恢复历史模型显示。

### 2026-05-25 · v0.7.0

- 版本号升级到 `v0.7.0`，发布为数据库化架构版本。
- 默认部署升级为 App / Worker / Postgres 三容器，Compose 增加 `makerhub-postgres` 和 `MAKERHUB_DATABASE_URL`；`depends_on` / `healthcheck` 作为高级可选注释保留。
- 配置、Cookie / Token、订阅、来源库 metadata、任务状态、分享记录、系统更新状态、业务日志和模型卡片索引迁移到 Postgres。
- 设置页新增数据库索引与历史信息补全状态，可手动重建历史模型索引并查看迁移/补全进度。
- 首页 MW 状态拆分为 `账号` 与 `3MF 下载` 检查项，下载验证、每日上限、接口受限和 Cookie 问题会分别显示。
- 在线账号、Cookie 认证探针和 MakerWorld/Bambu API 请求链路优化，减少普通 HTML/登录页被误判为“需要验证”。
- 系统更新加入 compose 升级保护，旧部署缺少数据库配置时会提示先改 compose，避免网页更新后容器不可用。
- 容器目录收敛为 `/app/config/{config,logs,state}` 与 `/app/data`，默认 compose 将宿主机 MakerHub 配置父目录映射到 `/app/config`，同时映射 `/app/data` 和 Postgres 数据目录。
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

</details>

更多细节见 [CHANGELOG.md](CHANGELOG.md)。
