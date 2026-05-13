# MakerHub iOS 快捷指令

这个快捷指令用于从微信、文件 App、聊天附件的共享菜单里把模型文件推送到 MakerHub。MakerHub 端会先接收文件，再复用网页端的本地导入整理流程。

## MakerHub 设置

1. 打开 `设置 -> 本地整理 -> 移动端导入`。
2. 点击 `生成 Token`，完整 Token 只显示一次。
3. 把 Token 和 MakerHub 地址填入手机快捷指令。MakerHub 地址可以是局域网地址，也可以是公网地址，不要以 `/` 结尾。

## 快捷指令变量

新建 iOS 快捷指令，名称建议为 `推送到 MakerHub`，开启 `在共享表单中显示`，接收类型选择 `文件`。

在快捷指令开头放 2 个 `文本` 动作，并分别改名为：

| 变量 | 内容 |
| --- | --- |
| `MakerHubToken` | MakerHub 设置页生成的 `mhi_...` Token |
| `MakerHubBaseUrl` | MakerHub 地址，不要以 `/` 结尾；例如 `http://192.168.1.20:1111` 或公网地址 |

## 动作流程

1. 从共享表单接收文件。直接运行快捷指令而不从微信或文件 App 分享文件时，上传体会为空。

2. `获取 URL 内容`
   - URL: `MakerHubBaseUrl` + `/api/mobile-import/ping-ipv4?token=` + `MakerHubToken`
   - 方法: `GET`
   - 返回内容需要包含 `makerhub:ok`

3. 地址不可用或 Token 错误时，iOS 会显示请求失败或 MakerHub 会拒绝上传。

4. `获取名称`
   - 输入: `快捷指令输入`
   - 保存为变量 `FileName`

5. `获取 URL 内容`
   - URL: `MakerHubBaseUrl` + `/api/mobile-import/raw-ipv4?token=` + `MakerHubToken` + `&filename=` + `FileName`
   - 方法: `POST`
   - 请求头:
     - `X-MakerHub-Filename` = `FileName`
   - 请求体: `文件`
   - 文件: `快捷指令输入`

6. 上传请求返回后：
   - `显示通知`: `已上传`

## 接口说明

- `GET /api/mobile-import/ping`: 用 Token 验证当前地址是否可用。
- `GET /api/mobile-import/ping-ipv4?token=...`: 给 iOS 快捷指令用的简化探测接口。
- `POST /api/mobile-import/raw?background=1`: 接收单个文件，并在后台进入本地导入整理流程。
- `POST /api/mobile-import/raw-ipv4?token=...&filename=...`: 给 iOS 快捷指令用的简化单文件上传接口；文件名会同时通过 URL 参数和 `X-MakerHub-Filename` 请求头传递，兼容微信中文附件名。
- `POST /api/mobile-import`: 仍保留网页/脚本使用的 multipart 批量上传入口。

文件已推送给 MakerHub 后提示 `已上传`，后续整理进度在 MakerHub 的本地整理进度卡片里查看。
