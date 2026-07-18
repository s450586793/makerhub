# 部署 / 更新

## 职责

- 维护 Dockerfile、compose、entrypoint 和运行环境变量。
- 支持 App / Worker / Postgres 三容器部署。
- 在显式挂载 Docker socket 时提供设置页一键更新能力：拉镜像、重建 worker/app、延迟清理旧镜像。
- 检测旧 compose 是否缺少数据库配置，并给出升级保护提示。
- 维护 README 部署说明、compose 示例、iOS 快捷指令下载链接。

## 不负责

- 不决定具体业务抓取规则。
- 不在更新流程里同步执行耗时数据重建；模型索引重建应由 worker 后台跑并显示状态。
- 不主动推送 GitHub；只有用户明确要求“推送”时才执行 `git push`。
- 用户要求发布/推送改动时，必须同步版本号和更新说明：小修复升 patch，小功能/较大功能升 minor，破坏性或迁移密集变更升 major。

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

## 数据和目录

- 文件:
  - `compose.yaml`
  - `Dockerfile`
  - `docker/entrypoint.sh`
  - `README.md`
  - `VERSION`
- Postgres/JSON state:
  - 更新状态存于 `makerhub_json_state:system_update`。
  - 数据库索引状态由 Core/归档索引模块维护。
- Docker:
  - `compose.yaml` 是唯一完整部署定义；`compose.external-flaresolverr.yaml` 只能与它合并使用，并且只覆盖外部 FlareSolverr 地址和禁用内置服务的 profile。
  - 默认 compose 不挂载 `/var/run/docker.sock`；只有用户显式 opt-in 后，设置页才可直接网页更新。
  - App / Worker 依赖 Postgres 的 `service_healthy`，App、Worker、Postgres 都必须保留 healthcheck。
  - 默认目录布局为 `/app/config/{config,logs,state}` 与 `/app/data`，compose 只映射 `/app/config`、`/app/data` 和 Postgres 数据目录。
  - `makerhub-app` 和 `makerhub-worker` 应使用同一镜像版本。
  - CloakBrowser token 必填且 Manager 默认绑定 `127.0.0.1`；仅在可信 LAN 下显式设置绑定地址和公共 URL。
  - 只有明确设置 `MAKERHUB_TRUSTED_PROXIES` 时才信任反向代理头，拒绝宽泛公网网段。

## 常用测试命令

```bash
.venv/bin/python -m unittest tests.test_self_update tests.test_github_changelog
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
