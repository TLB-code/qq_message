# QQ 群消息总结工具启动流程

这份文档用于记录从零启动本地项目的流程，包括 Python 后端、Vue 前端、NapCat 和 `.env` 配置说明。

## 1. 进入项目目录

打开 PowerShell：

```powershell
cd D:\tim\Desktop\qq_message
```

## 2. 检查 `.env` 配置

确认项目根目录存在：

```text
D:\tim\Desktop\qq_message\.env
```

可以参考下面的配置：

```env
DEEPSEEK_API_KEY=sk-your-real-key
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com

QQ_SUMMARY_HOST=127.0.0.1
QQ_SUMMARY_PORT=8000
QQ_SUMMARY_DB=data/qq_summary.sqlite3

QQ_SUMMARY_WEBHOOK_DEBUG=false
QQ_SUMMARY_WEBHOOK_TOKEN=use-a-long-random-token
QQ_SUMMARY_WEB_PASSWORD=use-a-strong-page-password

QQ_SUMMARY_AUTO_SUMMARY_ENABLED=true
QQ_SUMMARY_AUTO_SUMMARY_THRESHOLD=500
QQ_SUMMARY_SPECIAL_MEMBER_USER_ID=重点成员的QQ号
QQ_SUMMARY_SPECIAL_MEMBER_DISPLAY_NAME=魔女公主♪
```

不要把真实的 `DEEPSEEK_API_KEY`、`QQ_SUMMARY_WEBHOOK_TOKEN`、`QQ_SUMMARY_WEB_PASSWORD` 提交到 GitHub。

### `.env` 字段说明

| 字段 | 是否必填 | 示例 | 说明 |
| --- | --- | --- | --- |
| `DEEPSEEK_API_KEY` | 是 | `sk-xxxx` | DeepSeek API Key，用来调用 AI 总结接口。没有这个字段时，后端可以启动，但点击总结会失败。 |
| `DEEPSEEK_MODEL` | 否 | `deepseek-v4-flash` | DeepSeek 模型名称。不填时默认使用代码里的默认值。 |
| `DEEPSEEK_BASE_URL` | 否 | `https://api.deepseek.com` | DeepSeek API 地址。正常使用官方 DeepSeek 时保持这个值即可。 |
| `QQ_SUMMARY_HOST` | 否 | `127.0.0.1` | 后端监听地址。本地只给自己用时用 `127.0.0.1`；部署到服务器并需要外部访问时通常改成 `0.0.0.0`。 |
| `QQ_SUMMARY_PORT` | 否 | `8000` | 后端监听端口。打开网页时访问的端口就是这个。 |
| `QQ_SUMMARY_DB` | 否 | `data/qq_summary.sqlite3` | SQLite 数据库路径，用来保存群、消息、总结历史、自动总结开关等数据。不填时默认保存到 `data/qq_summary.sqlite3`。 |
| `QQ_SUMMARY_WEBHOOK_DEBUG` | 否 | `false` | 是否记录 NapCat 推送过来的原始事件。调试时可以改成 `true`，会写入 `data/webhook_events.log`。日志里可能包含消息内容，平时建议保持 `false`。 |
| `QQ_SUMMARY_WEBHOOK_TOKEN` | 建议填写 | `一串随机长密码` | NapCat 调用后端 webhook 时使用的校验 token。设置后，NapCat webhook URL 必须带上同样的 `token`，否则后端会拒绝消息。 |
| `QQ_SUMMARY_WEB_PASSWORD` | 建议填写 | `一个网页登录密码` | 网页访问密码。设置后，打开页面需要先登录；不设置则网页接口不需要登录。部署到服务器时强烈建议设置。 |
| `QQ_SUMMARY_AUTO_SUMMARY_ENABLED` | 否 | `true` | 是否全局启用后台自动总结功能。开启后，还需要在网页里给具体群聊打开“自动总结”，那个群才会自动总结。 |
| `QQ_SUMMARY_AUTO_SUMMARY_THRESHOLD` | 否 | `500` | 自动总结阈值。某个已开启自动总结的群，未读历史消息达到这个数量后，后台会自动总结最早的一批消息。 |
| `QQ_SUMMARY_SPECIAL_MEMBER_USER_ID` | 建议填写 | `重点成员的QQ号` | 专属总结使用的稳定身份。系统按 OneBot `user_id` 判断本人、@ 和回复关系，不按可能变化的昵称判断。 |
| `QQ_SUMMARY_SPECIAL_MEMBER_DISPLAY_NAME` | 否 | `魔女公主♪` | 专属总结标题中显示的名称，只用于展示，不参与身份判断。 |

## 3. 构建前端

前端现在是 Vite + Vue 项目。第一次启动，或前端代码有修改时，执行：

```powershell
cd D:\tim\Desktop\qq_message\frontend
npm.cmd install
npm.cmd run build
```

如果只是重启后端，并且前端代码没有改，可以跳过 `npm.cmd install`，通常也可以跳过 `npm.cmd run build`。

## 4. 启动后端服务

回到项目根目录：

```powershell
cd D:\tim\Desktop\qq_message
python -m app.server
```

保持这个 PowerShell 窗口不要关闭。

打开网页：

```text
http://127.0.0.1:8000
```

如果设置了 `QQ_SUMMARY_WEB_PASSWORD`，页面会先进入登录界面，输入 `.env` 里的网页密码即可。

检查服务是否正常：

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/health"
```

## 5. 启动 NapCat

再打开一个 PowerShell 窗口：

```powershell
cd D:\tim\Desktop\qq_message\tools\napcat\onekey\NapCat.44498.Shell
.\napcat.bat
```

保持这个窗口不要关闭。

NapCat WebUI 地址：

```text
http://127.0.0.1:6099/webui/?token=63d4a7435850
```

如果 NapCat 显示二维码，使用 QQ 扫码登录。

## 6. 配置 NapCat Webhook

NapCat 里需要有一个 HTTP Client，例如：

```text
qq-message-summary
```

Webhook 地址填写：

```text
http://127.0.0.1:8000/webhook/onebot?token=use-a-long-random-token
```

其中 `token=use-a-long-random-token` 要和 `.env` 里的 `QQ_SUMMARY_WEBHOOK_TOKEN` 完全一致。

推荐设置：

```text
enable: true
messagePostFormat: array
reportSelfMessage: false
token: 留空
```

这里 NapCat 自己的 `token` 字段可以留空，本项目使用 URL 里的 `token` 做校验。

## 7. 使用方式

这个工具只会记录 NapCat 连接之后收到的新群消息，不会自动导入 QQ 客户端里已经存在的旧未读消息。

基本流程：

1. 后端服务保持运行。
2. NapCat 保持登录。
3. QQ 群里有新消息后，网页会自动刷新。
4. 选择一个群。
5. 点击“总结未读”进行总结。

如果这个群开启了自动总结，并且未读消息达到 `QQ_SUMMARY_AUTO_SUMMARY_THRESHOLD`，后台会自动总结，不需要网页一直打开。

## 8. 常用检查命令

查看当前保存的群列表：

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/groups" | ConvertTo-Json -Depth 10
```

检查 DeepSeek Key 是否已经加载，不会打印真实 Key：

```powershell
python -c "from app.config import load_settings; s=load_settings(); print(bool(s.deepseek_api_key))"
```

检查 NapCat WebUI 端口：

```powershell
Get-NetTCPConnection -LocalPort 6099 -ErrorAction SilentlyContinue
```

检查后端端口：

```powershell
Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue
```

## 9. 只重启后端

查找正在运行的后端进程：

```powershell
Get-CimInstance Win32_Process -Filter "name = 'python.exe'" |
  Where-Object { $_.CommandLine -like '*-m app.server*' } |
  Select-Object ProcessId,CommandLine
```

停止对应进程：

```powershell
Stop-Process -Id <PID> -Force
```

重新启动：

```powershell
cd D:\tim\Desktop\qq_message
npm.cmd --prefix frontend run build
python -m app.server
```

## 10. 只重启 NapCat

关闭 NapCat 的 PowerShell 窗口，或者停止这个目录下启动的相关进程：

```text
D:\tim\Desktop\qq_message\tools\napcat\onekey\NapCat.44498.Shell
```

然后重新启动：

```powershell
cd D:\tim\Desktop\qq_message\tools\napcat\onekey\NapCat.44498.Shell
.\napcat.bat
```

## 11. 常见问题

### 页面没有新群或新消息

先确认 NapCat 已经登录，并且 QQ 群里有 NapCat 启动之后的新消息。

然后检查：

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/groups" | ConvertTo-Json -Depth 10
```

### 提示 DeepSeek API Key 缺失

确认 `.env` 文件在：

```text
D:\tim\Desktop\qq_message\.env
```

然后重启后端服务。

### 总结失败

先检查模型和 API 地址：

```env
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

然后重新点击“总结未读”。

### 需要查看原始 webhook 数据

把 `.env` 改成：

```env
QQ_SUMMARY_WEBHOOK_DEBUG=true
```

重启后端后，原始事件会写入：

```text
data\webhook_events.log
```

调试结束后建议改回：

```env
QQ_SUMMARY_WEBHOOK_DEBUG=false
```

因为这个日志可能包含真实聊天内容。
