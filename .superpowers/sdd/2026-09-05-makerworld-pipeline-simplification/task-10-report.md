# Task 10 Report

## 实现

- 新增 AST 架构边界测试 `tests/test_makerworld_transport_boundary.py`：parser 不得依赖网络或 store，服务层仅 `makerworld_browser_client.py` 可调用 `browser_fetch()`，Pipeline、`process_jobs.py` 与 `source_health.py` 不得直接调用 `requests.get()` 或 `session.get()`。
- `source_library.py` 停止从 `legacy_archiver.py` 导入控制面 HTML 获取和 Cookie 解析，改用 Pipeline 与 `cookie_utils` 的正式入口。
- 更新归档模块契约和模块索引：控制面经 BrowserTransport，静态资源经 AssetDownloader；`batch_discovery.py` 与 `legacy_archiver.py` 的兼容职责明确保留。

## 验证

- `.venv/bin/python -m pytest tests/test_makerworld_transport_boundary.py tests/test_process_jobs.py tests/test_batch_discovery.py tests/test_subscriptions.py tests/test_source_refresh.py tests/test_source_library.py -q`
  - `138 passed, 4 subtests passed`
- 兼容检查：`batch_discovery` 的发现入口导出存在，`legacy_archiver.archive_model` 保持可调用。
- `git diff --check` 通过。

## 范围确认

- 未修改 Runtime Engine、数据库、队列、Gate、下载限额或历史数据。
- AssetDownloader 静态下载与 `online_accounts` credential-login POST/ticket 流程不在 AST HTTP 禁止范围内，未修改其行为。
- 未触碰 `videos/makerhub-intro/output/`。
