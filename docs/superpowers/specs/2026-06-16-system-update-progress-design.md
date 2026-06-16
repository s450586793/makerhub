# 系统更新阶段进度条设计

日期：2026-06-16

## 背景

设置页“系统 / 容器更新”目前可以读取当前版本、GitHub 最新版本、更新状态、容器信息和更新日志。点击网页一键更新后，后端会写入 `system_update` 状态，并通过状态事件通知前端刷新。

现有状态已经包含：

- `status`：`idle`、`queued`、`launching_helper`、`running`、`pending_startup`、`succeeded`、`failed`。
- `phase`：`queued`、`launching_helper`、`pulling`、`updating_web`、`updating_worker`、`recreating`、`switching`、`starting`、`completed`、`failed` 等。
- `message`：当前更新步骤的中文说明。

这些字段足够展示“阶段式进度条”。现有实现没有 Docker image pull 的字节级进度，也没有每个 layer 的下载进度。

## 决策

第一阶段实现阶段式进度条，不实现真实下载百分比。

理由：

- 用户主要需要知道更新已经开始、当前正在做什么、是否卡在重启等待或失败。
- 阶段式进度可以直接复用现有 `status`、`phase`、`message`，风险低。
- Docker pull streaming progress 需要处理多 layer、helper 容器日志、网络断开和 App 重启期间的状态恢复，复杂度明显更高。
- 阶段式展示不会伪造精确下载速度，只表达当前流程阶段。

## 用户体验

在设置页“容器更新”卡片中，更新状态区域下方新增一条紧凑进度区。

展示内容：

- 当前阶段标题，例如 `正在拉取镜像`、`正在更新 Worker`、`正在替换 App`、`等待服务恢复`。
- 阶段进度条。
- 当前 `systemUpdate.message`。
- 失败时显示错误态和 `last_error` / `message`。

显示规则：

- 空闲且没有最近更新结果时，不显示进度条，只保留现有状态文案。
- `queued`、`launching_helper`、`running`、`pending_startup` 时显示进度条。
- `succeeded` 时显示 100% 和完成消息。
- `failed` 时显示失败态，进度条停留在对应阶段进度，不显示为 100%。

重启期间行为：

- 当前端请求 `/api/system/update` 失败且更新此前处于 active 状态时，保留本地已有进度展示。
- 文案显示“正在等待服务重启完成，页面恢复后会自动继续读取状态。”。
- 页面恢复后再次读取后端状态，若后端返回 `succeeded`，进度变为 100%。

## 阶段映射

第一阶段由前端把 `phase` 映射成展示进度：

| Phase | Label | Progress |
| --- | --- | --- |
| `queued` | 已提交更新 | 5 |
| `launching_helper` | 启动更新 helper | 10 |
| `pulling` | 正在拉取镜像 | 25 |
| `pulling_web` | 正在拉取 Web 镜像 | 30 |
| `creating_web` | 正在创建 Web 容器 | 40 |
| `switching_web` | 正在切换 Web 容器 | 48 |
| `starting_web` | 正在启动 Web 容器 | 55 |
| `web_updated` | Web 容器已更新 | 58 |
| `updating_worker` | 正在更新 Worker | 60 |
| `pulling_worker` | 正在拉取 Worker 镜像 | 62 |
| `creating_worker` | 正在创建 Worker 容器 | 68 |
| `switching_worker` | 正在切换 Worker 容器 | 74 |
| `starting_worker` | 正在启动 Worker 容器 | 80 |
| `worker_updated` | Worker 容器已更新 | 84 |
| `recreating` | 正在替换 App 容器 | 88 |
| `switching` | 正在切换 App 容器 | 92 |
| `starting` | 等待服务恢复 | 96 |
| `completed` | 更新完成 | 100 |
| `version_mismatch` | 版本校验失败 | 96 |
| `failed` | 更新失败 | 0 |

未识别的 active phase 使用 50%，并展示后端 `message`，避免进度区空白。

如果 `status` 是 `succeeded`，进度强制为 100%。如果 `status` 是 `failed` 且 phase 不是 `failed`，进度使用对应 phase 的进度，方便看出失败发生在哪一步。

## 实现范围

前端：

- 在 `SettingsPage.vue` 新增系统更新进度 computed。
- 在容器更新卡片中新增进度条区域。
- 在 `frontend/src/style.css` 增加系统更新进度条样式，兼容深色、浅色和自动主题。
- 保持现有刷新状态、按钮 disabled 逻辑和状态事件订阅。

后端：

- 第一阶段不改后端 API。
- 不新增 `progress` 字段。
- 不采集 Docker pull 字节级进度。

## 错误和边界

- API 重启短暂不可用时，不清空已有进度。
- `systemUpdate.message` 为空时，使用阶段默认文案。
- `last_error` 有值时，失败态优先展示 `last_error`。
- 不让进度条改变按钮禁用逻辑；是否能触发更新仍由 `canTriggerSystemUpdate` 决定。
- 失败后再次点击更新前，页面仍展示上次失败状态，直到后端状态被新请求覆盖。

## 测试范围

前端单元测试：

- 阶段映射函数覆盖 active、success、failed、unknown phase。
- API 重启失败时保留 active 文案的逻辑保持不变。

前端构建：

```bash
npm --prefix frontend run build
```

如后续改后端状态字段，再追加：

```bash
.venv/bin/python -m unittest tests.test_self_update
```

## 非目标

- 不展示 Docker layer 级下载进度。
- 不展示下载速度、剩余时间或镜像大小。
- 不新增 helper 日志流接口。
- 不改变网页一键更新流程。
- 不改变系统更新状态持久化结构。

## 验收标准

- 点击网页更新后，用户能立即看到进度条和当前阶段。
- 更新过程中阶段会随 `system_update` 状态刷新推进。
- App 重启期间页面显示等待恢复，不误报失败。
- 更新完成后显示 100% 和完成状态。
- 更新失败时显示失败样式和错误消息。
- 深色、浅色和自动主题下进度条、文案和错误态都清晰可读。
