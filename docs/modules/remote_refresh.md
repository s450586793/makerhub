# 源端刷新

## 职责

- 对已归档模型重新检查源端信息。
- 刷新评论、回复、附件、打印配置、实例详情、媒体引用。
- 检查模型原始链接是否源端删除，并写入远端同步状态。
- 合并新增评论/附件/实例，不覆盖本地已成功下载的 3MF。
- 维护源端刷新计划、批次进度、失败历史和慢模型统计。

## 不负责

- 不创建订阅，不扫描作者/收藏夹。
- 不执行本地导入。
- 不重新归档整个模型目录，除非调用归档 job 获取 fresh meta。
- 不把收藏夹缺失当作源端删除依据。

## 对外契约

### HTTP API

- `GET /api/source-refresh`：源端刷新页主读取入口，返回配置、核心批次状态和 `source_refresh` 投影状态。
- `POST /api/source-refresh/run`：手动触发源端刷新。
- `POST /api/source-refresh/repair`：修复源端刷新投影队列/运行态。
- `POST /api/config/remote-refresh`：保存源端刷新计划配置。配置模型名仍沿用 `remote_refresh`。
- `GET /api/remote-refresh`：旧入口兼容保留，返回 shape 与 `/api/source-refresh` 一致。
- `POST /api/remote-refresh/run`：旧触发入口兼容保留。

前端业务页应优先调用 `/api/source-refresh`。`/api/remote-refresh` 仅用于旧链接、旧客户端和过渡期兼容，不作为新代码首选。

### Service 函数/类

- `RemoteRefreshManager.start()` / `stop()`
- `RemoteRefreshManager.run_once()` / 定时刷新入口
- `RemoteRefreshManager.submit_manual_run()` / 手动刷新入口
- `SourceRefreshTaskManager`：当前对外 manager，继承 `RemoteRefreshManager` 并维护独立的 `source_refresh_queue` / `source_refresh_runs` 投影状态。
- `RemoteRefreshManager._run_batch(..., selected_candidates=..., selected_stats=...)`：核心批次引擎支持由子类预选候选；不要再通过临时替换 `_pick_candidates()` 注入候选。
- `run_source_deleted_check_job()`
- `_merge_comments()`
- `_merge_instances()`
- `_finalize_refreshed_meta()`
- `_update_meta_refresh_error()`

## 数据和目录

- Postgres/JSON state:
  - `makerhub_json_state:remote_refresh_state`：核心批次、调度、resume manifest、batch buffer 摘要状态。
  - `makerhub_json_state:source_refresh_queue`：源端刷新独立队列投影。
  - `makerhub_json_state:source_refresh_runs`：源端刷新独立运行态投影。
  - `makerhub_json_state:missing_3mf`
  - `makerhub_json_state:model_flags`
  - `archive_model_index`
- 文件:
  - `/app/data/<model_dir>/meta.json`
  - `/app/config/state/remote_refresh_batches/`：批次 resume manifest 和临时 batch buffer。
  - `/app/config/logs`：兼容日志目录；运行期业务日志写入 Postgres。

## 常用测试命令

```bash
.venv/bin/python -m unittest tests.test_remote_refresh tests.test_process_jobs tests.test_comment_replies
```

## 修改时不能破坏

- 已下载成功的 3MF 不应因为源端刷新被重复下载或覆盖。
- 评论合并要保留已有回复树，补齐新增内容，不能刷新后评论数量倒退。
- 源端删除标记必须来自可靠检查；不能因为订阅漏扫就标红。
- 单个模型失败不能中断整批刷新。
- 源端返回 HTML/验证页时要保存简短诊断，不把 HTML 原文塞进 UI。
- 刷新完成后要更新模型索引/快照，否则模型库仍显示旧数据。

## 给 Codex 的上下文入口

改源端刷新时，先读：

- `app/services/remote_refresh.py`
- `app/services/source_refresh.py`
- `app/api/remote_refresh_routes.py`
- `app/services/process_jobs.py`
- `app/services/catalog.py` 中 remote sync/评论标准化相关函数
- `frontend/src/pages/RemoteRefreshPage.vue`
