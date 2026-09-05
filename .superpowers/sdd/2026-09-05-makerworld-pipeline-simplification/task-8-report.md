# Task 8 Report

## 实现

- 将批量发现编排迁入 `app/services/makerworld_pipeline/discovery.py`。
- 新入口提供 `discover_source()`、`resolve_source_name()`、账号来源发现、关注作者/收藏夹发现和默认收藏夹来源。
- 控制面 JSON 与 HTML 请求均直接通过 `makerworld_browser_client`，`requests.Session` 仅保留 header/cookie 容器用途；同平台控制请求通过可重入锁串行。
- `batch_discovery.py` 收缩为兼容 facade，生产调用方改为依赖 Pipeline 或 parser URL helpers。
- Runtime Engine 的 `archive_adapter.py` 仅调整导入和调用名称，未改变注册、启动或执行逻辑。

## 验证

- TDD 入口测试先失败：`app.services.makerworld_pipeline.discovery` 尚不存在。
- `.venv/bin/python -m pytest tests/test_batch_discovery.py tests/test_makerworld_pipeline.py tests/test_subscriptions.py tests/test_source_library.py tests/test_archive_worker_batch_retry.py -q`
  - `143 passed, 4 subtests passed`
- `git diff --check` 通过。

## 范围确认

- 未修改数据库 schema、任务队列、账号 Gate、每日限额、历史数据、目录或 `online_accounts` 的 POST/ticket 流程。
- 未触碰 `videos/makerhub-intro/output/`。
