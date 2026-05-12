# MakerHub iOS 快捷指令

这个快捷指令用于从微信、文件 App、聊天附件的共享菜单里把模型文件推送到 MakerHub。MakerHub 端会先接收文件，再复用网页端的本地导入整理流程。

## MakerHub 设置

1. 打开 `设置 -> 本地整理 -> 移动端导入`。
2. 填入局域网地址，例如 `http://192.168.1.20:1111`。
3. 如果需要在外网使用，填入公网地址，例如 `https://makerhub.example.com`；不填公网地址时快捷指令会走局域网。
4. 点击 `生成 Token`，完整 Token 只显示一次。

## 快捷指令变量

新建 iOS 快捷指令，名称建议为 `推送到 MakerHub`，开启 `在共享表单中显示`，接收类型选择 `文件`。

在快捷指令开头放 3 个 `文本` 动作，并分别改名为：

| 变量 | 内容 |
| --- | --- |
| `MakerHubToken` | MakerHub 设置页生成的 `mhi_...` Token |
| `LanBaseUrl` | 局域网地址，不要以 `/` 结尾 |
| `PublicBaseUrl` | 公网地址，不要以 `/` 结尾；不需要公网时留空 |

## 动作流程

1. 判断 `PublicBaseUrl` 是否为空。
   - 有填写公网地址时，把 `UploadBaseUrl` 设置为 `PublicBaseUrl`
   - 没有填写公网地址时，把 `UploadBaseUrl` 设置为 `LanBaseUrl`

2. `获取 URL 内容`
   - URL: `UploadBaseUrl` + `/api/mobile-import/ping-ipv4?token=` + `MakerHubToken`
   - 方法: `GET`
   - 返回内容需要包含 `makerhub:ok`

3. 如果探测结果不包含 `makerhub:ok`：
   - `显示提醒`: `网络不通`
   - `停止此快捷指令`

4. `获取名称`
   - 输入: `快捷指令输入`
   - 保存为变量 `FileName`

5. `获取 URL 内容`
   - URL: `UploadBaseUrl` + `/api/mobile-import/raw-ipv4?token=` + `MakerHubToken`
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
- `POST /api/mobile-import/raw-ipv4?token=...`: 给 iOS 快捷指令用的简化单文件上传接口。
- `POST /api/mobile-import`: 仍保留网页/脚本使用的 multipart 批量上传入口。

快捷指令不会在局域网和公网之间自动重试：填了公网地址就走公网，没填公网地址就走局域网。选定地址不可用时提示 `网络不通`；文件已推送给 MakerHub 后提示 `已上传`，后续整理进度在 MakerHub 的本地整理进度卡片里查看。
