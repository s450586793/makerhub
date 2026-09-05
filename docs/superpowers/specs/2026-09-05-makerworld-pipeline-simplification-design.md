# MakerWorld 抓取流水线精简设计

日期：2026-09-05

## 背景

MakerHub 当前并不是简单地同时运行多个独立爬虫，而是由多种职责交织形成了看似多引擎的抓取链路：

- `makerworld_browser_client.py` 通过 CloakBrowser 和 `puppeteer-core` 获取受登录态、Cloudflare 或 MakerWorld 风控影响的 HTML / JSON 控制面内容。
- `legacy_archiver.py` 和 `batch_discovery.py` 各自承担请求编排、页面解析、业务归一化和部分资源下载，文件分别约为 7180 行和 3463 行。
- `requests` 用于静态资源和已签名文件的流式下载，也仍残留少量 MakerWorld 页面直连检查。
- `BeautifulSoup` 只在 Python 进程内解析已经取得的 HTML，不建立网络连接。
- `scrapling_fetch.py` 名称上代表另一种抓取引擎，实际实现只是 `requests.get()` 包装；当前没有生产调用者，只有配置、测试、日志白名单和文档残留。

把上述能力全部迁入 CloakBrowser 不会减少解析逻辑，反而会让图片、附件和大型 `3MF` 占用 CDP 通道。当前 bridge 需要缓冲响应正文，响应上限为 32 MB；同时 CloakBrowser profile 操作需要串行，线上已经出现过较高 CPU、内存和 `HTTP 502`。因此，本次采用“一条业务流水线、两个传输通道”，而不是“所有内容全部浏览器化”。

本设计延续 `2026-07-26-cloakbrowser-only-fetch-design.md` 已确定的边界，并落实 `2026-06-04-project-simplification-optimization-design.md` 中对归档巨型文件分阶段拆分的要求。

## 目标

1. 订阅、批量发现、手动归档和源端刷新复用同一条 MakerWorld 发现与归档流水线。
2. 所有 MakerWorld HTML / JSON 控制面请求只经过 `makerworld_browser_client.py`，不在业务模块中直连。
3. HTML / JSON 解析保持为无网络副作用的 Python 纯函数，继续使用标准 JSON 解析和 `BeautifulSoup`。
4. 图片、头像、附件和已取得签名直链的 `3MF` 继续使用 `requests` 流式下载。
5. 移除无生产价值的 Scrapling 伪引擎、设置项和专用测试，减少用户可见但不起作用的配置。
6. 逐步缩小 `legacy_archiver.py` 和 `batch_discovery.py`，同时保持现有调用契约、任务状态和归档结果不变。
7. 降低 CloakBrowser 的请求量、响应体积、标签页数量和资源占用，不降低当前静态资源下载并发能力。

## 非目标

- 不更换 CloakBrowser、`puppeteer-core`、`requests` 或 `BeautifulSoup` 技术栈。
- 不把静态资源和大型文件转发到 CloakBrowser。
- 不重写整个归档器，也不在同一提交中同时移动代码和改变解析规则。
- 不修改数据库 schema、任务队列、归档目录、模型索引格式、账号 Gate 或历史数据。
- 不恢复或迁移当前冻结的 Runtime Engine。
- 不改变每日 `3MF` 限额、并发数、重试次数或验证策略。
- 不删除历史日志中已有的 `scrapling` 字样，也不批量改写旧设置数据。

## 方案选择

### 采用：统一 Pipeline，保留两个传输通道

```text
手动归档 / 订阅 / 批量来源 / 源端刷新
                    |
                    v
          MakerWorld Pipeline
          |         |         |
          |         |         +--> 状态与结果适配（沿用现有契约）
          |         |
          |         +--> Python Parser
          |              JSON + BeautifulSoup，无网络访问
          |
          +--> BrowserTransport
          |    makerworld_browser_client.py
          |    HTML / JSON 控制面
          |
          +--> 3MF Authorizer
          |    现有 Puppeteer 真实点击授权
          |
          +--> AssetDownloader
               requests 流式下载
               图片 / 附件 / 已签名 3MF
```

Pipeline 统一“何时发现、解析、下载和产出结果”，但不强迫所有字节通过同一种网络通道。

### 不采用：全部迁入 CloakBrowser

优点是表面上只有一个 HTTP 实现，缺点是每个静态文件都要占用浏览器页面或 CDP 请求，二进制数据还需要在 Node 与 Python 之间传输。它会放大目前的 CloakBrowser CPU、内存和 502 问题，也会降低大文件流式下载效率。

### 不采用：普通 HTTP 优先，CloakBrowser 兜底

该方案对控制面请求会继续维持两套 Cookie、TLS 指纹和风控会话。普通请求失败后再进入浏览器还会重复访问同一接口，既增加延迟，也可能增加验证或下载授权的无效消耗。

## 架构边界

### 1. BrowserTransport

`app/services/makerworld_browser_client.py` 继续作为唯一的 MakerWorld 控制面传输入口。它负责：

- 根据 URL 和显式平台选择 `cn` 或 `global` profile。
- 读取关联 profile 与代理配置。
- 过滤旧 Cookie、Token、User-Agent 等会污染已关联 profile 的请求头。
- 调用 CloakBrowser bridge，并返回结构化的 URL、状态码、Content-Type、正文和 profile ID。
- 对可恢复的 CDP 断开、超时和 `5xx` 执行现有的一次重试。
- 将底层异常转换为脱敏、稳定的浏览器错误类型。

业务模块不得直接调用 `browser_fetch()`，也不得使用 `requests` 获取 MakerWorld 页面、列表 API、设计 API、评论 API、账号状态或链接存在性。`process_jobs._source_looks_deleted()` 这类残留直连检查也必须迁入该客户端。

现有 `makerworld_browser_get()`、`makerworld_browser_get_text()` 和 `makerworld_browser_get_json()` 公共函数保持兼容。首阶段不增加常驻 Node 服务，不改变 profile 锁容量，也不把浏览器生命周期所有权移入 Pipeline。

`browser_authorize_3mf_download()` 是有副作用的真实点击命令，不属于 HTML / JSON GET 客户端。它继续通过现有 CloakBrowser session 与 Puppeteer 授权器执行，但必须由 Pipeline 编排，并与普通控制请求共享同一 profile 资源槽。

### 2. Python Parser

从 `legacy_archiver.py` 和 `batch_discovery.py` 提取纯解析模块，建议按稳定业务对象划分到 `app/services/makerworld_parsers/`：

- `model.py`：`__NEXT_DATA__`、设计详情、实例和打印配置归一化。
- `listing.py`：作者上传、收藏夹、合集及分页结果归一化。
- `comments.py`：评论、回复、附件元数据归一化。
- `common.py`：模型 URL、ID、资源 URL 和安全文本等共享规范化函数。

解析函数只接收 `str`、`dict`、`list` 等内存数据，只返回普通 Python 数据或明确的解析错误。它们不得读取账号配置、创建 `requests.Session`、调用 CloakBrowser、写数据库或写文件。

`BeautifulSoup` 继续用于容错 HTML 解析。将它改写为浏览器 DOM 查询既不会改善登录态，也会把可并行的本地 CPU 工作放进串行 profile，故不在本次范围内。

### 3. AssetDownloader

从 `legacy_archiver.py` 提取窄职责的 `app/services/asset_downloader.py`，统一处理：

- 图片、头像、评论附件和模型附件。
- 浏览器/API 已经取得的签名 `3MF` 直链。
- 临时文件、原子替换、流式分块、状态码检查、内容校验和失败清理。
- 现有代理策略、超时、资源槽和并发限制。

AssetDownloader 只消费已经解析或授权得到的资源 URL，不负责从 MakerWorld 控制面发现 URL。除现有下载确有要求外，不携带账号 Cookie 或控制面 Token；日志不得记录签名查询参数。

它不得将响应完整读入内存，也不得调用 BrowserTransport 作为下载兜底。静态下载失败按现有资源错误处理，不得因此把账号标记为失效或需要验证。

### 4. MakerWorld Pipeline

新增窄编排层 `app/services/makerworld_pipeline/`，按用例而不是按传输技术暴露能力：

- `discover_source(...)`：识别作者、收藏夹、合集等来源，分页获取控制面内容，调用 listing parser，返回当前批量发现结果形状。
- `archive_model(...)`：获取模型详情、解析实例与资源、执行可选资源下载和 `3MF` 授权，返回当前归档结果形状。
- `refresh_model(...)`：复用 `archive_model(...)` 的受限选项，不复制一套抓取和解析实现。

Pipeline 依赖 BrowserTransport、Parser、AssetDownloader 和现有 `three_mf` 服务，但不拥有队列、订阅计划、账号 Gate、数据库或前端状态。调用方仍决定任务何时入队、何时暂停以及如何展示结果。

首阶段保留现有函数签名和返回字典，不引入新的持久化领域模型。只有当提取后的多个模块确实共享稳定字段时，才在后续计划中增加小型 dataclass；不为本次精简预建抽象。

### 5. `3MF` 授权边界

`3MF` 保持两段式流程：

1. 由 CloakBrowser / Puppeteer 在对应 profile 中执行真实页面交互并取得授权或签名下载地址。
2. 由 AssetDownloader 使用现有资源下载通道流式保存文件并执行内容检查。

授权动作不得在 BrowserTransport 内部自动重复，避免浪费每日下载次数。每日上限、验证、Cookie 失效和网络错误继续使用现有分类与 Gate 状态，不因模块拆分改变。

## 统一入口数据流

### 批量发现与订阅

```text
ArchiveTaskManager / SubscriptionManager
  -> run_discover_batch_urls_job()
  -> makerworld_pipeline.discover_source()
  -> BrowserTransport 获取来源 HTML / JSON
  -> listing parser 归一化并去重
  -> 返回现有 items / source_name / expected_total 等结果
  -> 现有调用方负责入队和状态更新
```

订阅不再通过单独解析路径抓取来源；它继续调用 `run_discover_batch_urls_job()`。发现数量与源端总数不闭环时，仍保留当前告警和停止误标删除的规则。

### 单模型归档与源端刷新

```text
ArchiveTaskManager / source_refresh_jobs
  -> run_archive_model_job()
  -> makerworld_pipeline.archive_model()
  -> BrowserTransport 获取详情、评论和授权控制面
  -> model / comments parser
  -> AssetDownloader 保存选定资源
  -> 刷新现有模型索引和快照
```

源端刷新继续通过 `run_archive_model_job()` 的选项限制下载范围，不另建 refresh crawler。

## 兼容与模块迁移

`process_jobs.py` 是跨进程执行边界，`crawler.py` 是 `ArchiveTaskManager` facade，二者都不是新的抓取引擎。迁移期间保持以下公共入口稳定：

- `run_archive_model_job()`
- `run_discover_batch_urls_job()`
- `discover_batch_model_urls()`
- `archive_model()`
- `fetch_html_with_browser()`、`fetch_design_from_api()` 和 `extract_next_data()` 等已有公共 import

调用方逐个迁移到 Pipeline 后，`legacy_archiver.py` 与 `batch_discovery.py` 暂时通过 re-export 或薄包装兼容旧 import。只有仓库内调用者和测试全部迁移完成后，才在后续独立提交删除兼容 facade；不得在提取纯函数的同一提交中改变外部行为。

最终职责如下：

- `archive_worker.py`：队列、任务调度、Gate 与状态。
- `process_jobs.py`：子进程隔离、进度和结果传输。
- `makerworld_pipeline/`：发现与归档编排。
- `makerworld_browser_client.py`：控制面网络传输。
- `makerworld_parsers/`：无副作用解析。
- `asset_downloader.py`：静态资源流式下载。
- `three_mf.py` / `three_mf_quota.py`：授权结果、限额与失败分类。

## Scrapling 配置退场

`scrapling_fetch.py` 当前既未导入 Scrapling 包，也没有生产调用者。继续保留“Scrapling 优先”“仅 Scrapling”会让用户误以为系统存在可切换的反爬引擎。

退场分两个兼容阶段：

1. 后端继续接受旧 `scraping_engine` 值但忽略它，运行时固定使用 BrowserTransport；前端删除该设置和请求字段，旧配置加载不得失败。
2. 至少经过一个可升级版本后，从持久化 schema 中删除字段，并在配置规范化时丢弃旧值。

随后删除：

- `app/services/scrapling_fetch.py`
- `tests/test_scrapling_fetch.py`
- `legacy_archiver.py` 中无调用价值的 `_scrapling_trace()`
- `business_logs.py` 中只服务于新 `scrapling/fetch_trace` 的降噪白名单
- 模块文档、测试命令和设置测试中的 Scrapling 描述

历史数据库日志保持可读，不做迁移或删除。若 `_scrapling_trace()` 当前仍承载有用的抓取诊断字段，则先迁移到现有 `archive` 或 `makerworld` 日志分类，再删除旧名称。

## 错误与状态处理

模块重构不得新增一套错误状态。异常在边界处按现有语义归类：

- CloakBrowser `5xx`、CDP 超时、断开和 Manager 不可用：瞬时网络错误，可按现有策略重试，不更新账号登录或验证状态。
- 浏览器真实返回登录页、明确的 `401/403`、Cloudflare challenge 或 MakerWorld 验证载荷：更新对应平台现有账号/Gate 状态。
- `404`、下架、私有或草稿：模型或来源业务终态，不污染账号状态。
- 解析失败：包含阶段、响应类型和脱敏 URL 的解析错误，不回退到普通 HTTP 再请求一次。
- 静态资源失败：记录具体资源类别和脱敏错误，其他可归档数据继续按现有降级规则处理。
- `3MF` 每日上限：停止对应平台的自动授权重试，不通过重复探针验证上限。

日志允许记录平台、阶段、耗时、状态码、Content-Type、解析项数和资源字节数；不得记录 Cookie、Token、完整签名 URL、响应正文或验证码敏感载荷。

## 性能与资源约束

- 同一 profile 的控制面操作继续通过全局资源槽串行，不为解析或静态下载占用 profile 锁。
- 一次控制面请求只创建并清理自己的临时页面，不导航或关闭用户页面。
- Pipeline 不为同一 HTML 同时执行多个网络 fallback；一次取得后在内存中完成全部必要解析。
- JSON 直接使用 `json.loads()`，只有 HTML 才进入 `BeautifulSoup`。
- 静态资源保持流式写入、现有并发和资源槽，不把大响应放入进程队列或 bridge 标准输出。
- 子进程结果继续使用现有结果文件机制；不通过 multiprocessing queue 传输大型正文或二进制。
- 解析器必须可在不启动 CloakBrowser、数据库和 Worker 的情况下单元测试。
- 性能基线至少记录单模型控制请求数、批量每页控制请求数、浏览器总耗时、解析耗时、下载字节数和峰值进程内存。重构后控制请求数不得增加。

## 分阶段实施

### 阶段 0：建立行为基线

- 为现有模型、作者、收藏夹、合集、评论、附件和 `3MF` 结果补充 fixture/characterization tests。
- 记录典型单模型和批量发现的请求次数、结果结构与错误分类。
- 增加架构边界测试，证明 MakerWorld 控制面不会通过 `requests` 发出。

### 阶段 1：提取纯解析器

- 先复制测试用例，再逐组移动纯函数到 `makerworld_parsers/`。
- 旧模块通过 import/re-export 保持调用路径。
- 此阶段不改变网络请求顺序、fallback、日志、字段或错误文案。

### 阶段 2：提取 AssetDownloader

- 移动流式下载、临时文件和校验代码。
- 覆盖成功、重定向、超时、截断、错误 Content-Type、原子替换和清理测试。
- 验证下载不持有 profile 锁，且不会把静态失败映射为账号失败。

### 阶段 3：引入 Pipeline 编排

- 让 `run_archive_model_job()` 和 `run_discover_batch_urls_job()` 调用 Pipeline。
- 让订阅、手动归档和源端刷新继续只经这两个稳定入口复用新实现。
- 迁移 `_source_looks_deleted()` 等残留控制面直连到 BrowserTransport。

### 阶段 4：缩小旧模块

- 将已覆盖的模型归档与批量发现编排移出巨型文件。
- 每次只移动一个职责，保留兼容 facade 并运行对应回归测试。
- 通过 import 图检查避免 Pipeline 反向依赖 `archive_worker.py` 或订阅模块。

### 阶段 5：删除 Scrapling 残留

- 先发布“接受但忽略旧字段”的兼容版本。
- 再删除前端设置、模块、测试、日志白名单和文档残留。
- 验证旧配置和旧前端请求仍能被升级版本安全读取。

每个阶段形成独立、可测试、可回退的提交。用户可见阶段发布时按项目规则更新版本号、README 和 CHANGELOG；本设计文档本身不触发版本升级。

## 测试策略

### 单元测试

- 每个公开 parser 函数覆盖正常、缺字段、空值、非预期类型和变体 HTML。
- BrowserTransport 覆盖平台/profile 选择、旧认证头过滤、查询参数、JSON/HTML、超时、断开、`5xx`、`401/403/404` 和脱敏错误。
- AssetDownloader 覆盖流式分块、重定向、代理、超时、截断文件、校验失败、原子替换和失败清理。
- Pipeline 使用 mock transport/downloader 验证请求次序、解析结果、降级和错误传播。

### 集成回归

- 单模型归档、API fallback 与离线页面生成。
- 作者上传、收藏夹、合集发现、分页、去重和 expected total 闭环。
- 订阅同步和批量任务入队不重复。
- 评论、回复、图片和附件采集。
- `3MF` 点击授权、签名直链下载、文件校验、每日限额和验证 Gate。
- 源端刷新与来源删除判断。
- CN / Global profile 与代理隔离。

### 资源与发布验证

- 对典型 fixture 和受控线上样本比较重构前后控制请求数与耗时。
- 检查任务结束后没有新增残留标签页、孤儿进程或持续增长内存。
- 运行完整后端测试、前端测试与构建、Compose 校验、Docker smoke test 和 `git diff --check`。
- 发布阶段验证 App / Worker readiness、GitHub Release 与版本镜像。

## 验收标准

1. 生产路径只有一条 MakerWorld 发现/归档 Pipeline，不再存在按入口复制的网络与解析编排。
2. 所有 MakerWorld HTML / JSON 控制面请求都可追溯到 `makerworld_browser_client.py`，业务模块不存在普通 HTTP 控制面兜底。
3. 图片、附件和签名 `3MF` 仍通过 `requests` 流式下载，bridge 不传输大型二进制。
4. `BeautifulSoup` 只存在于无网络副作用的 parser 模块。
5. `scrapling_fetch.py` 和用户可见抓取引擎设置完成兼容退场，旧配置升级不失败。
6. `legacy_archiver.py` 与 `batch_discovery.py` 的业务职责显著减少，既有公共入口在迁移窗口内保持兼容。
7. 订阅、手动归档、批量发现和源端刷新产出与错误语义保持不变。
8. 账号 Gate、每日限额和验证状态只由真实业务结果更新，网络或静态资源错误不污染账号状态。
9. 重构后典型流程的 CloakBrowser 控制请求数不增加，静态下载吞吐不降低，任务结束后资源可回收。
10. 阶段测试、完整回归、构建和发布检查全部通过。
