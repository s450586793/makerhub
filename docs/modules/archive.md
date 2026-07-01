# 归档 / MakerWorld / 下载 / 3MF

## 职责

- 提交单模型归档任务。
- 对作者页、收藏夹、合集进行批量预扫描和入队。
- 调用 MakerWorld/Bambu API、Scrapling 和必要 fallback 获取模型详情。
- 下载图片、评论、附件、打印配置、3MF 文件。
- 维护缺失 3MF 列表、下载限额、防重复入队和失败原因。
- 支持重建模型索引、修复 3MF 映射。

## 不负责

- 不负责订阅调度本身；订阅只调用本模块发现和入队能力。
- 不负责本地 zip/rar/STL 整理。
- 不负责前端模型卡片布局。
- 不保存 Cookie；只从配置读取对应站点 Cookie。

## 对外契约

### HTTP API

- `POST /api/archive`
- `POST /api/archive/preview`
- `POST /api/tasks/missing-3mf/retry`
- `POST /api/tasks/missing-3mf/retry-all`
- `POST /api/tasks/missing-3mf/cancel`
- `GET /api/admin/archive/repair-3mf`
- `POST /api/admin/archive/repair-3mf`

### Service 函数/类

- `ArchiveTaskManager.submit()`
- `ArchiveTaskManager.preview_batch()`
- `ArchiveTaskManager.ensure_worker_for_pending()`
- `ArchiveTaskManager.resume_pending_tasks()`
- `run_archive_model_job()`
- `run_discover_batch_urls_job()`
- `run_source_deleted_check_job()`
- `discover_batch_model_urls()`
- `resolve_batch_source_name()`
- `normalize_source_url()`
- `fetch_with_scrapling()` / Scrapling helper
- `reserve_three_mf_download_slot()`
- `inspect_3mf_file()` / `resolve_model_instance_files()`

## 数据和目录

- Postgres/JSON state:
  - `makerhub_json_state:archive_queue`
  - `makerhub_json_state:missing_3mf`
  - `makerhub_json_state:three_mf_limit_guard`
  - `archive_model_index`
- 文件:
  - `/app/data/<model_dir>/`
  - `/app/config/state`：锁、临时文件和兼容挂载目录。
  - `/app/config/logs`：兼容日志目录；运行期业务日志写入 Postgres。

## 常用测试命令

```bash
.venv/bin/python -m unittest tests.test_batch_discovery tests.test_legacy_archiver_validation tests.test_legacy_archiver_three_mf_wait tests.test_scrapling_fetch tests.test_missing_3mf tests.test_three_mf_quota tests.test_process_jobs tests.test_asset_sync
```

## 修改时不能破坏

- Cookie 失效、Cloudflare、403/404/418、HTML 验证页要给出可诊断错误，不能把整段 HTML 写到 UI 或日志。
- 能用 Scrapling 的地方优先用 Scrapling；fallback 要有日志 trace，但不要泄露 Cookie/Token。
- 批量发现结果要和源端总数形成闭环；数量不匹配时应保留状态并提示，不要误归档或误标删除。
- 同一任务不能重复入队；缺失 3MF 重试也要检查已排队任务。
- 3MF 每日/站点限额命中后要暂停自动重试，避免每天半夜反复触发上限。
- 下载成功后必须刷新模型索引和快照，否则前端会看不到新模型。
- 源端删除判断不能只靠收藏夹/合集缺失；只有作者上传页确认缺失或直接检查模型链接删除时才标源端删除。

## 给 Codex 的上下文入口

改归档、3MF、MakerWorld 接口、Scrapling、批量发现时，先读：

- `app/services/archive_worker.py`
- `app/services/process_jobs.py`
- `app/services/legacy_archiver.py`
- `app/services/batch_discovery.py`
- `app/services/scrapling_fetch.py`
- `app/services/three_mf.py`
- `app/services/three_mf_quota.py`
- `app/services/archive_repair.py`
- `app/services/archive_model_index_rebuild.py`
