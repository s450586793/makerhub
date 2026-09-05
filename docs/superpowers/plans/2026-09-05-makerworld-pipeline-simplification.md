# MakerWorld 抓取流水线精简 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将订阅、批量发现、手动归档和源端刷新收敛到同一条 MakerWorld Pipeline，同时保留 CloakBrowser 控制面与 `requests` 静态下载两类传输通道。

**Architecture:** `makerworld_browser_client.py` 继续独占 MakerWorld HTML / JSON 控制面请求；新 `makerworld_parsers` 包只做无副作用解析，新 `asset_downloader.py` 只做静态资源流式下载，新 `makerworld_pipeline` 包负责发现、归档和来源检查编排。旧 `legacy_archiver.py`、`batch_discovery.py` 在迁移期保留 facade，Scrapling 残留分兼容发布与最终 schema 清理两个版本退出。

**Tech Stack:** Python 3.11、FastAPI、Pydantic、requests、BeautifulSoup、CloakBrowser、Puppeteer Core、unittest/pytest、Vue 3、Node.js 20。

**Spec:** `docs/superpowers/specs/2026-09-05-makerworld-pipeline-simplification-design.md`

## Global Constraints

- MakerWorld HTML / JSON 控制面请求只能经过 `app/services/makerworld_browser_client.py`。
- 图片、头像、附件和签名 `3MF` 必须保留 `requests` 流式下载，不得进入 32 MB bridge 正文通道。
- `browser_authorize_3mf_download()` 保持独立的有副作用 Puppeteer 命令，不得自动重复授权。
- 不修改数据库 schema、任务队列、归档目录、模型索引格式、账号 Gate、每日限额或历史数据。
- Runtime Engine 继续冻结；只维护其现有兼容 import，不重新接管生产任务。
- 同一 profile 的控制面操作继续通过现有 `resource_slot("makerworld_page_api")` 串行。
- `run_archive_model_job()`、`run_discover_batch_urls_job()`、`archive_model()` 和 `discover_batch_model_urls()` 的参数及返回字典保持兼容。
- 移动代码的提交不得同时更改解析结果、fallback 顺序、状态映射或用户文案。
- 每个任务只暂存列出的文件；不得暂存或删除 `videos/makerhub-intro/output/`。
- 未经用户明确要求不得推送、打 Tag 或部署。
- 第一阶段发布版本为 `0.17.0`；Scrapling schema 最终移除版本为 `0.17.1`，且只能在 `v0.17.0` 已实际发布后执行。

## File Structure

### 新增

- `app/services/makerworld_parsers/__init__.py`：稳定 parser 导出。
- `app/services/makerworld_parsers/common.py`：URL、ID 和平台纯规范化。
- `app/services/makerworld_parsers/model.py`：Next.js 与设计 payload 纯解析。
- `app/services/makerworld_parsers/listing.py`：来源列表、账号、作者和收藏夹纯解析。
- `app/services/makerworld_parsers/comments.py`：评论树、回复与数量纯解析。
- `app/services/asset_downloader.py`：原子流式下载和有界并发任务执行。
- `app/services/makerworld_pipeline/__init__.py`：Pipeline 稳定入口。
- `app/services/makerworld_pipeline/discovery.py`：批量来源发现编排。
- `app/services/makerworld_pipeline/archive.py`：单模型归档编排。
- `app/services/makerworld_pipeline/source_status.py`：来源删除检查。
- `tests/test_makerworld_parser_common.py`
- `tests/test_makerworld_parser_model.py`
- `tests/test_makerworld_parser_listing.py`
- `tests/test_makerworld_parser_comments.py`
- `tests/test_asset_downloader.py`
- `tests/test_makerworld_pipeline.py`
- `tests/test_makerworld_transport_boundary.py`

### 修改

- `app/services/legacy_archiver.py`：改用 parser/downloader，并最终保留兼容导出。
- `app/services/batch_discovery.py`：改用 parser，并最终保留兼容导出。
- `app/services/process_jobs.py`：只调用 Pipeline，不再用 `requests` 检查来源。
- `app/services/source_library.py`、`app/services/catalog.py`、`app/services/archive_model_index.py`、`app/services/archive_worker.py`、`app/services/subscriptions.py`、`app/services/online_accounts.py`、`app/api/config.py`：迁移公共 import。
- `app/services/runtime_engine/archive_adapter.py`：只更新兼容 import，不启用 Runtime Engine。
- `app/services/remote_refresh.py`：迁移评论 parser import。
- `app/schemas/models.py`、`frontend/src/lib/settingsPayloads.js`、`frontend/src/lib/settingsPayloads.test.mjs`：Scrapling 配置兼容退场。
- `app/services/business_logs.py`：在新 trace 停止写入后删除 Scrapling 降噪项。
- `docs/modules/archive.md`、`docs/MODULES.md`：更新模块契约。
- `VERSION`、`frontend/package.json`、`frontend/package-lock.json`、`README.md`、`CHANGELOG.md`、`tests/test_release_contract.py`：发布元数据。

### 删除

- `app/services/scrapling_fetch.py`
- `tests/test_scrapling_fetch.py`

---

### Task 1: 提取 URL 与平台纯解析边界

**Files:**
- Create: `app/services/makerworld_parsers/__init__.py`
- Create: `app/services/makerworld_parsers/common.py`
- Create: `tests/test_makerworld_parser_common.py`
- Modify: `app/services/batch_discovery.py:30-121`
- Modify: `app/services/catalog.py`
- Modify: `app/services/archive_model_index.py`
- Modify: `app/services/remote_refresh.py`
- Modify: `app/api/config.py`

**Interfaces:**
- Consumes: Python `urllib.parse` 和当前 `MODEL_PATH_RE`、`AUTHOR_UPLOAD_RE`、`AUTHOR_ROOT_RE`、`COLLECTION_DETAIL_RE` 规则。
- Produces: `normalize_source_url(url: str) -> str`、`normalize_model_url(url: str, fallback_base: str = "https://makerworld.com.cn") -> str`、`extract_model_id(url: str) -> str`、`platform_from_url(url: str) -> Literal["cn", "global", ""]`。

- [ ] **Step 1: 写新模块的失败测试**

```python
from app.services.makerworld_parsers.common import (
    extract_model_id,
    normalize_model_url,
    normalize_source_url,
    platform_from_url,
)


def test_common_parser_keeps_cn_and_global_separate():
    assert normalize_source_url("https://makerworld.com/@Ace/upload?x=1") == "https://makerworld.com/zh/@Ace/upload"
    assert normalize_source_url("https://makerworld.com.cn/@Ace") == "https://makerworld.com.cn/zh/@Ace/upload"
    assert normalize_model_url("https://makerworld.com/en/models/123-demo") == "https://makerworld.com/zh/models/123"
    assert extract_model_id("https://makerworld.com.cn/zh/models/456-demo") == "456"
    assert platform_from_url("https://api.bambulab.cn/v1/design-service/design/456") == "cn"
    assert platform_from_url("https://makerworld.com/zh/models/123") == "global"
    assert platform_from_url("https://example.test/models/123") == ""
```

- [ ] **Step 2: 运行测试并确认模块不存在**

Run: `.venv/bin/python -m pytest tests/test_makerworld_parser_common.py -q`

Expected: FAIL，错误为 `ModuleNotFoundError: app.services.makerworld_parsers`。

- [ ] **Step 3: 移动实现并保留旧导出**

```python
# app/services/batch_discovery.py
from app.services.makerworld_parsers.common import (
    extract_model_id,
    normalize_model_url,
    normalize_source_url,
)
```

`common.py` 复制现有正则和三个函数的实现，不改 URL 输出。新增 `platform_from_url()` 时只接受 MakerWorld / Bambu 的 CN 与 Global 域名，其他域名返回空字符串。逐个把仓库内公共调用方改为从 `makerworld_parsers.common` 导入；`batch_discovery.py` 继续导入并暴露同名符号，兼容旧测试与外部 import。

- [ ] **Step 4: 运行纯解析和调用方回归**

Run: `.venv/bin/python -m pytest tests/test_makerworld_parser_common.py tests/test_batch_discovery.py tests/test_catalog_memory.py tests/test_catalog_placeholder_cover.py tests/test_archive_model_index.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add app/services/makerworld_parsers/__init__.py app/services/makerworld_parsers/common.py app/services/batch_discovery.py app/services/catalog.py app/services/archive_model_index.py app/services/remote_refresh.py app/api/config.py tests/test_makerworld_parser_common.py
git commit -m "refactor: 提取 MakerWorld URL 解析器"
```

### Task 2: 提取模型页面与设计 Payload 解析器

**Files:**
- Create: `app/services/makerworld_parsers/model.py`
- Create: `tests/test_makerworld_parser_model.py`
- Modify: `app/services/makerworld_parsers/__init__.py`
- Modify: `app/services/legacy_archiver.py:778-1173`
- Modify: `app/services/source_library.py`
- Modify: `tests/test_legacy_archiver_validation.py`

**Interfaces:**
- Consumes: `str` HTML、`dict` Next.js payload 和 Task 1 的 `extract_model_id()`。
- Produces: `extract_next_data(html_text: str) -> dict`、`extract_design_from_next_data(next_data: dict) -> dict | None`、`parse_design_id(url: str) -> int | None`、`design_payload_error(design: object, source_url: str) -> str`、`normalize_design_payload_identity(design: dict, source_url: str) -> None`、`append_api_base_candidate(bases: list[str], base: str, source: str) -> None`、`extract_api_host(html_text: str) -> str | None`、`is_cloudflare_challenge(html_text: str) -> bool`、`is_makerworld_not_found_page(html_text: str) -> bool`、`unwrap_design_payload(payload: object) -> dict | None`。

- [ ] **Step 1: 写 parser 直接调用测试**

```python
import json

from app.services.makerworld_parsers.model import (
    design_payload_error,
    extract_design_from_next_data,
    extract_next_data,
)


def test_model_parser_extracts_matching_design_without_io():
    design = {"id": 2416065, "title": "Demo", "coverUrl": "https://cdn.example.test/a.jpg"}
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps({"props": {"pageProps": {"design": design}}})}</script>'
    next_data = extract_next_data(html)
    assert extract_design_from_next_data(next_data) == design
    assert design_payload_error(design, "https://makerworld.com.cn/zh/models/2416065") == ""
```

另加畸形 script、JavaScript assignment、ID 不匹配、标题为空、Cloudflare 和 404 HTML fixture，预期与现有 `legacy_archiver` 测试完全一致。

- [ ] **Step 2: 运行测试并确认失败**

Run: `.venv/bin/python -m pytest tests/test_makerworld_parser_model.py -q`

Expected: FAIL，提示 `makerworld_parsers.model` 尚不存在。

- [ ] **Step 3: 原样移动纯函数并建立 facade alias**

移动 `_json_loads_maybe` 至 `_unwrap_design_payload` 之间的纯解析函数；网络函数 `fetch_html_with_browser()` 与 `fetch_design_from_api()` 暂留原模块。私有函数改为新模块内部实现，旧模块仅保留下列兼容 alias：

```python
from app.services.makerworld_parsers.model import (
    append_api_base_candidate as _append_api_base_candidate,
    design_payload_error as _design_payload_error,
    extract_api_host as _extract_api_host,
    extract_design_from_next_data,
    extract_next_data,
    is_cloudflare_challenge as _is_cloudflare_challenge,
    is_makerworld_not_found_page as _is_makerworld_not_found_page,
    normalize_design_payload_identity as _normalize_design_payload_identity,
    parse_design_id as _parse_design_id,
    unwrap_design_payload as _unwrap_design_payload,
)
```

`source_library.py` 直接从新 parser 导入 `extract_next_data()`；测试同时断言旧 facade 和新模块返回相同结果。

- [ ] **Step 4: 运行模型与来源库回归**

Run: `.venv/bin/python -m pytest tests/test_makerworld_parser_model.py tests/test_legacy_archiver_validation.py tests/test_source_library.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add app/services/makerworld_parsers/__init__.py app/services/makerworld_parsers/model.py app/services/legacy_archiver.py app/services/source_library.py tests/test_makerworld_parser_model.py tests/test_legacy_archiver_validation.py
git commit -m "refactor: 提取 MakerWorld 模型解析器"
```

### Task 3: 提取来源列表纯解析器

**Files:**
- Create: `app/services/makerworld_parsers/listing.py`
- Create: `tests/test_makerworld_parser_listing.py`
- Modify: `app/services/makerworld_parsers/__init__.py`
- Modify: `app/services/batch_discovery.py`
- Modify: `tests/test_batch_discovery.py`

**Interfaces:**
- Consumes: Task 1 的 URL 函数和 Task 2 的 `extract_next_data()`。
- Produces: `extract_hits_payload()`、`extract_total_count()`、`extract_has_next()`、`extract_model_source_items()`、`extract_account_profile()`、`extract_user_info_from_next_data()`、`extract_followed_authors()`、`extract_followed_collections()`、`extract_collection_entries()`、`extract_page_links()`；输入只允许内存 payload/HTML，输出保持当前 list/dict 形状。

- [ ] **Step 1: 将代表性 fixture 改为直接测试新模块**

```python
from app.services.makerworld_parsers.listing import (
    extract_collection_entries,
    extract_followed_authors,
    extract_model_source_items,
)


def test_listing_parser_separates_collection_entries_from_nested_designs():
    payload = {"hits": [{
        "id": 518732,
        "title": "默认收藏夹",
        "designCnt": 2,
        "designs": [{"id": 2356866, "title": "嵌套模型"}],
    }]}
    assert extract_collection_entries(payload, "2024907479") == [
        {"id": "518732", "name": "默认收藏夹", "count": 2}
    ]


def test_listing_parser_emits_only_author_hits():
    payload = {"hits": [
        {"uid": 1, "handle": "AcePrint", "name": "Ace", "avatarUrl": "https://example.test/a.jpg"},
        {"designId": 2, "title": "Not author", "coverUrl": "https://example.test/m.jpg"},
    ]}
    assert [item["url"] for item in extract_followed_authors(payload, "cn")] == [
        "https://makerworld.com.cn/zh/@AcePrint/upload"
    ]
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `.venv/bin/python -m pytest tests/test_makerworld_parser_listing.py -q`

Expected: FAIL，提示导出不存在。

- [ ] **Step 3: 移动纯 helper，网络编排留在发现模块**

将当前以下私有实现及其纯依赖移动并改为上方公共名称：`_iter_nodes`、`_iter_dicts`、`_extract_hits_payload`、`_extract_total_count`、`_extract_has_next`、`_extract_model_source_items_from_hits`、`_extract_account_profile`、`_extract_user_info_from_next_data`、`_extract_followed_authors`、`_extract_followed_collections`、`_extract_collection_entries`、`_extract_page_links`。与 endpoint、session、日志有关的 `_api_get_json()`、`_fetch_*()`、`discover_*()` 留在编排模块。

```python
# app/services/batch_discovery.py，迁移期 facade
from app.services.makerworld_parsers.listing import (
    extract_account_profile as _extract_account_profile,
    extract_collection_entries as _extract_collection_entries,
    extract_followed_authors as _extract_followed_authors,
    extract_followed_collections as _extract_followed_collections,
    extract_hits_payload as _extract_hits_payload,
    extract_model_source_items as _extract_model_source_items_from_hits,
    extract_page_links as _extract_page_links,
    extract_total_count as _extract_total_count,
    extract_user_info_from_next_data as _extract_user_info_from_next_data,
)
```

- [ ] **Step 4: 运行完整批量发现测试**

Run: `.venv/bin/python -m pytest tests/test_makerworld_parser_listing.py tests/test_batch_discovery.py tests/test_subscriptions.py -q`

Expected: PASS，现有 expected total、分页、收藏夹和账号发现断言不变。

- [ ] **Step 5: 提交**

```bash
git add app/services/makerworld_parsers/__init__.py app/services/makerworld_parsers/listing.py app/services/batch_discovery.py tests/test_makerworld_parser_listing.py tests/test_batch_discovery.py
git commit -m "refactor: 提取 MakerWorld 来源解析器"
```

### Task 4: 提取评论树纯解析器

**Files:**
- Create: `app/services/makerworld_parsers/comments.py`
- Create: `tests/test_makerworld_parser_comments.py`
- Modify: `app/services/makerworld_parsers/__init__.py`
- Modify: `app/services/legacy_archiver.py:1253-1565,1859-2434,2536-2787`
- Modify: `app/services/remote_refresh.py`
- Modify: `tests/test_comment_replies.py`

**Interfaces:**
- Consumes: 评论 API 的 `dict/list` payload。
- Produces: `normalize_threaded_comments(comment_items: list[dict] | None) -> list[dict]`、`extract_comment_sections(payload: object) -> list[object]`、`extract_comment_replies(payload: object, root_comment_id: str) -> list[dict]`、`extract_comment_list_items(payload: object) -> list[dict]`、`resolve_comment_count(*, unique_sections: list[object], next_data: dict, design: dict, comment_total: int, page_fetch_stats: dict[str, object]) -> int`。

- [ ] **Step 1: 写无网络评论 parser 测试**

```python
from app.services.makerworld_parsers.comments import normalize_threaded_comments


def test_comments_parser_attaches_flat_replies_to_root():
    items = [
        {"commentId": "root", "commentContent": "主评论", "commentTime": "2026-09-05 10:00:00"},
        {"commentId": "reply", "rootCommentId": "root", "commentContent": "回复", "commentTime": "2026-09-05 10:01:00"},
    ]
    result = normalize_threaded_comments(items)
    assert [item["id"] for item in result] == ["root"]
    assert [item["id"] for item in result[0]["replies"]] == ["reply"]
```

从 `tests/test_comment_replies.py` 复制评分评论、空内容、嵌套回复、扁平回复、重复回复和 comment count 优先级 fixture，直接覆盖新公共函数。

- [ ] **Step 2: 运行测试并确认失败**

Run: `.venv/bin/python -m pytest tests/test_makerworld_parser_comments.py -q`

Expected: FAIL，提示 `makerworld_parsers.comments` 不存在。

- [ ] **Step 3: 移动纯评论函数并保留旧 alias**

移动评论文本、作者、图片候选、树合并、payload 遍历、回复提取、列表提取和数量解析函数；以下内容留在归档编排：API endpoint 候选、`_fetch_comment_*()`、`collect_comments()`、本地头像/图片路径和下载。

```python
# app/services/legacy_archiver.py
from app.services.makerworld_parsers.comments import (
    extract_comment_list_items as _extract_comment_list_items,
    extract_comment_replies as _extract_comment_replies_from_payload,
    normalize_threaded_comments,
    resolve_comment_count as _resolved_comment_count,
)
```

`remote_refresh.py` 直接导入新模块的 `normalize_threaded_comments()`；旧 facade 保留 `COMMENT_SCHEMA_VERSION`。

- [ ] **Step 4: 运行评论与刷新回归**

Run: `.venv/bin/python -m pytest tests/test_makerworld_parser_comments.py tests/test_comment_replies.py tests/test_remote_refresh.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add app/services/makerworld_parsers/__init__.py app/services/makerworld_parsers/comments.py app/services/legacy_archiver.py app/services/remote_refresh.py tests/test_makerworld_parser_comments.py tests/test_comment_replies.py
git commit -m "refactor: 提取 MakerWorld 评论解析器"
```

### Task 5: 提取静态资源流式下载器

**Files:**
- Create: `app/services/asset_downloader.py`
- Create: `tests/test_asset_downloader.py`
- Modify: `app/services/legacy_archiver.py:595-634,1697-1796`
- Modify: `tests/test_legacy_archiver_parallel_assets.py`

**Interfaces:**
- Consumes: `requests.Session`、静态 URL、目标 `Path` 和现有 `resource_slot("comment_assets")`。
- Produces: `download_file(session, url, dest, overwrite=False, *, timeout=(15, 30), max_duration=45) -> None`、`download_with_fresh_session(base_session, url, dest) -> None`、`run_asset_tasks(tasks, *, max_workers, on_progress=None, on_error=None) -> dict[str, int]`。

- [ ] **Step 1: 写原子下载和并发失败测试**

```python
def test_download_file_removes_partial_file_on_stream_failure(tmp_path):
    destination = tmp_path / "asset.bin"

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            assert chunk_size == 64 * 1024
            yield b"partial"
            raise requests.RequestException("stream interrupted")

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    with pytest.raises(requests.RequestException):
        download_file(Session(), "https://cdn.example.test/asset.bin", destination)
    assert not destination.exists()
    assert list(tmp_path.glob("*.part")) == []


def test_run_asset_tasks_applies_every_success_and_caps_workers():
    applied = []
    tasks = [
        {"url": str(index), "download": lambda: None, "apply": [lambda index=index: applied.append(index)]}
        for index in range(8)
    ]
    stats = run_asset_tasks(tasks, max_workers=4)
    assert stats == {"completed": 8, "failed": 0}
    assert sorted(applied) == list(range(8))
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `.venv/bin/python -m pytest tests/test_asset_downloader.py -q`

Expected: FAIL，提示 `asset_downloader` 不存在。

- [ ] **Step 3: 原样移动流式实现并增加通用任务执行器**

```python
def run_asset_tasks(tasks, *, max_workers, on_progress=None, on_error=None):
    stats = {"completed": 0, "failed": 0}
    if not tasks:
        return stats
    with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), len(tasks)))) as executor:
        future_map = {executor.submit(task["download"]): task for task in tasks}
        for completed, future in enumerate(as_completed(future_map), start=1):
            task = future_map[future]
            if on_progress:
                on_progress(completed, len(tasks))
            try:
                future.result()
            except Exception as exc:
                stats["failed"] += 1
                if on_error:
                    on_error(task, exc)
                continue
            stats["completed"] += 1
            for apply_ref in task.get("apply") or []:
                try:
                    apply_ref()
                except Exception:
                    continue
    return stats
```

`legacy_archiver._download_comment_assets()` 与 `_download_image_assets()` 改为薄包装，分别传入当前进度频率、文案和 `COMMENT_ASSET_DOWNLOAD_WORKERS` / `IMAGE_ASSET_DOWNLOAD_WORKERS`；旧模块 re-export `download_file`，保证现有 patch 路径有效。

- [ ] **Step 4: 运行下载与归档资源回归**

Run: `.venv/bin/python -m pytest tests/test_asset_downloader.py tests/test_legacy_archiver_parallel_assets.py tests/test_asset_sync.py tests/test_comment_replies.py -q`

Expected: PASS，最大 worker 仍为 4，图片顺序不变。

- [ ] **Step 5: 提交纯移动**

```bash
git add app/services/asset_downloader.py app/services/legacy_archiver.py tests/test_asset_downloader.py tests/test_legacy_archiver_parallel_assets.py
git commit -m "refactor: 提取静态资源下载器"
```

### Task 6: 收紧静态下载安全与错误边界

**Files:**
- Modify: `app/services/asset_downloader.py`
- Modify: `tests/test_asset_downloader.py`
- Modify: `tests/test_legacy_archiver_validation.py`

**Interfaces:**
- Consumes: Task 5 下载接口。
- Produces: `safe_asset_url(url: str) -> str`，只保留 scheme、host、path；静态资源异常不包含查询参数，也不改变账号/Gate。

- [ ] **Step 1: 写签名 URL 脱敏和静态错误隔离测试**

```python
def test_download_error_redacts_signed_query(tmp_path):
    signed = "https://cdn.example.test/file.3mf?token=secret&signature=hidden"

    class Response:
        status_code = 403

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            raise requests.HTTPError("403 for signed URL")

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    with pytest.raises(AssetDownloadError) as caught:
        download_file(Session(), signed, tmp_path / "file.3mf")
    assert "secret" not in str(caught.value)
    assert "signature" not in str(caught.value)
    assert "https://cdn.example.test/file.3mf" in str(caught.value)
```

- [ ] **Step 2: 运行测试并确认敏感查询串仍会进入异常/日志**

Run: `.venv/bin/python -m pytest tests/test_asset_downloader.py::test_download_error_redacts_signed_query -q`

Expected: FAIL。

- [ ] **Step 3: 添加专用异常和脱敏 URL**

```python
class AssetDownloadError(RuntimeError):
    pass


def safe_asset_url(url: str) -> str:
    parsed = urlsplit(str(url or ""))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
```

捕获 `requests`、HTTP 和超时异常时，用 `raise AssetDownloadError(f"静态资源下载失败：{safe_asset_url(url)}") from exc`；不要将异常映射为 `MakerWorldBrowserError` 或账号状态。

同时把 Task 5 的流中断测试从 `pytest.raises(requests.RequestException)` 更新为 `pytest.raises(AssetDownloadError)`，并继续断言 `.part` 文件已清理。

- [ ] **Step 4: 运行资源与账号错误分类回归**

Run: `.venv/bin/python -m pytest tests/test_asset_downloader.py tests/test_legacy_archiver_validation.py tests/test_archive_worker_browser_recovery.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add app/services/asset_downloader.py tests/test_asset_downloader.py tests/test_legacy_archiver_validation.py
git commit -m "fix: 隔离并脱敏静态下载错误"
```

### Task 7: 建立 Pipeline 公共入口并迁移来源删除检查

**Files:**
- Create: `app/services/makerworld_pipeline/__init__.py`
- Create: `app/services/makerworld_pipeline/source_status.py`
- Create: `tests/test_makerworld_pipeline.py`
- Modify: `app/services/process_jobs.py:90-124,446-563`
- Modify: `app/services/source_health.py:338-385`
- Modify: `tests/test_process_jobs.py`
- Modify: `tests/test_source_health.py`

**Interfaces:**
- Consumes: `makerworld_browser_get()`、Task 1 `normalize_source_url()`、现有 legacy `archive_model()` 和 `discover_batch_model_urls()`。
- Produces: `archive_model(**kwargs) -> dict[str, Any]`、`discover_source(url: str, raw_cookie: str, max_pages: int = 12) -> dict[str, Any]`、`source_is_deleted(url: str, raw_cookie: str) -> bool`。

- [ ] **Step 1: 写来源状态只走浏览器的失败测试**

```python
def test_source_is_deleted_uses_browser_response():
    response = MakerWorldBrowserResponse(
        url="https://makerworld.com.cn/404",
        status_code=404,
        content_type="text/html",
        text="not found",
        profile_id="cn-profile",
    )
    with patch("app.services.makerworld_pipeline.source_status.makerworld_browser_get", return_value=response) as browser_get:
        assert source_is_deleted("https://makerworld.com.cn/zh/models/1", "token=ok") is True
    browser_get.assert_called_once()


def test_source_is_deleted_treats_browser_outage_as_unknown_not_deleted():
    with patch("app.services.makerworld_pipeline.source_status.makerworld_browser_get", side_effect=MakerWorldBrowserError("暂不可用")):
        assert source_is_deleted("https://makerworld.com.cn/zh/models/1", "token=ok") is False


def test_auth_probe_uses_browser_client_without_session_get():
    class FailingSession:
        def get(self, *_args, **_kwargs):
            raise AssertionError("account probes must use BrowserTransport")

        def close(self):
            return None

    response = SimpleNamespace(
        status_code=200,
        text='{"uid": 1, "name": "ok"}',
        headers={"content-type": "application/json"},
        url="https://api.bambulab.com/v1/user-service/my/message/count",
    )
    with patch.object(source_health, "_make_session", return_value=FailingSession()), \
         patch.object(source_health, "makerworld_browser_get", return_value=response) as browser_get:
        payload = source_health._probe_auth_endpoints("global", "token=abc", None)
    assert payload["ok"] is True
    assert browser_get.call_count == 2
```

- [ ] **Step 2: 运行测试并确认 Pipeline 不存在**

Run: `.venv/bin/python -m pytest tests/test_makerworld_pipeline.py -q`

Expected: FAIL。

- [ ] **Step 3: 实现入口 facade 与浏览器来源检查**

```python
# makerworld_pipeline/source_status.py
def source_is_deleted(url: str, raw_cookie: str) -> bool:
    try:
        response = makerworld_browser_get(
            normalize_source_url(url),
            raw_cookie=raw_cookie,
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
    except MakerWorldBrowserError:
        return False
    final_path = urlparse(response.url).path.lower()
    return response.status_code == 404 or "/404" in final_path or "not found" in response.text[:400].lower()
```

`makerworld_pipeline.archive_model()` 和 `discover_source()` 首先仅委托旧实现，固定新公共入口；`process_jobs.py` 改为从 Pipeline 导入三个函数并删除 `_source_looks_deleted()`、`requests` 与 Cookie header 直连逻辑。

`source_health._probe_auth_endpoints()` 保持现有两个 endpoint 和状态聚合规则，但把每个 `session.get()` 替换为 `makerworld_browser_get(url, raw_cookie=raw_cookie, headers=headers)`。`proxy_config` 参数继续保留兼容；实际 profile 代理由 BrowserTransport 从 `JsonStore` 读取。短信/邮箱验证码登录的 POST 写操作属于账号建立流程，不是抓取 fallback，本任务不修改 `online_accounts.py`。

- [ ] **Step 4: 运行 Pipeline 与子进程边界测试**

Run: `.venv/bin/python -m pytest tests/test_makerworld_pipeline.py tests/test_process_jobs.py tests/test_source_health.py tests/test_source_refresh.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add app/services/makerworld_pipeline/__init__.py app/services/makerworld_pipeline/source_status.py app/services/process_jobs.py app/services/source_health.py tests/test_makerworld_pipeline.py tests/test_process_jobs.py tests/test_source_health.py
git commit -m "refactor: 统一 MakerWorld 任务入口"
```

### Task 8: 将批量发现编排移入 Pipeline

**Files:**
- Create: `app/services/makerworld_pipeline/discovery.py`
- Modify: `app/services/makerworld_pipeline/__init__.py`
- Modify: `app/services/batch_discovery.py`
- Modify: `app/services/archive_worker.py`
- Modify: `app/services/subscriptions.py`
- Modify: `app/services/online_accounts.py`
- Modify: `app/services/source_library.py`
- Modify: `app/services/runtime_engine/archive_adapter.py`
- Modify: `tests/test_batch_discovery.py`
- Modify: `tests/test_makerworld_pipeline.py`
- Modify: `tests/test_subscriptions.py`

**Interfaces:**
- Consumes: BrowserTransport、Task 1/2/3 parser 和现有发现日志。
- Produces: `discover_source()`、`resolve_source_name()`、`discover_account_profile()`、`discover_followed_authors()`、`discover_followed_collections()`、`default_favorites_source()`。

- [ ] **Step 1: 写入口等价与控制请求计数测试**

```python
def test_discover_source_fetches_each_candidate_once_and_keeps_result_shape():
    payload = {"hits": [{"id": 1001, "title": "A"}], "total": 1}
    with patch("app.services.makerworld_pipeline.discovery.makerworld_browser_get_json", return_value=payload) as fetch:
        result = discover_source("https://makerworld.com.cn/zh/@ace/upload", "token=ok", max_pages=1)
    assert set(("items", "expected_total", "mode")).issubset(result)
    assert result["expected_total"] == 1
    assert fetch.call_count >= 1
```

为同一 fixture 同时调用旧 `batch_discovery.discover_batch_model_urls()`，断言返回完全相等；另覆盖空页、API endpoint fallback、HTML fallback、strict expected total 和 CN/Global 不串域。

- [ ] **Step 2: 运行新入口测试并确认 facade 尚未拥有实现**

Run: `.venv/bin/python -m pytest tests/test_makerworld_pipeline.py -q`

Expected: FAIL，patch 目标或请求计数断言失败。

- [ ] **Step 3: 机械移动发现编排并反转 facade**

把 session/header/endpoint 候选、账号来源发现、作者/收藏夹分页、HTML fallback、结果择优及当前顶层 `discover_batch_model_urls()` 移入 `makerworld_pipeline/discovery.py`。新文件只从 BrowserTransport 获取控制面内容；`requests.Session` 只保留为旧函数参数兼容和 header/cookie 容器，禁止调用 `.get()`。

```python
# app/services/batch_discovery.py 最终兼容导出形状
from app.services.makerworld_pipeline.discovery import (
    default_favorites_source as default_favorites_subscription_source,
    discover_account_home_summary as discover_cookie_account_home_summary,
    discover_account_profile as discover_cookie_account_profile,
    discover_followed_authors as discover_cookie_followed_authors,
    discover_followed_authors_from_page as discover_cookie_followed_authors_from_page,
    discover_followed_collections as discover_cookie_followed_collections,
    discover_source as discover_batch_model_urls,
    resolve_source_name as resolve_batch_source_name,
)
```

生产调用方改从 `makerworld_pipeline` 或 `makerworld_parsers.common` 导入。冻结的 `runtime_engine/archive_adapter.py` 只换 import，不能增加注册或启动代码。

- [ ] **Step 4: 运行发现、订阅、账号和 Worker 回归**

Run: `.venv/bin/python -m pytest tests/test_batch_discovery.py tests/test_makerworld_pipeline.py tests/test_subscriptions.py tests/test_source_library.py tests/test_archive_worker_batch_retry.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add app/services/makerworld_pipeline/__init__.py app/services/makerworld_pipeline/discovery.py app/services/batch_discovery.py app/services/archive_worker.py app/services/subscriptions.py app/services/online_accounts.py app/services/source_library.py app/services/runtime_engine/archive_adapter.py tests/test_batch_discovery.py tests/test_makerworld_pipeline.py tests/test_subscriptions.py
git commit -m "refactor: 收敛 MakerWorld 批量发现流程"
```

### Task 9: 将单模型归档编排移入 Pipeline

**Files:**
- Create: `app/services/makerworld_pipeline/archive.py`
- Modify: `app/services/makerworld_pipeline/__init__.py`
- Modify: `app/services/legacy_archiver.py`
- Modify: `app/services/process_jobs.py`
- Modify: `tests/test_makerworld_pipeline.py`
- Modify: `tests/test_legacy_archiver_validation.py`
- Modify: `tests/test_missing_3mf.py`
- Modify: `tests/test_asset_sync.py`

**Interfaces:**
- Consumes: BrowserTransport、`browser_authorize_3mf_download()`、Task 2/4 parser、Task 5 AssetDownloader 和现有 `three_mf_quota`。
- Produces: 与当前 `legacy_archiver.archive_model(...) -> dict` 完全相同的 `makerworld_pipeline.archive_model(...)`；内部保留 `fetch_instance_3mf(...)` 和 `browser_authorize_3mf_download(...)` 供现有 3MF 测试 patch。

- [ ] **Step 1: 写新旧入口等价及授权不重试测试**

```python
def test_archive_pipeline_keeps_result_contract(tmp_path):
    design = {"id": 123, "title": "Demo", "instances": []}
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"props": {"pageProps": {"design": design}}})
        + "</script>"
    )
    with patch("app.services.makerworld_pipeline.archive.fetch_html_with_browser", return_value=html), \
         patch("app.services.makerworld_pipeline.archive.reserve_three_mf_download_slot", side_effect=AssertionError("no instance")):
        result = archive_model(
            url="https://makerworld.com.cn/zh/models/123",
            cookie="",
            download_dir=tmp_path / "archive",
            logs_dir=tmp_path / "logs",
            download_assets=False,
            collect_comments_data=False,
            rebuild_archive=False,
        )
    assert set(("base_name", "work_dir", "missing_3mf", "action", "model_id", "instances", "stats")) == set(result)


def test_archive_pipeline_calls_browser_authorizer_at_most_once_per_instance():
    session = requests.Session()
    browser_result = {
        "status_code": 200,
        "text": "",
        "payload": {"name": "demo.3mf", "url": "https://cdn.example.test/demo.3mf?signature=ok"},
        "verification": {},
    }
    with patch(
        "app.services.makerworld_pipeline.archive.browser_authorize_3mf_download",
        return_value=browser_result,
    ) as authorize:
        name, signed_url, _api_url, failure = fetch_instance_3mf(
            session,
            456,
            "",
            api_url="https://api.bambulab.cn/v1/i/456/3mf",
            origin="https://makerworld.com.cn",
            browser_authorization=True,
            browser_profile_id="profile-cn",
            model_page_url="https://makerworld.com.cn/zh/models/123",
        )
    authorize.assert_called_once()
    assert name == "demo.3mf"
    assert signed_url.startswith("https://cdn.example.test/demo.3mf")
    assert failure["state"] == "available"
```

- [ ] **Step 2: 运行测试并确认 Pipeline 仍委托旧顶层函数**

Run: `.venv/bin/python -m pytest tests/test_makerworld_pipeline.py -q`

Expected: FAIL，patch 目标不存在或授权边界不在新模块。

- [ ] **Step 3: 机械移动 `archive_model()` 编排主体**

将 `legacy_archiver.py` 当前 `fetch_html_with_browser()`、`fetch_design_from_api()`、评论 endpoint/fetch/分页/`collect_comments()`、`browser_authorize_3mf_download()`、`fetch_instance_3mf()` 和 `archive_model()` 函数体原样移动到 `makerworld_pipeline/archive.py`，并显式导入所需 parser、downloader、`three_mf`、quota、日志、离线重建和兼容 helper；禁止 `import *`。本提交不改阶段百分比、fallback、下载条件、返回字段或错误文案。

旧模块保留同签名 lazy facade，避免循环 import：

```python
def archive_model(*args, **kwargs):
    from app.services.makerworld_pipeline.archive import archive_model as pipeline_archive_model

    return pipeline_archive_model(*args, **kwargs)
```

`process_jobs.py` 继续只导入 `makerworld_pipeline.archive_model`。如果新归档模块暂时需要离线重建 helper，应显式从 `legacy_archiver` 导入；后续只在触及该职责时继续提取，不在本任务重写离线页面。

- [ ] **Step 4: 运行单模型、评论、资源和 `3MF` 回归**

Run: `.venv/bin/python -m pytest tests/test_makerworld_pipeline.py tests/test_legacy_archiver_validation.py tests/test_comment_replies.py tests/test_missing_3mf.py tests/test_legacy_archiver_three_mf_wait.py tests/test_asset_sync.py tests/test_profile_details.py -q`

Expected: PASS，返回字典和进度终点均不变。

- [ ] **Step 5: 提交**

```bash
git add app/services/makerworld_pipeline/__init__.py app/services/makerworld_pipeline/archive.py app/services/legacy_archiver.py app/services/process_jobs.py tests/test_makerworld_pipeline.py tests/test_legacy_archiver_validation.py tests/test_missing_3mf.py tests/test_asset_sync.py
git commit -m "refactor: 收敛 MakerWorld 单模型归档流程"
```

### Task 10: 强制传输边界并清理兼容 import

**Files:**
- Create: `tests/test_makerworld_transport_boundary.py`
- Modify: `app/services/source_library.py`
- Modify: `app/services/archive_worker.py`
- Modify: `app/services/subscriptions.py`
- Modify: `app/services/online_accounts.py`
- Modify: `app/services/runtime_engine/archive_adapter.py`
- Modify: `docs/modules/archive.md`
- Modify: `docs/MODULES.md`

**Interfaces:**
- Consumes: Tasks 1-9 的最终公共入口。
- Produces: 自动化架构约束：parser 不含网络 import，Pipeline 不直接使用 `requests.get()` / `Session.get()`，控制面客户端是唯一 `browser_fetch()` 调用者。

- [ ] **Step 1: 写 AST 架构边界测试**

```python
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PARSER_FILES = tuple((ROOT / "app/services/makerworld_parsers").glob("*.py"))
PIPELINE_FILES = tuple((ROOT / "app/services/makerworld_pipeline").glob("*.py"))
CONTROL_GET_FILES = (
    ROOT / "app/services/process_jobs.py",
    ROOT / "app/services/source_health.py",
    *PIPELINE_FILES,
)


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


def _imported_modules(path: Path) -> set[str]:
    result = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def _calls_name(path: Path, name: str) -> bool:
    return any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
        for node in ast.walk(_tree(path))
    )


def _calls_get(path: Path) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"requests", "session"}
        for node in ast.walk(_tree(path))
    )


def test_parsers_have_no_network_or_store_dependencies():
    forbidden = {"requests", "app.core.store", "app.services.cloakbrowser_session"}
    for path in PARSER_FILES:
        imported = _imported_modules(path)
        assert imported.isdisjoint(forbidden), f"{path}: {sorted(imported & forbidden)}"


def test_only_browser_client_calls_browser_fetch():
    service_files = tuple((ROOT / "app/services").glob("*.py"))
    callers = {path.relative_to(ROOT) for path in service_files if _calls_name(path, "browser_fetch")}
    assert callers == {Path("app/services/makerworld_browser_client.py")}


def test_pipeline_does_not_issue_requests_get():
    for path in CONTROL_GET_FILES:
        assert not _calls_get(path), path
```

测试 helper 必须使用 `ast`，不能只做易误报的字符串搜索。AssetDownloader 明确排除在 HTTP 禁止列表之外。

- [ ] **Step 2: 运行测试并定位剩余越界 import**

Run: `.venv/bin/python -m pytest tests/test_makerworld_transport_boundary.py -q`

Expected: 首次运行如有残留则 FAIL，并列出准确文件。

- [ ] **Step 3: 迁移剩余调用方并更新模块契约**

生产模块从 `makerworld_pipeline` 和 `makerworld_parsers` 导入；`batch_discovery.py` 只保留兼容 re-export，`legacy_archiver.py` 保留离线页面重建、归档目录整理和兼容 re-export，不再拥有 MakerWorld 控制面编排。更新 `docs/modules/archive.md` 的对外契约和测试命令，删除“Scrapling 是辅助抓取器”的错误描述，并明确两通道边界。

- [ ] **Step 4: 运行架构与主要集成回归**

Run: `.venv/bin/python -m pytest tests/test_makerworld_transport_boundary.py tests/test_process_jobs.py tests/test_batch_discovery.py tests/test_subscriptions.py tests/test_source_refresh.py tests/test_source_library.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add tests/test_makerworld_transport_boundary.py app/services/source_library.py app/services/archive_worker.py app/services/subscriptions.py app/services/online_accounts.py app/services/runtime_engine/archive_adapter.py docs/modules/archive.md docs/MODULES.md
git commit -m "test: 固化 MakerWorld 双通道边界"
```

### Task 11: Scrapling 兼容退场

**Files:**
- Delete: `app/services/scrapling_fetch.py`
- Delete: `tests/test_scrapling_fetch.py`
- Modify: `app/services/legacy_archiver.py`
- Modify: `app/services/business_logs.py`
- Modify: `app/schemas/models.py`
- Modify: `app/api/config.py`
- Modify: `frontend/src/lib/settingsPayloads.js`
- Modify: `frontend/src/lib/settingsPayloads.test.mjs`
- Modify: `tests/test_business_logs.py`
- Modify: `tests/test_resource_limiter.py`
- Modify: `tests/test_web_routes.py`
- Modify: `tests/test_makerworld_transport_boundary.py`

**Interfaces:**
- Consumes: 当前旧值 `legacy | scrapling_first | scrapling_only`。
- Produces: `v0.17.0` 兼容阶段中后端仍接受并忽略 `scraping_engine`，前端不再发送该字段，运行时无 Scrapling 模块或日志源。

- [ ] **Step 1: 写旧配置兼容与前端 payload 失败测试**

```python
def test_advanced_config_accepts_old_scraping_engine_but_does_not_expose_runtime_choice():
    config = AdvancedRuntimeConfig.model_validate({
        "scraping_engine": "scrapling_only",
        "makerworld_request_limit": 2,
    })
    assert config.scraping_engine == "scrapling_only"  # 仅兼容读取，生产代码不得分支使用
```

```javascript
test("advanced payload omits retired scraping engine", () => {
  assert.deepEqual(buildAdvancedPayload({ makerworld_request_limit: 2 }), {
    remote_refresh_model_workers: 2,
    makerworld_request_limit: 2,
    comment_asset_download_limit: 4,
    three_mf_download_limit: 1,
    disk_io_limit: 1,
  });
});
```

```python
def test_runtime_has_no_scrapling_engine_branches():
    allowed = {Path("app/schemas/models.py")}
    forbidden = {"scraping_engine", "scrapling_first", "scrapling_only"}
    violations = []
    for path in (ROOT / "app").rglob("*.py"):
        relative = path.relative_to(ROOT)
        if relative in allowed:
            continue
        tree = _tree(path)
        tokens = {
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        tokens.update(node.id for node in ast.walk(tree) if isinstance(node, ast.Name))
        tokens.update(node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute))
        if tokens & forbidden:
            violations.append(relative.as_posix())
    assert violations == []
```

- [ ] **Step 2: 运行测试并确认前端仍发送字段**

Run: `npm --prefix frontend test`

Expected: FAIL，实际 payload 多出 `scraping_engine`。

Run: `.venv/bin/python -m pytest tests/test_resource_limiter.py tests/test_makerworld_transport_boundary.py -q`

Expected: 新兼容读取测试 PASS，AST 边界测试 FAIL 并列出尚未删除的生产引用。

- [ ] **Step 3: 删除死模块与运行时痕迹，保留 schema 兼容字段**

```python
class AdvancedRuntimeConfig(BaseModel):
    # v0.17.0 只用于读取旧配置和接受旧客户端请求；运行时固定使用 CloakBrowser。
    scraping_engine: Literal["legacy", "scrapling_first", "scrapling_only"] = "scrapling_first"
```

前端 payload 删除字段；设置保存日志删除 `scraping_engine` 属性；删除 `_scrapling_trace()` 和 `NOISY_INFO_EVENTS` 中的 `("scrapling", "fetch_trace")`。确认没有生产调用者后删除模块及专用测试。历史数据库日志不处理。

- [ ] **Step 4: 运行后端、前端和引用检查**

Run: `.venv/bin/python -m pytest tests/test_business_logs.py tests/test_resource_limiter.py tests/test_web_routes.py tests/test_makerworld_transport_boundary.py -q`

Expected: PASS。

Run: `npm --prefix frontend test`

Expected: PASS。

Run: `rg -n "scrapling_fetch|fetch_with_scrapling|_scrapling_trace" app frontend tests docs/modules docs/MODULES.md`

Expected: 无输出。

- [ ] **Step 5: 提交**

```bash
git add app/services/legacy_archiver.py app/services/business_logs.py app/schemas/models.py app/api/config.py frontend/src/lib/settingsPayloads.js frontend/src/lib/settingsPayloads.test.mjs tests/test_business_logs.py tests/test_resource_limiter.py tests/test_web_routes.py tests/test_makerworld_transport_boundary.py docs/modules/archive.md docs/MODULES.md
git rm app/services/scrapling_fetch.py tests/test_scrapling_fetch.py
git commit -m "refactor: 移除 Scrapling 抓取残留"
```

### Task 12: 性能基线、完整验证与 `v0.17.0` 发布准备

**Files:**
- Modify: `tests/test_makerworld_pipeline.py`
- Modify: `VERSION`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `tests/test_release_contract.py`

**Interfaces:**
- Consumes: 完整 Pipeline。
- Produces: 控制请求计数回归、资源回收断言和版本一致的 `0.17.0` 发布提交。

- [ ] **Step 1: 增加请求次数和大文件通道回归**

```python
def test_archive_does_not_send_signed_asset_through_browser(tmp_path):
    signed_url = "https://cdn.example.test/model.3mf?signature=secret"

    response = MagicMock()
    response.__enter__.return_value = response
    response.raise_for_status.return_value = None
    response.iter_content.return_value = [b"PK\x03\x04valid-3mf"]
    session = MagicMock()
    session.get.return_value = response

    with patch("app.services.makerworld_browser_client.makerworld_browser_get") as browser:
        download_file(session, signed_url, tmp_path / "model.3mf")
    browser.assert_not_called()
    assert (tmp_path / "model.3mf").read_bytes().startswith(b"PK")


def test_discovery_control_request_count_does_not_double_on_success():
    payload = {"hits": [{"id": 1001, "title": "A"}], "total": 1}
    with patch(
        "app.services.makerworld_pipeline.discovery._service_endpoint_candidates",
        return_value=["https://api.bambulab.cn/v1/design-service/designs"],
    ), patch(
        "app.services.makerworld_pipeline.discovery.makerworld_browser_get_json",
        return_value=payload,
    ) as fetch:
        result = _api_get_json(
            requests.Session(),
            "https://makerworld.com.cn/zh/@ace/upload",
            "",
            "design-service",
            "/designs",
            {"offset": 0, "limit": 20},
        )
    assert result == payload
    fetch.assert_called_once()
```

请求次数使用单 endpoint fixture 固定为 1，禁止使用线上波动数据；临时页面清理继续由已有 `test_cloakbrowser_session.py` 覆盖。

- [ ] **Step 2: 运行定向与完整测试**

Run: `.venv/bin/python -m pytest tests/test_makerworld_pipeline.py tests/test_makerworld_browser_client.py tests/test_cloakbrowser_session.py tests/test_asset_downloader.py -q`

Expected: PASS。

Run: `.venv/bin/python -m pytest -q`

Expected: PASS。

Run: `npm --prefix frontend test`

Expected: PASS。

Run: `npm --prefix frontend run build`

Expected: PASS。

- [ ] **Step 3: 更新 `0.17.0` 版本与说明**

`VERSION`、前端 package/lock 和静态 release 测试统一为 `0.17.0`。README 最新三版加入 `v0.17.0`，将第四版移入折叠历史；CHANGELOG 记录统一 Pipeline、Parser/Downloader 拆分、控制面单通道、Scrapling 兼容退场和不改变历史数据。

```text
0.17.0
```

- [ ] **Step 4: 运行发布门禁**

Run: `.venv/bin/python scripts/check_release_version.py --tag v0.17.0`

Expected: `release version check passed: v0.17.0`。

Run: `.venv/bin/python -m pytest tests/test_release_contract.py -q`

Expected: PASS。

Run: `node --check app/services/cloakbrowser_bridge.mjs`

Expected: exit code 0。

Run: `docker compose config --quiet`

Expected: exit code 0。

Run: `git diff --check`

Expected: 无输出。

- [ ] **Step 5: 只提交发布相关文件**

```bash
git add VERSION frontend/package.json frontend/package-lock.json README.md CHANGELOG.md tests/test_release_contract.py tests/test_makerworld_pipeline.py
git commit -m "chore: 准备 v0.17.0 抓取流水线版本"
```

- [ ] **Step 6: 停在发布授权门禁**

运行 `git status --short --branch` 并汇报测试结果。只有用户明确要求“推送”后，才能推送 `main`、创建 annotated `v0.17.0` Tag，并验证 GitHub Actions、GitHub Release 和 `ghcr.io/s450586793/makerhub:v0.17.0`。

### Task 13: 在 `v0.17.0` 已发布后移除兼容 schema

**Prerequisite:** GitHub Release `v0.17.0` 和对应 GHCR 镜像均已存在。未满足时不得执行本任务。

**Files:**
- Modify: `app/schemas/models.py`
- Modify: `tests/test_resource_limiter.py`
- Modify: `tests/test_config_payloads.py`
- Modify: `VERSION`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `tests/test_release_contract.py`

**Interfaces:**
- Consumes: 旧配置可能仍包含 `advanced.scraping_engine`。
- Produces: `AdvancedRuntimeConfig` 不再暴露该字段；Pydantic 加载旧 JSON 和旧客户端 POST 时忽略该额外字段；版本 `0.17.1`。

- [ ] **Step 1: 写旧数据兼容失败测试**

```python
def test_advanced_config_ignores_retired_scraping_engine():
    config = AdvancedRuntimeConfig.model_validate({
        "scraping_engine": "scrapling_only",
        "makerworld_request_limit": 3,
    })
    assert not hasattr(config, "scraping_engine")
    assert config.makerworld_request_limit == 3


def test_app_config_loads_old_advanced_payload_without_round_tripping_engine():
    config = AppConfig.model_validate({"advanced": {"scraping_engine": "legacy"}})
    assert "scraping_engine" not in config.model_dump()["advanced"]
```

- [ ] **Step 2: 运行测试并确认字段仍存在**

Run: `.venv/bin/python -m pytest tests/test_resource_limiter.py -q`

Expected: FAIL，`hasattr(config, "scraping_engine")` 为真。

- [ ] **Step 3: 删除 schema 字段并更新断言**

从 `AdvancedRuntimeConfig` 删除 `scraping_engine`。保持 Pydantic 默认 extra-ignore 行为；不要添加数据库迁移，不要重写历史配置文件。删除 Task 11 中仅为兼容保留的测试与注释。

- [ ] **Step 4: 更新 `0.17.1` 并运行完整门禁**

版本文件统一为 `0.17.1`，README/CHANGELOG 记录“完成废弃抓取引擎字段清理，旧配置继续兼容读取”。

Run: `.venv/bin/python -m pytest -q`

Expected: PASS。

Run: `npm --prefix frontend test`

Expected: PASS。

Run: `npm --prefix frontend run build`

Expected: PASS。

Run: `.venv/bin/python scripts/check_release_version.py --tag v0.17.1`

Expected: `release version check passed: v0.17.1`。

Run: `git diff --check`

Expected: 无输出。

- [ ] **Step 5: 提交并停在发布授权门禁**

```bash
git add app/schemas/models.py tests/test_resource_limiter.py tests/test_config_payloads.py VERSION frontend/package.json frontend/package-lock.json README.md CHANGELOG.md tests/test_release_contract.py
git commit -m "chore: 完成废弃抓取引擎配置清理"
```

只有用户明确要求推送后，才能创建并推送 annotated `v0.17.1` Tag 和验证发布产物。
