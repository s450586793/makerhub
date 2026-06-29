# 部署 / 更新

## 职责

- 维护 Dockerfile、compose、entrypoint 和运行环境变量。
- 支持 App / Worker / Postgres 三容器部署。
- 在显式挂载 Docker socket 时提供设置页一键更新能力：拉镜像、重建 worker/app、延迟清理旧镜像。
- 检测旧 compose 是否缺少数据库配置，并给出升级保护提示。
- 维护 README 部署说明、compose 示例、iOS 快捷指令下载链接。

## 不负责

- 不决定具体业务抓取规则。
- 不在更新流程里同步执行耗时数据迁移；迁移应由 worker 后台跑并显示状态。
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
  - 数据库迁移状态由 Core/归档索引模块维护。
- Docker:
  - 默认 compose 不挂载 `/var/run/docker.sock`；只有用户显式 opt-in 后，设置页才可直接网页更新。
  - `depends_on` 与 Postgres `healthcheck` 作为高级可选注释保留，默认不启用。
  - 默认目录布局为 `/app/config/{config,logs,state}` 与 `/app/data`，compose 只映射 `/app/config`、`/app/data` 和 Postgres 数据目录。
  - `makerhub-app` 和 `makerhub-worker` 应使用同一镜像版本。

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
- 一键更新应先处理 worker，再处理 app，保证页面尽量可恢复。
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
