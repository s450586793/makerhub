# 分享 / 移动端导入

## 职责

- 创建单次模型分享，生成分享码/访问码。
- 接收分享码并预览、去重、导入为本地模型。
- 管理已分享列表和分享过期/撤销。
- 支持 iOS 快捷指令上传文件到 MakerHub。
- 维护移动端导入 Token 和快捷指令文档。

## 不负责

- 不做订阅分享。
- 不替接收方做持续源端刷新；分享导入的是一次性本地模型。
- 不把分享端公网地址、token、access code 明文写日志。
- 不在前端直接解析分享文件内容。

## 对外契约

### HTTP API

- `POST /api/config/sharing`
- `POST /api/config/sharing/test`
- `GET /api/config/sharing/check`
- `POST /api/sharing/create`
- `GET /api/sharing/shares`
- `POST /api/sharing/shares/{share_id}/code`
- `DELETE /api/sharing/shares/{share_id}`
- `POST /api/sharing/shares/cleanup`
- `POST /api/sharing/receive/preview`
- `POST /api/sharing/receive/import`
- `GET /api/public/makerhub/ping`
- `GET /api/public/shares/{share_id}/manifest`
- `GET /api/public/share-access/{access_code}/manifest`
- `GET /api/public/shares/{share_id}/files/{file_id}`
- `POST /api/config/mobile-import/token`
- `POST /api/config/mobile-import/disable`
- `GET /api/mobile-import/ping`
- `GET /api/mobile-import/ping-ipv4`
- `POST /api/mobile-import`
- `POST /api/mobile-import/raw`
- `POST /api/mobile-import/raw-ipv4`

### Service 函数/类

- 分享逻辑目前集中在 `app/api/config.py` 的 share helper 中。
- 移动端上传最终调用 `local_import_upload.py` 和本地整理链路。
- `AuthManager` 校验 `mobile_import` Token 权限。
- `TaskStateStore` 记录移动端上传/整理进度。

## 数据和目录

- Postgres/JSON state:
  - `makerhub_json_state:app_config` 中 sharing/mobile import 设置。
  - 分享记录 state。
  - `makerhub_json_state:organize_tasks` 用于上传后整理进度。
- 文件:
  - `/app/local`：移动端上传落地后进入本地整理。
  - `docs/ios-makerhub-shortcut.md`
  - `docs/makerhub-ios-shortcut-template.json`
  - `docs/推送到 MakerHub.shortcut`

## 常用测试命令

```bash
.venv/bin/python -m unittest tests.test_share_receive_security tests.test_mobile_import tests.test_upload_limits
```

涉及快捷指令说明或设置页时再跑：

```bash
npm --prefix frontend run build
```

## 修改时不能破坏

- 分享码不能明文暴露公网地址、token、access code。
- 后台解码分享码时不能把真实地址、token、access code 写进日志。
- 有效期内同一模型二次分享应提示已分享过，而不是生成重复分享记录。
- 接收分享时必须先检查本地/订阅/模型库是否已有，发现重复要提示并阻止导入。
- iOS 快捷指令上传的原始文件名要尽量保留，不能总变成 `wechat-upload`。
- 移动端导入的上传阶段和整理阶段要接入同一个进度展示。
- 快捷指令文档中的示例地址不能暴露真实部署地址。

## 给 Codex 的上下文入口

改分享码、已分享列表、接收分享、iOS 快捷指令时，先读：

- `app/api/config.py` 中 share/mobile-import helper 和路由段落
- `app/services/local_import_upload.py`
- `app/services/auth.py`
- `frontend/src/components/ShareDialog.vue`
- `frontend/src/pages/SettingsPage.vue` 中分享和 Token 设置
- `docs/ios-makerhub-shortcut.md`

