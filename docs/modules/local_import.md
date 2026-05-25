# 本地导入 / 本地整理

## 职责

- 统一处理三个入口：
  - Web 端上传。
  - iOS 快捷指令上传。
  - `/app/local` 文件夹放入文件/文件夹。
- 按文件类型整理本地模型：
  - 单/多个纯 3MF：按 3MF 指纹和模型 identity 分模型或合并配置。
  - STL/STEP/OBJ 等模型文件：作为同一个本地模型导入。
  - 文件夹/zip/rar：按图片、模型文件、PDF/Excel/附件等分类，生成本地模型。
  - 混合 3MF 与其他文件：按文件夹/包整体识别为同一模型，不走纯 3MF 批量拆分。
- 维护本地整理任务进度、最近失败、跳过的不支持文件。
- 支持编辑本地模型：标题、描述、图册、封面、模型文件、附件增删。
- 对没有图片的本地模型排队生成 Three.js 预览封面。

## 不负责

- 不抓取 MakerWorld 源端数据。
- 不创建订阅，也不刷新源端评论。
- 不把大模型文件写入数据库。
- 不在前端直接解压大文件或渲染历史全库封面。

## 对外契约

### HTTP API

- `POST /api/local-library/import`
- `POST /api/local-library/merge`
- `GET /api/mobile-import/ping`
- `GET /api/mobile-import/ping-ipv4`
- `POST /api/mobile-import`
- `POST /api/mobile-import/raw`
- `POST /api/mobile-import/raw-ipv4`
- `POST /api/models/{model_dir:path}/local/files`
- `DELETE /api/models/{model_dir:path}/local/files`
- `POST /api/models/{model_dir:path}/local/images`
- `DELETE /api/models/{model_dir:path}/local/images`
- `POST /api/models/{model_dir:path}/local/preview-image`
- `POST /api/models/{model_dir:path}/local/preview-image/failure`
- `POST /api/config/organizer`
- `POST /api/tasks/organize/clear`

### Service 函数/类

- `save_local_import_uploads()` / local import upload helpers
- `LocalOrganizerService.start()` / `stop()` / `run_once()`
- `LocalOrganizerService.process_candidate()`
- `LocalOrganizerService._inspect_3mf()` / `_build_meta()` / `_organize_file()`
- `update_local_model_metadata()`
- `update_local_model_description()`
- `add_local_model_file()` / `delete_local_model_file()`
- `add_local_model_image()` / `delete_local_model_image()` / `set_local_model_cover_image()`
- `save_local_model_generated_preview()` / `save_local_model_generated_preview_failure()`
- `mark_local_preview_queue_updated()` / `run_local_preview_generation_once()`

## 数据和目录

- Postgres/JSON state:
  - `makerhub_json_state:organize_tasks`
  - `makerhub_json_state:model_flags`
  - `archive_model_index` 最终模型索引。
- 文件:
  - `/app/local`：导入入口和本地整理工作目录。
  - `/app/archive/LOCAL_*`：本地模型最终目录。
  - `/app/logs/organizer.log`
  - `/app/state/organize_tasks.json` 仅作为旧部署迁移输入。

## 常用测试命令

```bash
.venv/bin/python -m unittest tests.test_local_import_upload tests.test_local_organizer tests.test_local_model_edit tests.test_local_preview_worker tests.test_mobile_import tests.test_upload_limits
```

涉及详情页编辑 UI 时再跑：

```bash
npm --prefix frontend run build
```

## 修改时不能破坏

- 三个入口必须走同一套整理规则，不能 Web 上传一套、iOS 上传一套、local 文件夹一套。
- 上传完成不等于整理完成；前端成功提示应以后台整理状态/快照可见为准。
- 大文件上传进度可以展示上传阶段，但后续整理必须后台运行，不能卡住前端。
- zip/rar 中有坏包或不支持文件时应记录跳过，不应中断整个模型导入。
- 文件夹内多层 STL 要去重，但不能把不同模型错误合成一个模型。
- 纯 3MF 批量导入和混合包导入的分组规则不能混淆。
- 已生成或失败的 Three.js 预览状态要写入 meta，避免反复重跑吃满 CPU。
- 编辑本地模型后必须刷新详情和模型索引，否则用户会看到“保存成功但列表没变”。

## 给 Codex 的上下文入口

改上传、整理、local 文件夹、iOS 导入、本地模型编辑时，先读：

- `app/services/local_import_upload.py`
- `app/services/local_organizer.py`
- `app/services/local_organizer_worker.py`
- `app/services/local_model_edit.py`
- `app/services/local_model_merge.py`
- `app/services/local_preview_worker.py`
- `app/api/config.py` 中 `/local-library`、`/mobile-import`、`/models/*/local` 段落
- `frontend/src/pages/OrganizerPage.vue`
- `frontend/src/pages/ModelDetailPage.vue` 的本地编辑弹窗
