# 部署 / 更新

## 职责

- 维护 Dockerfile、compose、entrypoint 和运行环境变量。
- 支持 App / Worker / Postgres / CloakBrowser 四容器部署。
- 在显式挂载 Docker socket 时提供设置页一键更新能力：拉镜像、重建 worker/app、延迟清理旧镜像。
- 检测旧 compose 是否缺少数据库配置，并给出升级保护提示。
- 维护 README 部署说明、compose 示例、iOS 快捷指令下载链接。

## 不负责

- 不决定具体业务抓取规则。
- 不在更新流程里同步执行耗时数据重建；模型索引重建应由 worker 后台跑并显示状态。
- 不主动推送 GitHub；只有用户明确要求“推送”时才执行 `git push`。
- 用户要求发布/推送改动时，必须同步版本号和更新说明：小修复升 patch，小功能/较大功能升 minor，破坏性或迁移密集变更升 major。
- 用户明确要求推送且改动包含用户可见版本时，`main` 推送不是发布完成：必须再推送与根目录 `VERSION` 一致的 annotated `v<VERSION>` tag，等待 GitHub Actions 成功，并确认 GitHub Release 和 `ghcr.io/s450586793/makerhub:v<VERSION>` 镜像存在。只有三者完成，网页更新才会显示该版本。
- 未准备发布的普通 `main` 推送不得修改发布版本号，也不得作为可网页更新版本对外说明。

## 对外契约

### HTTP API

- `GET /api/system/update`
- `GET /api/system/version`
- `POST /api/system/update`

### Service 函数/类

- `get_update_capability()`
- `get_update_status()`
- `request_system_update()`
- `mark_update_started_after_restart()`
- `run_update_helper()`
- `DockerSocketClient`
- `normalize_runtime_resource_config()`

### 默认部署与历史监控兼容

默认使用仓库根目录的 `compose.yaml` 管理完整四容器。发布时推送 `main` 和对应版本 tag；GitHub Actions 验证、构建并发布镜像，将成功发布的版本更新为 `ghcr.io/s450586793/makerhub:latest`。等待构建成功后，在飞牛的 Compose 目录执行：

```bash
docker compose pull
docker compose up -d --remove-orphans
```

App 和 Worker 使用同一镜像，由 Compose 的依赖关系、健康检查和 `restart: unless-stopped` 管理。常规部署和更新不安装、启用额外的冻结监控或发布专用 systemd unit。

`scripts/makerhub_monitor.py` 仅保留为已有冻结部署迁出的兼容工具，不是标准 Compose 的启动或更新依赖。已有实例迁回标准 Compose 时，应停用旧监控，避免镜像或配置变更触发保护停机。迁出前如需使用旧监控的 `stop`，仍须验证监控进程与精确容器身份，只停止所属 App / Worker。

## 数据和目录

- 文件:
  - `compose.yaml`
  - `.env.example`
  - `Dockerfile`
  - `docker/entrypoint.sh`
  - `README.md`
  - `VERSION`
- Postgres/JSON state:
  - 更新状态存于 `makerhub_json_state:system_update`。
  - 数据库索引状态由 Core/归档索引模块维护。
- Docker:
  - `compose.yaml` 是唯一完整部署定义，不再需要额外的浏览器抓取 override。
  - README 必须在快速安装区直接展示完整 Compose；`tests.test_release_contract` 保证展示内容与根目录 `compose.yaml` 一致。
  - 宿主机配置、归档、Postgres 和 CloakBrowser 目录分别由 `MAKERHUB_CONFIG_PATH`、`MAKERHUB_ARCHIVE_PATH`、`MAKERHUB_POSTGRES_DATA_PATH`、`MAKERHUB_CLOAKBROWSER_DATA_PATH` 控制；默认写入 Compose 同目录的 `./data/`。
  - 镜像、端口、时区、并发、超时和日志轮转使用 `compose.yaml` 中的明确默认值；`.env.example` 只保留必填密钥和少量实例覆盖项。
  - `.env.example` 的必填密钥必须保持空值，真实 `.env` 和默认本地 `data/` 必须被 Git 忽略。
  - 默认 compose 不挂载 `/var/run/docker.sock`；只有用户显式 opt-in 后，设置页才可直接网页更新。
  - App / Worker 依赖 Postgres 的 `service_healthy`，App、Worker、Postgres 都必须保留 healthcheck。
  - 默认目录布局为 `/app/config/{config,logs,state}` 与 `/app/data`，compose 只映射 `/app/config`、`/app/data` 和 Postgres 数据目录。
  - `makerhub-app` 和 `makerhub-worker` 应使用同一镜像版本。
  - 所有服务必须限制 Docker 日志文件大小和保留数量，避免长期抓取日志占满宿主机磁盘。
  - 新部署默认 Worker 并发为 `4`，运行时仍必须限制在 `1-4`。
  - CloakBrowser token 必填且 Manager 默认绑定 `127.0.0.1`；仅在可信 LAN 下显式设置绑定地址和公共 URL。
  - 新 App / Worker readiness 和发布组提交成功后，一键更新会 best-effort 删除精确命名为 `makerhub-flaresolverr` 的旧内置容器；共享或自定义 FlareSolverr 不处理。
  - 只有明确设置 `MAKERHUB_TRUSTED_PROXIES` 时才信任反向代理头，拒绝宽泛公网网段。

## 常用测试命令

```bash
.venv/bin/python -m unittest tests.test_self_update tests.test_github_changelog
```

仅在修改历史冻结监控兼容代码时运行：

```bash
.venv/bin/python -m pytest -q tests/test_makerhub_monitor.py tests/test_release_contract.py
```

compose 或入口脚本改动后，至少检查：

```bash
git diff --check
```

## 修改时不能破坏

- 旧单容器、缺少 Postgres 或仍使用旧分散目录挂载的部署不能被网页更新直接推到不可启动状态；必须提示需要改 compose。
- 一键更新按单一发布组切换 App / Worker，避免跨版本服务短暂混用；页面在整组就绪验证完成后恢复。
- 一键更新必须把 App / Worker 视为同一发布组：拉取、启动、HTTP/心跳验证任一环节失败时执行整组回滚，旧容器保留到成功提交后才删除。
- 首次网页更新不能替代旧 compose 迁移：旧镜像没有内置 canonical compose，先手工替换 `compose.yaml` 并启动完整服务，再考虑网页更新。
- 删除旧镜像必须异步/延迟，不能阻塞实例更新。
- Docker socket 未挂载时要显示不可用原因，而不是按钮假装可用。
- App/Worker 资源配置应通过少量清晰环境变量控制，不要制造过多难填配置。
- README 更新记录只展开最新三条，历史更新说明放入折叠区。
- 发布/推送前必须确认根目录 `VERSION`、前端包版本、`CHANGELOG.md` 和 README 当前版本一致。

## 给 Codex 的上下文入口

改 compose、Docker、网页更新、版本/README 时，先读：

- `compose.yaml`
- `Dockerfile`
- `docker/entrypoint.sh`
- `app/services/self_update.py`
- `app/api/config.py` 中 `/system/update` 段落
- `frontend/src/pages/SettingsPage.vue` 中系统更新区域
- `README.md`
