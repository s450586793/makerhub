# Task 2 实现报告

## 实现摘要

- 新增 `makerworld_parsers.model`，承接 MakerWorld 模型页面和设计 payload 的纯解析逻辑。
- `legacy_archiver` 保留浏览器 HTML/API 获取函数，并通过兼容 alias 调用新 parser。
- `source_library` 直接从新 parser 导入 `extract_next_data()`。

## 文件清单

- `app/services/makerworld_parsers/model.py`
- `app/services/makerworld_parsers/__init__.py`
- `app/services/legacy_archiver.py`
- `app/services/source_library.py`
- `tests/test_makerworld_parser_model.py`
- `tests/test_legacy_archiver_validation.py`

## 测试结果

```text
.venv/bin/python -m pytest tests/test_makerworld_parser_common.py tests/test_makerworld_parser_model.py tests/test_legacy_archiver_validation.py tests/test_source_library.py -q
67 passed in 2.59s
```

同时通过 `compileall` 和 `git diff --check`。

## 自查

- 新 parser 使用 Task 1 的 `extract_model_id()`，不含网络 I/O。
- `fetch_html_with_browser()` 和 `fetch_design_from_api()` 仍位于 `legacy_archiver`。
- `legacy_archiver` 对外保留所有 brief 指定的兼容 alias。
- 未修改数据库 schema、任务队列、账号 Gate、每日限额、归档目录或 Runtime Engine。
- 未暂存或修改 `videos/makerhub-intro/output/`。

## 提交

实现提交：`c1ed72aab231a25b7ec26439efbf0701388bfa47`（`refactor: 提取 MakerWorld 模型解析器`）
