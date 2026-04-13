# makerhub

> 此项目由 Codex 生成，并在此基础上继续迭代。

一个从零重建的 MakerWorld 本地归档后台。

## 更新记录

### 2026-04-12
- 设置页新增全站主题控制，支持浅色 / 深色 / 自动三档，并可跟随系统暗色模式
- 左侧导航底部新增版本号展示，页面可直接看到当前站点版本
- 模型库页删除顶部说明卡与卡片底部统计条，列表页改为更紧凑的纯筛选 + 卡片布局
- 新增登录页，公网访问默认先进入单用户登录流程
- 增加会话鉴权与 API Token 鉴权，页面、API 与 `/archive` 资源统一受保护
- 设置页新增密码修改与 Token 生成 / 撤销管理
- 归档入口接入作者上传页 / 收藏夹模型页批量扫描，并将模型链接拆分入现有队列
- 缺失 3MF 任务新增“重新下载 / 全部重试”按钮，并改成支持强制重新入队
- Docker 镜像补充 `python-multipart` 依赖，支持登录表单提交

### 2026-04-11
- 修复评论作者字段异常，避免评论区显示字典对象串
- 评论图片改为固定缩略图尺寸，并支持单击放大预览
- 模型卡片补充“采集时间 / 发布时间”双时间展示
- 设置页的国内 / 国际 Cookie 默认掩码显示，并增加显示 / 隐藏按钮
- 补充本地图片失败时的远端回退加载逻辑

### 2026-04-11
- 重构为 `首页 / 模型库 / 设置 / 任务` 四页结构
- 首页改为真实数据展示台，直接读取归档模型、配置和任务状态
- 模型库改为独立卡片页，详情页入口统一为 `/models/{model_dir}`
- 设置页改为单页内标签，合并国内 Cookie、国际 Cookie、HTTP 代理到“连接设置”
- 任务页统一展示归档队列、缺失 3MF、本地整理任务

### 2026-04-10
- 统一运行目录语义为 `/app/config`、`/app/logs`、`/app/state`、`/app/archive`、`/app/local`
- 新增根目录 `compose.yaml`，统一 DSM / Docker 部署入口
- 所有部署示例统一使用 `9042:8000`

### 2026-04-10
- 初始化 `makerhub` 项目骨架
- 建立 FastAPI、基础模板、Dockerfile、GHCR 工作流
- 接入旧项目爬虫桥接层，作为后续归档能力复用入口

设计目标：
- 复用旧项目里已经验证过的爬虫与下载逻辑
- 重写后台结构、配置模型、任务系统和前端页面
- 支持国内 / 国际 Cookie、HTTP 代理、模型归档、通知、缺失 3MF 重下、本地 3MF 整理
- 详情页按目标站做高保真复刻

当前阶段：
- 已完成四页信息架构重构
- 已完成真实归档数据扫描与首页 / 模型库聚合展示
- 已完成设置页和任务页拆分
- 下一步继续接入实际归档队列、批量抓取和 DSM 持续部署验证

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
