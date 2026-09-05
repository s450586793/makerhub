# Task 9 Report

## 实现

- 新增 `app/services/makerworld_pipeline/archive.py`，承接单模型归档的页面/API、评论、3MF 授权和归档编排。
- `makerworld_pipeline.archive_model()` 保持原有 kwargs 与 dict 返回字段；`process_jobs.py` 继续通过 Pipeline 公开入口调用。
- 页面、JSON 与 3MF 授权继续走 CloakBrowser/Puppeteer；图片、附件和签名 3MF 仍使用 `AssetDownloader` 的流式下载接口。
- `browser_authorize_3mf_download()` 在每实例授权路径最多调用一次，不会退回直接控制面请求。
- `legacy_archiver.archive_model()` 收缩为 lazy facade；离线重建、目录和媒体 helper 暂由 Pipeline 显式复用，避免 eager import cycle。

## 测试

- 增加 Pipeline 返回结果契约测试。
- 增加每实例浏览器授权至多一次测试。
- `.venv/bin/python -m pytest tests/test_makerworld_pipeline.py tests/test_legacy_archiver_validation.py tests/test_comment_replies.py tests/test_missing_3mf.py tests/test_legacy_archiver_three_mf_wait.py tests/test_asset_sync.py tests/test_profile_details.py -q`
  - `129 passed, 65 subtests passed`
- `.venv/bin/python -m py_compile app/services/makerworld_pipeline/archive.py app/services/makerworld_pipeline/__init__.py app/services/legacy_archiver.py`
  - 通过。
- `git diff --check`
  - 通过。

## 范围确认

- 未修改数据库 schema、任务队列、Gate、每日限额、历史数据、目录或 Runtime Engine。
- 未触碰 `videos/makerhub-intro/output/`。
