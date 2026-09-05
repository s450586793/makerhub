# 归档 / MakerWorld / 下载 / 3MF

## 职责

- 提交单模型归档任务。
- 对作者页、收藏夹、合集进行批量预扫描和入队。
- 通过 `makerworld_browser_client.py` 的 BrowserTransport 调用对应 CloakBrowser profile 的 MakerWorld/Bambu 控制面；页面和 JSON 载荷由 `makerworld_pipeline/` 编排、`makerworld_parsers/` 解析。
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
- `makerworld_browser_get()` / `makerworld_browser_get_text()` / `makerworld_browser_get_json()`
- `browser_authorize_3mf_download()`
- `AssetDownloader` / `download_file()` / `download_with_fresh_session()`
- `reserve_three_mf_download_slot()`
- `inspect_3mf_file()` / `resolve_model_instance_files()`

### MakerWorld 传输边界

- `makerworld_browser_client.py` 是服务层唯一允许调用 `browser_fetch()` 的客户端。MakerWorld 的页面和 JSON 控制面请求必须经由此客户端，供 `makerworld_pipeline/` 使用。
- `makerworld_parsers/` 只负责 URL、HTML 和 JSON 载荷解析，不得依赖网络客户端或状态存储。
- `AssetDownloader` 保持图片、附件和已取得签名直链 `3MF` 的 `requests` 流式下载通道；它不是控制面 BrowserTransport 的例外实现。
- `batch_discovery.py` 仅保留发现入口的兼容 re-export。`legacy_archiver.py` 仍保留离线页面重建、归档目录整理、归档入口 facade，以及迁移期兼容和既有 monkeypatch 测试所需的旧控制面实现；生产调用与控制面编排入口已迁到 `makerworld_pipeline/`，不得再新增对 legacy 控制面实现的生产依赖。
- `AdvancedRuntimeConfig.scraping_engine` 在 `v0.17.0` 中只用于读取旧 JSON 和接受旧客户端请求，运行时忽略该字段且不提供可切换的抓取引擎。

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
.venv/bin/python -m pytest tests/test_business_logs.py tests/test_resource_limiter.py tests/test_web_routes.py tests/test_makerworld_transport_boundary.py -q
.venv/bin/python -m pytest tests/test_process_jobs.py tests/test_batch_discovery.py tests/test_subscriptions.py tests/test_source_refresh.py tests/test_source_library.py -q
```

## 修改时不能破坏

- Cookie 失效、Cloudflare、403/404/418、HTML 验证页要给出可诊断错误，不能把整段 HTML 写到 UI 或日志。
- MakerWorld 页面和 JSON 控制请求必须复用对应 CloakBrowser profile；已关联 profile 不得注入 MakerHub 旧 Cookie / Token，国内和国际 profile 不得串用。
- CloakBrowser `5xx`、CDP 超时和断开按网络错误重试；只有真实 `401/403`、登录页、Cloudflare challenge 或验证载荷才能更新账号/gate 状态。
- 普通页面抓取和 3MF 授权遇到瞬时 CDP 超时时只能断开并重连，不得停止共享 profile；只有显式登录态同步/人工恢复流程可以在平台级锁内重启一次 profile。
- 图片、附件和已取得签名直链的 `3MF` 必须保留普通下载器直连，不得把大文件塞进浏览器通道；真实 `3MF` 点击授权不得内部重复。
- 不得绕过 BrowserTransport 以其他 HTTP 客户端承担 MakerWorld 控制面抓取；静态资源下载继续使用 AssetDownloader，日志不得泄露 Cookie/Token。
- 批量发现结果要和源端总数形成闭环；数量不匹配时应保留状态并提示，不要误归档或误标删除。
- 同一任务不能重复入队；缺失 3MF 重试也要检查已排队任务。
- 3MF 每日/站点限额命中后要暂停自动重试，避免每天半夜反复触发上限。
- Cookie 更新后的 `unknown` gate 每个平台只允许一个 3MF 探测任务；探测成功后必须打开 gate，已有同平台探测运行时不得并发放行第二个。
- 下载成功后必须刷新模型索引和快照，否则前端会看不到新模型。
- 源端删除判断不能只靠收藏夹/合集缺失；只有作者上传页确认缺失或直接检查模型链接删除时才标源端删除。
- Runtime Engine 当前处于冻结状态；归档、订阅和来源刷新继续由 Legacy manager 执行，不得把入队确认描述为最终完成，也不得在没有迁移计划时重新启用 Runtime Engine。

## 给 Codex 的上下文入口

改归档、3MF、MakerWorld 接口、传输边界、批量发现时，先读：

- `app/services/archive_worker.py`
- `app/services/process_jobs.py`
- `app/services/makerworld_pipeline/`
- `app/services/makerworld_parsers/`
- `app/services/makerworld_browser_client.py`
- `app/services/asset_downloader.py`
- `app/services/legacy_archiver.py`（离线归档工具、兼容 facade 与迁移期 monkeypatch 实现；非生产控制面入口）
- `app/services/batch_discovery.py`（兼容 re-export）
- `app/services/three_mf.py`
- `app/services/three_mf_quota.py`
- `app/services/archive_repair.py`
- `app/services/archive_model_index_rebuild.py`
