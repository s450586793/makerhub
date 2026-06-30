# 订阅 / 来源库

## 职责

- 管理作者、收藏夹、合集订阅的增删改查和定时同步。
- 保存订阅扫描状态：扫描到的模型、已归档、缺失、失败、源端数量闭环。
- Cookie 保存后自动同步关注作者、默认收藏夹、关注合集，并生成订阅来源。
- 定期刷新关注来源清单，发现新增关注后添加订阅。
- 构建订阅库、来源库、来源分组、来源卡头像、来源快照图。
- 保持收藏夹排序接近 MakerWorld 源端顺序，尤其默认收藏夹按加入收藏时间。

## 不负责

- 不直接下载模型文件；发现新模型后交给归档队列。
- 不负责单模型详情展示。
- 不保存账号密码；线上账号/Cookie 保存归 Core。
- 不把收藏夹缺失直接判定为模型源端删除。

## 对外契约

### HTTP API

- `GET /api/subscriptions`
- `POST /api/subscriptions`
- `PUT /api/subscriptions/{subscription_id}`
- `DELETE /api/subscriptions/{subscription_id}`
- `POST /api/subscriptions/{subscription_id}/sync`
- `POST /api/config/subscriptions`
- `GET /api/source-library`
- `GET /api/source-library/snapshots/{filename}`
- `GET /api/source-library/sources/{source_type}/{source_key}`
- `GET /api/source-library/states/{state_key}`

### Service 函数/类

- `SubscriptionManager.list_payload()`
- `SubscriptionManager.add_subscription()`
- `SubscriptionManager.update_subscription()`
- `SubscriptionManager.delete_subscription()`
- `SubscriptionManager.sync_subscription_now()`
- `discover_cookie_followed_authors()`
- `discover_cookie_followed_collections()`
- `default_favorites_subscription_source()`
- `build_source_library_payload()`
- `build_subscription_overview_payload()`
- `build_source_group_models_payload()`
- `build_state_group_models_payload()`
- `refresh_subscription_source_metadata()`
- `refresh_source_preview_snapshots()`
- `source_identity_key()`

## 数据和目录

- Postgres/JSON state:
  - `makerhub_json_state:app_config` 中的 subscriptions 配置。
  - `makerhub_json_state:subscriptions_state`
  - Cookie 来源同步 inventory/state。
  - 来源 metadata cache。
  - `archive_model_index` 用于来源库聚合模型。
- 文件:
  - `/app/config/state`：来源快照、锁和兼容挂载目录。
  - `/app/config/logs`：兼容日志目录；运行期业务日志写入 Postgres。
  - 来源快照图片通常保存在运行状态/快照目录，并通过 `/api/source-library/snapshots/{filename}` 暴露。

## 常用测试命令

```bash
.venv/bin/python -m unittest tests.test_subscriptions tests.test_source_library tests.test_source_health tests.test_batch_discovery
```

涉及订阅页面 UI 时再跑：

```bash
npm --prefix frontend run build
```

## 修改时不能破坏

- 保存 Cookie 后的关注来源同步应后台执行，设置页保存不能长时间卡住。
- 国区和国际区来源 identity 要分开，默认收藏夹 URL 不能串站。
- 同一来源重复出现时要用 canonical URL/identity 合并，不能一份默认收藏夹显示两次。
- 来源卡数量应优先使用最新同步状态，预览遮罩、已归档数、源端总数要使用同一口径。
- 收藏夹少扫或移除收藏不能自动把模型标为源端删除。
- 来源快照应由 worker/后台生成，前端优先使用 `preview_snapshot_url`，避免打开订阅库时逐图加载过慢。
- Cookie/Token 相关字段不得出现在订阅日志里。

## 给 Codex 的上下文入口

改订阅库、关注作者/收藏夹同步、来源卡、来源排序时，先读：

- `app/services/subscriptions.py`
- `app/services/source_library.py`
- `app/services/batch_discovery.py` 中 Cookie 关注/收藏发现函数
- `app/services/source_health.py`
- `frontend/src/pages/SubscriptionsPage.vue`
- `frontend/src/pages/SubscriptionsManagePage.vue`
- `frontend/src/components/SourceLibraryCard.vue`
