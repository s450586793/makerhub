# 前端 / 设置页 / 设计系统

## 职责

- Vue SPA 路由、AppShell、页面导航和通用视觉体验。
- 模型库、订阅库、本地库、任务、设置、日志等页面的前端展示。
- 设置页分区：线上账号、代理、Token、分享、移动端导入、订阅、源端刷新、高级、系统更新等。
- 深色模式/浅色模式/自动模式和设计系统变量。
- 前端 API client、页面缓存、Three.js 详情预览弹窗。
- 维护当前 MakerHub 新 UI 风格，具体规则见 `docs/UI_DESIGN_SYSTEM.md`。

## 不负责

- 不在前端执行重型归档、解压、STL 扫描或历史全库封面生成。
- 不绕过后端权限直接访问文件系统。
- 不把业务状态只保存在组件内存里。
- 不在前端决定 Cookie/Token 是否有效；前端只展示后端结果。

## 对外契约

### 前端入口

- `frontend/src/main.js`
- `frontend/src/App.vue`
- `frontend/src/router.js`
- `frontend/src/layouts/AppShell.vue`
- `frontend/src/lib/api.js`
- `frontend/src/lib/appState.js`
- `frontend/src/lib/theme.js`
- `frontend/src/style.css`

### 主要页面/组件

- `DashboardPage.vue`
- `ModelsPage.vue`
- `ModelDetailPage.vue`
- `OrganizerPage.vue`
- `SubscriptionsPage.vue`
- `SubscriptionsManagePage.vue`
- `TasksPage.vue`
- `RemoteRefreshPage.vue`
- `SettingsPage.vue`
- `LogsPage.vue`
- `ModelCard.vue`
- `SourceLibraryCard.vue`
- `ShareDialog.vue`
- `ThemeSegment.vue`

## 数据和目录

- 前端构建产物：`frontend/dist/`
- FastAPI 静态分发：`app/api/web.py` 和 `app/main.py`
- 旧模板/静态文件仍存在于 `app/templates/`、`app/static/`，但新功能优先改 Vue。

## 常用测试命令

```bash
npm --prefix frontend run build
```

涉及 API 契约时追加对应后端模块测试。

## 修改时不能破坏

- 顶部功能栏高度要和首页、模型库、订阅库、本地库等保持一致。
- 设置页“线上账号”和“代理”是分开的；HTTP 代理不放在线上账号里。
- Token 页面要展示名称、过期时间、权限、Token 字符，并允许撤销/生成。
- 主题应支持 `light`、`dark`、`auto`。
- 深色模式下不要出现硬编码白线、白底卡片或不可读文字。
- 按钮/输入/卡片要遵守 `docs/UI_DESIGN_SYSTEM.md` 和 `AGENTS.md`，避免魔法数字和过大圆角。
- 不要把界面改回旧的白底浅灰 SaaS 风格；当前设计以深色紧凑工作台为基准。
- 3MF/文件/附件列表不要拆成一堆重边框小块，优先使用紧凑列表或表格。
- 长任务按钮点击后要及时反馈，并通过任务状态刷新，不让页面卡死。
- 任何复制失败要有 fallback 或明确原因。

## 给 Codex 的上下文入口

改 UI、设置页、深色模式、页面布局时，先读：

- `docs/UI_DESIGN_SYSTEM.md`
- `AGENTS.md`
- `frontend/src/style.css`
- `frontend/src/pages/SettingsPage.vue`
- 涉及页面的 `.vue` 文件
- `frontend/src/lib/api.js`
- 后端对应 API 路由段落
