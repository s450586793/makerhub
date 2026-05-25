# 模型库 / 详情 / 附件

## 职责

- 构建首页、模型库、详情页需要的模型数据。
- 读取 `archive_model_index` 或按模型 `meta.json` 主数据重建当前快照，生成模型卡片列表。
- 标准化模型详情：主图、图集、打印配置、3MF/STL/STEP/OBJ 文件、附件、评论、作者、统计信息。
- 处理模型 flags：收藏、已打印、本地删除、源端删除展示。
- 提供附件上传/删除、下载全部文件、Bambu Studio 打开链接。
- 本地模型详情页隐藏不适用的源端评论板块，并支持本地编辑入口。

## 不负责

- 不执行 MakerWorld 抓取和下载。
- 不扫描订阅来源；只消费订阅/来源库写入的 metadata。
- 不处理上传 zip/rar 的整理过程。
- 不把大文件写进数据库。

## 对外契约

### HTTP API

- `GET /api/dashboard`
- `GET /api/models`
- `GET /api/models/{model_dir:path}`
- `GET /api/models/{model_dir:path}/comments`
- `GET /api/models/{model_dir:path}/download-all`
- `POST /api/models/{model_dir:path}/bambu-studio-link`
- `GET /api/public/bambu-studio/models/{model_dir:path}/files/{file_name}`
- `POST /api/models/{model_dir:path}/attachments`
- `DELETE /api/models/{model_dir:path}/attachments/{attachment_id}`
- `GET /api/models/flags`
- `POST /api/models/flags/favorite`
- `POST /api/models/flags/printed`
- `POST /api/models/flags/deleted`
- `POST /api/models/delete`

### Service 函数

- `get_archive_snapshot(force=False)`
- `invalidate_archive_snapshot(reason="")`
- `invalidate_model_detail_cache(model_dir="")`
- `get_decorated_models()`
- `load_archive_models(include_detail=False)`
- `get_model_detail(model_dir, include_detail=True)`
- `get_model_comments_page(model_dir, offset=0, limit=...)`
- `upsert_archive_snapshot_model(model_dir, reason="", broadcast=True)`
- `upsert_archive_model_index()` / `delete_archive_model_index()`
- `create_manual_attachment()` / `delete_manual_attachment()`

## 数据和目录

- Postgres:
  - `archive_model_index`
  - `makerhub_json_state:model_flags`
- 文件:
  - `/app/data/<model_dir>/meta.json`
  - `/app/data/<model_dir>/images/`
  - `/app/data/<model_dir>/files/`
  - `/app/data/<model_dir>/attachments/`
  - 附件 sidecar 文件由 `model_attachments.py` 管理。

## 常用测试命令

```bash
.venv/bin/python -m unittest tests.test_archive_model_index tests.test_catalog_placeholder_cover tests.test_model_attachments tests.test_model_downloads tests.test_comment_replies tests.test_profile_details tests.test_profile_rating
```

前端涉及模型库/详情页时再跑：

```bash
npm --prefix frontend run build
```

## 修改时不能破坏

- 模型列表不能每次都全量重扫所有图片和元数据；应优先使用索引/快照。
- 本地模型和 MakerWorld 归档模型可以重复存在，不能因为文件指纹相同就自动覆盖其中一份。
- 本地导入模型不展示评论板块。
- 详情页的文件列表、附件和下载按钮必须能处理 3MF、STL、STEP、OBJ、PDF、Excel 等类型。
- 任何文件下载路径都要限制在模型目录内，不能允许路径穿越。
- 源端删除是标记，不应自动删除本地文件。
- Bambu Studio 打开链接必须有过期签名，不应暴露无鉴权永久公网下载入口。

## 给 Codex 的上下文入口

改模型库、详情页、附件、评论、下载时，先读：

- `app/services/catalog.py`
- `app/services/archive_model_index.py`
- `app/services/model_attachments.py`
- `app/api/config.py` 中 `/models` 相关段落
- `frontend/src/pages/ModelsPage.vue`
- `frontend/src/pages/ModelDetailPage.vue`
- `frontend/src/components/ModelCard.vue`
