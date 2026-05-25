# 任务 / Worker / 资源控制

## 职责

- 维护归档队列、缺失 3MF、整理任务、订阅状态、源端刷新状态、模型 flags。
- worker 主循环负责拉起归档、订阅、本地整理、来源库、源端刷新、封面生成等后台任务。
- 提供任务页、首页状态卡、本地整理进度条的数据。
- 控制请求线程池、worker 并发、重任务 nice 值和运行资源配置。
- 清理最近失败、整理历史等终态信息。

## 不负责

- 不实现具体抓取/整理业务，只记录和调度状态。
- 不直接渲染前端进度条。
- 不替业务模块决定失败是否可重试。

## 对外契约

### HTTP API

- `GET /api/tasks`
- `POST /api/tasks/recent-failures/clear`
- `POST /api/tasks/organize/clear`
- `GET /api/events/archive`
- 其他业务模块通过任务状态影响首页/模型库/订阅库。

### Service 函数/类

- `TaskStateStore.load_archive_queue()` / `save_archive_queue()`
- `TaskStateStore.enqueue_archive_task()`
- `TaskStateStore.update_active_task()`
- `TaskStateStore.complete_archive_task()`
- `TaskStateStore.clear_archive_recent_failures()`
- `TaskStateStore.load_missing_3mf()` / `update_missing_3mf_status()`
- `TaskStateStore.load_organize_tasks()` / `save_organize_tasks()`
- `TaskStateStore.load_subscriptions_state()` / `save_subscriptions_state()`
- `TaskStateStore.load_remote_refresh_state()` / `save_remote_refresh_state()`
- `TaskStateStore.update_model_flag()`
- `configure_resource_limits()`
- `shutdown_request_threads()`
- `app.worker.main()`

## 数据和目录

- Postgres/JSON state:
  - `archive_queue`
  - `missing_3mf`
  - `organize_tasks`
  - `subscriptions_state`
  - `remote_refresh_state`
  - `model_flags`
  - `three_mf_limit_guard`
  - `three_mf_daily_quota`
  - `archive_repair_status`
  - `archive_profile_backfill_status`
  - `archive_snapshot_marker`
  - `local_preview_queue_marker`
- 旧文件迁移输入:
  - `/app/state/archive_queue.json`
  - `/app/state/missing_3mf.json`
  - `/app/state/organize_tasks.json`
  - `/app/state/subscriptions_state.json`
  - `/app/state/remote_refresh_state.json`
  - `/app/state/three_mf_daily_quota.json`
  - `/app/state/archive_repair_status.json`
  - `/app/state/archive_profile_backfill_status.json`
  - `/app/state/archive_snapshot.marker`
  - `/app/state/local_preview_queue.marker`
  - `/app/logs/*.log`

## 常用测试命令

```bash
.venv/bin/python -m unittest tests.test_task_state tests.test_resource_limiter tests.test_request_threads tests.test_process_jobs
```

跨业务任务改动建议追加相关模块测试，例如归档、本地整理或订阅。

## 修改时不能破坏

- App 容器不应承担重后台任务；重任务应由 worker 运行。
- worker CPU 打满时，App 页面仍应尽量能读取状态。
- 一个任务的状态来源要统一，不能出现首页、本地库、弹窗三个进度互相矛盾。
- 终态任务要能清理；重复/失败/跳过要有可读原因。
- 数据库版本运行期状态以 Postgres 为准；旧 state 文件只用于迁移导入。
- 日志和任务 message 要过滤 HTML 验证页长文本。

## 给 Codex 的上下文入口

改任务页、进度条、worker 卡死、资源配置时，先读：

- `app/services/task_state.py`
- `app/worker.py`
- `app/services/resource_limiter.py`
- `app/services/request_threads.py`
- `app/main.py` startup/shutdown
- `frontend/src/pages/TasksPage.vue`
- `frontend/src/pages/DashboardPage.vue`
- `frontend/src/pages/OrganizerPage.vue` 中整理进度展示
