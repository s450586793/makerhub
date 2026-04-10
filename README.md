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

本地启动：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Docker：

```bash
docker build -t makerhub .
docker run -d --name makerhub -p 9050:8000 makerhub
```
