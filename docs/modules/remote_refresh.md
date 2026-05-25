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

- `GET /api/remote-refresh`
- `POST /api/remote-refresh/run`
- `POST /api/config/remote-refresh`

### Service 函数/类

- `RemoteRefreshManager.start()` / `stop()`
- `RemoteRefreshManager.run_once()` / 定时刷新入口
- `RemoteRefreshManager.submit_manual_run()` / 手动刷新入口
- `run_source_deleted_check_job()`
- `_merge_comments()`
- `_merge_instances()`
- `_finalize_refreshed_meta()`
- `_update_meta_refresh_error()`

## 数据和目录

- Postgres/JSON state:
  - `makerhub_json_state:remote_refresh_state`
  - `makerhub_json_state:missing_3mf`
  - `makerhub_json_state:model_flags`
  - `archive_model_index`
- 文件:
  - `/app/data/archive/<model_dir>/meta.json`
  - `/app/config/state/remote_refresh_state.json`
  - `/app/config/logs/remote_refresh.log` 仅作为历史日志迁移输入。

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
- `app/services/process_jobs.py`
- `app/services/catalog.py` 中 remote sync/评论标准化相关函数
- `app/api/config.py` 中 `/remote-refresh` 段落
- `frontend/src/pages/RemoteRefreshPage.vue`
