# makerhub

> 此项目由 Codex 生成，并在此基础上继续迭代。

一个从零重建的 MakerWorld 本地归档后台。

设计目标：
- 复用旧项目里已经验证过的爬虫与下载逻辑
- 重写后台结构、配置模型、任务系统和前端页面
- 支持国内 / 国际 Cookie、HTTP 代理、模型归档、通知、缺失 3MF 重下、本地 3MF 整理
- 详情页按目标站做高保真复刻

当前阶段：
- 已完成 FastAPI 基础骨架
- 已完成后台导航与详情页预览骨架
- 已接入旧项目 `archiver.py` 作为可复用抓取核心文件
- 下一步继续补任务队列、配置保存、批量归档和 DSM 部署

运行目录约定：
- `/app/config`：运行配置
- `/app/logs`：应用日志
- `/app/state`：任务状态、JSON/SQLite 等轻量持久化数据
- `/app/archive`：归档模型目录
- `/app/local`：本地模型监测/整理目录

本地启动：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

默认情况下，本地开发环境会把运行数据写到仓库下的 `runtime/` 目录；Docker 容器内则固定使用 `/app/config`、`/app/logs`、`/app/state`、`/app/archive`、`/app/local`。

Docker：

```bash
docker build -t makerhub .
docker run -d \
  --name makerhub \
  -p 9042:8000 \
  -v /volume4/docker/docker/makerhub/config:/app/config \
  -v /volume4/docker/docker/makerhub/logs:/app/logs \
  -v /volume4/docker/docker/makerhub/state:/app/state \
  -v /volume2/entertainment/3D打印/makerhub:/app/archive \
  -v /volume2/entertainment/3D打印/makerhub/local:/app/local \
  makerhub
```

Compose：

```bash
docker compose -f compose.yaml up -d
```
