# Core / 配置 / 鉴权 / 数据库 / 日志

## 职责

- 管理应用运行目录、环境变量、版本号、进程角色。
- 管理单用户登录、session、API Token、Token 权限。
- 管理 Cookie、线上账号、代理、分享默认设置、移动端导入 Token、运行资源配置。
- 提供 Postgres 连接和 JSON 状态读写。
- 写入业务日志，并对敏感字段脱敏。
- 负责旧 JSON/日志文件到数据库的迁移入口；运行期不再把旧文件作为后备来源。

## 不负责

- 不解析 MakerWorld 模型详情，不下载 3MF。
- 不决定订阅扫描规则和来源库卡片排序。
- 不直接整理本地 zip/rar/STL 文件。
- 不在前端实现权限判断的最终安全边界；后端鉴权才是准入点。

## 对外契约

### HTTP API

- `POST /api/auth/login`
- `POST /api/auth/logout`
- `GET /api/auth/me`
- `GET /api/auth/tokens`
- `POST /api/auth/tokens`
- `DELETE /api/auth/tokens/{token_id}`
- `POST /api/auth/password`
- `GET /api/bootstrap`
- `GET /api/config`
- `POST /api/config/cookies`
- `POST /api/config/cookies/test`
- `POST /api/config/online-accounts/login`
- `POST /api/config/online-accounts/{platform}/test`
- `DELETE /api/config/online-accounts/{platform}`
- `POST /api/config/proxy`
- `POST /api/config/proxy/test`
- `POST /api/config/user`
- `POST /api/config/theme`
- `POST /api/config/notifications`
- `POST /api/config/advanced`
- `POST /api/config/runtime`
- `GET /api/system/diagnostics`
- `GET /api/logs`

### Service 函数/类

- `JsonStore.load()` / `JsonStore.save()`
- `AuthManager.resolve_request_auth()`
- `AuthManager.create_token()` / `AuthManager.revoke_token()`
- `normalize_token_permissions()`
- `login_online_account()` / `online_account_metadata_from_cookie()`
- `append_business_log()` / `append_structured_log()`
- `list_log_files()` / 日志读取函数
- `initialize_database()`
- `load_json_state()` / `save_json_state()` / `delete_json_state()`
- `migrate_json_files_to_database()` / `migrate_log_files_to_database()`
- `build_runtime_diagnostics()`

## 数据和目录

- Postgres:
  - `makerhub_json_state`
  - `makerhub_logs`
  - `makerhub_metadata`
- 关键 JSON state keys:
  - `app_config`
  - `auth_sessions`
  - `archive_queue`
  - `missing_3mf`
  - `organize_tasks`
  - `model_flags`
  - `subscriptions_state`
  - `remote_refresh_state`
  - `three_mf_limit_guard`
  - `three_mf_daily_quota`
  - `archive_repair_status`
  - `archive_profile_backfill_status`
  - `system_update`
  - `archive_snapshot_marker`
  - `local_preview_queue_marker`
  - `cookie_source_sync_state`
  - `cookie_source_inventory`
  - `source_library_metadata`
  - `model_shares`
- 旧文件迁移输入:
  - `/app/config/config/config.json`
  - 兼容旧部署的 `/app/config/config.json`
  - `/app/config/state/*.json`
  - `/app/config/state/*.marker`
  - `/app/config/logs/*.log`

## 常用测试命令

```bash
.venv/bin/python -m unittest tests.test_auth_guard tests.test_config_cookies tests.test_database_json_state tests.test_business_logs tests.test_proxy_policy
```

## 修改时不能破坏

- 数据库版本的结构化状态必须落在 Postgres；未配置 `MAKERHUB_DATABASE_URL` 时应提示升级 compose，而不是继续新增文件状态分支。
- 业务日志运行期必须读写 `makerhub_logs`；旧 `/app/config/logs/*.log` 只作为迁移输入。
- 高频状态事件和纯成功追踪日志应在后端合并或降噪；失败、告警和用户动作日志必须保留。
- 保存 Cookie/Token/分享码/公网地址不能把明文写进业务日志。
- Token 权限必须由后端校验，前端隐藏按钮不能替代后端权限。
- 保存设置后要更新资源限制和相关后台任务触发逻辑。
- Cookie 保存后触发关注作者/收藏夹同步时应快速返回，耗时工作交给 worker。
- 数据库不可用时要有明确错误，不能静默写回或读取旧 state/log 文件。

## 给 Codex 的上下文入口

改设置、账号、Cookie、Token、日志、数据库迁移时，先读：

- `app/schemas/models.py`
- `app/core/settings.py`
- `app/core/store.py`
- `app/core/database.py`
- `app/services/auth.py`
- `app/services/business_logs.py`
- `app/api/auth.py`
- `app/api/config.py` 中 `/config`、`/auth`、`/logs` 相关段落
