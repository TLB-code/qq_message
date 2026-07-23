# QQ 群消息总结工具服务器部署方案

这份文档是一份从零开始的服务器部署流程。目标是让项目在服务器后台长期运行：即使网页没有打开，只要后端服务和 NapCat 正常在线，选中的群消息达到阈值后就能自动总结。

## 0. 部署目标

部署完成后，整体结构是：

```text
QQ 群消息
  ↓
NapCat / OneBot
  ↓ HTTP Webhook
Python 后端服务
  ↓
SQLite 数据库保存消息和总结
  ↓
DeepSeek API
  ↓
网页查看未读消息、历史消息、总结历史
```

服务器上建议运行：

```text
Nginx：对外提供网页访问，后续可配置 HTTPS
Python 后端：运行 app.server
Vue 前端：构建到 frontend/dist，由后端提供静态页面
SQLite：保存消息、总结历史、群配置
NapCat：登录 QQ，接收群消息并推送到后端 webhook
systemd：让后端服务开机自启、崩溃自动重启
```

## 1. 准备服务器

推荐配置：

```text
系统：Ubuntu 22.04 / Ubuntu 24.04
CPU：1 核起步，推荐 2 核
内存：2GB 起步，推荐 4GB
硬盘：20GB 起步，推荐 40GB+
网络：能访问 GitHub、DeepSeek API
```

本文档假设：

```text
项目目录：/opt/qq_message
后端端口：8000
域名：message.tanlb.xyz
Linux 用户：当前登录用户
```

下面的命令会直接使用你的域名 `message.tanlb.xyz`。

## 1.1 从旧服务器迁移到新服务器

如果这是第一次部署，可以跳到“1. 准备服务器”。如果旧服务器即将到期，并且需要保留现有消息、总结历史、已读游标、群自动总结开关和后台任务断点，必须先完成本节。

迁移时需要保留的核心数据：

```text
/opt/qq_message/data/qq_summary.sqlite3
/opt/qq_message/.env
NapCat 登录状态和 HTTP Client 配置，或准备在新服务器重新登录并配置
```

`qq_summary.sqlite3` 已经包含消息、总结、群配置、手动总结任务、消息快照和分块检查点。不要只克隆 GitHub 仓库，仓库里不包含数据库和 `.env`。

### 迁移前准备

1. 确认本地最新代码已经提交并推送到 GitHub。
2. 在 DNS 服务商处把 `message.tanlb.xyz` 的 TTL 提前降低到 `300` 秒左右。
3. 记录旧服务器和新服务器 IP。
4. 在新服务器完成本文第 1-8 节，但先不要停止旧服务器，也不要启动新服务器上的 NapCat。
5. 暂时不要在新服务器执行 Certbot；域名仍指向旧服务器时，HTTP 验证通常无法通过。

在旧服务器确认当前版本和任务状态：

```bash
cd /opt/qq_message
git rev-parse HEAD
sudo systemctl status qq-message --no-pager

sqlite3 data/qq_summary.sqlite3 \
  "SELECT task_id, group_id, status, stage, completed_chunks, total_chunks FROM summary_tasks WHERE status IN ('queued', 'running');"
```

如果有总结任务，优先等待任务完成。必须中途迁移时也可以继续：数据库里的消息快照和已完成分块会一起迁移，新服务器启动后会重新入队，只补做未完成分块。

### 最终数据切换

为了避免数据库复制期间仍有新消息写入，先停止旧服务器的 NapCat，再停止后端。NapCat 如果由 systemd 管理：

```bash
sudo systemctl stop napcat
sudo systemctl stop qq-message
```

如果 NapCat 仍在 `screen` 中运行：

```bash
screen -ls
screen -r napcat
```

进入会话后正常停止 NapCat，再退出 `screen`。从此时到新服务器 NapCat 上线之间的消息可能无法由 webhook 实时接收，因此应尽量缩短这段时间。

旧服务器停止写入后，创建最终迁移文件：

```bash
cd /opt/qq_message
mkdir -p migration

cp -a data/qq_summary.sqlite3 migration/qq_summary.sqlite3
cp -a .env migration/qq-message.env
chmod 600 migration/qq-message.env

sha256sum migration/qq_summary.sqlite3 migration/qq-message.env
ls -lh migration/
```

在新服务器拉取迁移文件，将 `旧服务器IP` 替换为实际地址：

```bash
sudo systemctl stop qq-message 2>/dev/null || true
mkdir -p /opt/qq_message/data

scp root@旧服务器IP:/opt/qq_message/migration/qq_summary.sqlite3 /opt/qq_message/data/qq_summary.sqlite3
scp root@旧服务器IP:/opt/qq_message/migration/qq-message.env /opt/qq_message/.env

chmod 600 /opt/qq_message/.env
chown -R $(whoami):$(whoami) /opt/qq_message/data /opt/qq_message/.env
sha256sum /opt/qq_message/data/qq_summary.sqlite3 /opt/qq_message/.env
sqlite3 /opt/qq_message/data/qq_summary.sqlite3 "PRAGMA quick_check;"
```

对比新旧服务器的 SHA256，`PRAGMA quick_check` 应输出 `ok`。数据库文件必须完全一致；`.env` 复制后不要把内容发到聊天或日志中。

启动新服务器后端并观察数据库迁移、任务恢复日志：

```bash
sudo systemctl start qq-message
sudo journalctl -u qq-message -f
```

看到下面类似日志后再继续：

```text
QQ Message Summary running at http://127.0.0.1:8000
Resumed N manual summary task(s)
```

### 迁移 NapCat

推荐在新服务器重新安装 NapCat 并重新扫码登录，不要直接复制旧服务器的 QQ 程序目录。新服务器登录成功后，重新创建 HTTP Client：

```text
http://127.0.0.1:8000/webhook/onebot?token=与迁移后的.env完全一致的QQ_SUMMARY_WEBHOOK_TOKEN
```

同一个 QQ 不建议在两台服务器的 NapCat 中同时运行。确认旧 NapCat 已停止后，再登录新 NapCat。

### 切换域名和 HTTPS

先在本地电脑临时验证新服务器的 HTTP Nginx，其中 `新服务器IP` 替换为实际地址：

```bash
curl --resolve message.tanlb.xyz:80:新服务器IP http://message.tanlb.xyz/api/auth/status
```

确认返回认证状态后，在 DNS 服务商处把 `message.tanlb.xyz` 的 A 记录改为新服务器 IP。检查解析：

```bash
dig +short message.tanlb.xyz
```

解析到新服务器后，再在新服务器申请证书：

```bash
sudo certbot --nginx -d message.tanlb.xyz
sudo certbot renew --dry-run
```

如果域名使用 Cloudflare 橙云代理，`dig` 可能显示 Cloudflare IP，而不是源服务器 IP。迁移时可以暂时把记录改为“仅 DNS / 灰云”，确认新服务器和证书正常后再重新开启代理；或者直接在 Cloudflare 控制台确认源站 A 记录已经改为新服务器。

最终检查：

```bash
curl -I https://message.tanlb.xyz
sudo systemctl status qq-message nginx --no-pager
sudo journalctl -u qq-message -n 100 --no-pager
```

浏览器使用 `Ctrl+F5` 强制刷新，确认历史消息、总结数量和群自动总结开关都存在。旧服务器至少保留到新服务器稳定运行并完成一次消息接收和总结测试后再释放。

### 迁移回退

如果新服务器出现问题：

1. 停止新服务器 NapCat 和 `qq-message`。
2. 把 DNS A 记录切回旧服务器 IP。
3. 启动旧服务器 `qq-message` 和 NapCat。
4. 不要让新旧 NapCat 同时登录。

新服务器切换后收到的消息不会自动合并回旧数据库，因此回退决定应尽早做，并保留新服务器数据库用于后续人工合并或再次迁移。

## 2. 安装系统依赖

登录服务器后执行：

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip nginx curl ca-certificates gnupg sqlite3 rsync dnsutils

curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
```

检查版本：

```bash
python3 --version
node --version
npm --version
nginx -v
```

项目使用 Vite 6，Node.js 必须是 18、20 或 22 以上版本，推荐 Node.js 20 LTS。不要直接使用 Ubuntu 22.04 仓库中的旧版 Node.js。可以额外验证：

```bash
node -e "const major=Number(process.versions.node.split('.')[0]); if(major<18){process.exit(1)}; console.log('Node.js version OK')"
```

建议 Python 使用 3.11 或更高版本。Ubuntu 22.04 默认 Python 3.10 也可以运行当前项目。

## 3. 拉取项目代码

进入 `/opt`：

```bash
cd /opt
```

拉取 GitHub 公开仓库：

```bash
sudo git clone https://github.com/TLB-code/qq_message.git
sudo chown -R $USER:$USER /opt/qq_message
cd /opt/qq_message
```

现在仓库已经公开，正常情况下服务器不需要登录 GitHub，也不需要配置 Personal Access Token、SSH Key 或 Deploy Key。

如果服务器提示找不到仓库，优先检查仓库地址是否写对：

```text
https://github.com/TLB-code/qq_message.git
```

## 4. 创建 `.env`

在项目根目录创建配置文件：

```bash
cd /opt/qq_message
nano .env
```

写入下面内容：

```env
DEEPSEEK_API_KEY=你的DeepSeek_API_Key
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com

QQ_SUMMARY_HOST=127.0.0.1
QQ_SUMMARY_PORT=8000
QQ_SUMMARY_DB=data/qq_summary.sqlite3

QQ_SUMMARY_WEBHOOK_DEBUG=false
QQ_SUMMARY_WEBHOOK_TOKEN=换成一串很长的随机token
QQ_SUMMARY_WEB_PASSWORD=换成一个网页登录密码

QQ_SUMMARY_AUTO_SUMMARY_ENABLED=true
QQ_SUMMARY_AUTO_SUMMARY_THRESHOLD=500
QQ_SUMMARY_SPECIAL_MEMBER_USER_ID=重点成员的QQ号
QQ_SUMMARY_SPECIAL_MEMBER_DISPLAY_NAME=魔女公主♪
```

保存后设置权限，避免其他用户直接读取密钥：

```bash
chmod 600 /opt/qq_message/.env
```

### `.env` 字段说明

| 字段 | 建议 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 必填 | DeepSeek API Key，用来调用 AI 总结。没有这个值，服务可以启动，但总结会失败。 |
| `DEEPSEEK_MODEL` | 建议填写 | DeepSeek 模型名。当前项目默认使用 `deepseek-v4-flash`。 |
| `DEEPSEEK_BASE_URL` | 建议填写 | DeepSeek API 地址，官方地址通常是 `https://api.deepseek.com`。 |
| `QQ_SUMMARY_HOST` | 服务器部署建议 `127.0.0.1` | 后端只监听本机，由 Nginx 对外代理，更安全。 |
| `QQ_SUMMARY_PORT` | 默认 `8000` | 后端监听端口。 |
| `QQ_SUMMARY_DB` | 默认 `data/qq_summary.sqlite3` | SQLite 数据库路径，消息和总结历史都保存在这里。 |
| `QQ_SUMMARY_WEBHOOK_DEBUG` | 平时 `false` | 是否保存 NapCat 推送的原始事件。调试时可以改成 `true`，但日志会包含聊天内容。 |
| `QQ_SUMMARY_WEBHOOK_TOKEN` | 强烈建议填写 | NapCat 调用 webhook 的校验 token，防止别人伪造消息推送。 |
| `QQ_SUMMARY_WEB_PASSWORD` | 强烈建议填写 | 网页登录密码。服务器部署时必须设置，否则网页接口会裸露在公网。 |
| `QQ_SUMMARY_AUTO_SUMMARY_ENABLED` | 建议 `true` | 全局自动总结开关。还需要在网页里给具体群开启自动总结。 |
| `QQ_SUMMARY_AUTO_SUMMARY_THRESHOLD` | 建议 `500` | 自动总结触发阈值。达到阈值后，后台固定每批最多总结 500 条；它不改变手动总结上限。 |
| `QQ_SUMMARY_SPECIAL_MEMBER_USER_ID` | 建议填写 | 专属总结使用的 QQ `user_id`。昵称或群名片修改后仍可识别，也不会把相似昵称或带“伪”的昵称算作本人。 |
| `QQ_SUMMARY_SPECIAL_MEMBER_DISPLAY_NAME` | 可选 | 专属总结标题中的显示名称，默认是 `魔女公主♪`，不参与身份判断。 |

## 5. 构建前端

进入前端目录：

```bash
cd /opt/qq_message/frontend
```

安装依赖并构建：

```bash
npm ci
npm run build
```

构建完成后，确认存在：

```bash
ls -la /opt/qq_message/frontend/dist
```

如果能看到 `index.html` 和 `assets` 目录，说明前端构建成功。

## 6. 准备后端运行环境

回到项目根目录：

```bash
cd /opt/qq_message
```

创建 Python 虚拟环境：

```bash
python3 -m venv .venv
```

激活虚拟环境：

```bash
source .venv/bin/activate
```

当前后端只使用 Python 标准库，所以项目里没有 `requirements.txt`，正常情况下不需要安装 Python 第三方依赖。

测试后端能否启动：

```bash
python -m app.server
```

看到类似输出就说明启动成功：

```text
QQ Message Summary running at http://127.0.0.1:8000
SQLite database: data/qq_summary.sqlite3
```

按 `Ctrl+C` 停止，下一步改成 systemd 后台运行。

## 7. 配置 systemd 后台服务

创建服务文件：

```bash
sudo nano /etc/systemd/system/qq-message.service
```

写入：

```ini
[Unit]
Description=QQ Message Summary Server
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/qq_message
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/qq_message/.venv/bin/python -m app.server
Restart=always
RestartSec=5
User=你的Linux用户名
Group=你的Linux用户名
UMask=0077

[Install]
WantedBy=multi-user.target
```

把 `你的Linux用户名` 替换成当前用户。可以用下面命令查看：

```bash
whoami
```

加载并启动服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable qq-message
sudo systemctl start qq-message
```

查看状态：

```bash
sudo systemctl status qq-message
```

查看日志：

```bash
journalctl -u qq-message -f
```

本机检查后端：

```bash
curl http://127.0.0.1:8000/api/auth/status
```

设置了 `QQ_SUMMARY_WEB_PASSWORD` 后，其他 `/api/*` 接口需要登录 Cookie，直接请求 `/api/health` 可能返回 `401 Login required`。`/api/auth/status` 返回 JSON 即可证明后端已经监听。

## 8. 配置 Nginx 反向代理

创建 Nginx 配置：

```bash
sudo nano /etc/nginx/sites-available/qq-message
```

写入：

```nginx
server {
    listen 80;
    server_name message.tanlb.xyz;

    client_max_body_size 20m;

    location = / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        add_header Cache-Control "no-cache, no-store, must-revalidate" always;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

根页面禁用缓存，避免更新后浏览器继续引用旧的带哈希前端文件；`/assets/` 文件名包含构建哈希，可以继续由浏览器正常缓存。

这里已经使用你的域名 `message.tanlb.xyz`。

启用配置：

```bash
sudo ln -s /etc/nginx/sites-available/qq-message /etc/nginx/sites-enabled/qq-message
```

如果默认站点占用了配置，可以移除默认站点：

```bash
sudo rm -f /etc/nginx/sites-enabled/default
```

检查配置：

```bash
sudo nginx -t
```

重载 Nginx：

```bash
sudo systemctl reload nginx
```

此时可以访问：

```text
http://message.tanlb.xyz
```

如果你还没有域名，也可以先用服务器 IP 访问：

```text
http://服务器IP
```

## 9. 配置 HTTPS

如果有域名，建议立刻配置 HTTPS。

安装 Certbot：

```bash
sudo apt install -y certbot python3-certbot-nginx
```

申请证书：

```bash
sudo certbot --nginx -d message.tanlb.xyz
```

按提示选择自动跳转 HTTPS。

检查自动续期：

```bash
sudo certbot renew --dry-run
```

配置完成后访问：

```text
https://message.tanlb.xyz
```

## 10. 配置 NapCat

自动总结能不能在网页关闭后继续工作，关键取决于两个后台程序：

```text
Python 后端必须一直运行
NapCat 必须一直在线并持续接收 QQ 群消息
```

### 服务器安装 NapCat

方案 A 是 NapCat 和后端在同一台服务器上，所以需要先在服务器里安装并登录 NapCat。

这里推荐使用 NapCat 官方 Linux 一键安装脚本的“本地 Shell 安装”，不要优先用 Docker。原因是 Docker 里配置 webhook 时，`127.0.0.1` 默认指向容器自己，容易和后端服务地址混淆；本地 Shell 安装更适合当前这个项目。

官方文档：

```text
https://napneko.github.io/guide/boot/Shell
https://github.com/NapNeko/NapCat-Installer
```

#### 1. 安装基础工具

在服务器执行：

```bash
sudo apt update
sudo apt install -y curl screen xvfb
```

如果你一直用 `root` 部署，可以继续用 `root` 安装 NapCat。此时 NapCat 默认会安装到：

```text
/root/Napcat
```

如果你用普通用户安装，则默认会安装到：

```text
/home/你的用户名/Napcat
```

#### 2. 下载 NapCat 安装脚本

建议单独建一个目录放安装脚本：

```bash
mkdir -p /opt/napcat-installer
cd /opt/napcat-installer
curl -o napcat.sh https://nclatest.znin.net/NapNeko/NapCat-Installer/main/script/install.sh
```

#### 3. 使用交互式安装

第一次安装推荐用 TUI 交互模式：

```bash
sudo bash napcat.sh --tui
```

安装界面里建议这样选：

```text
安装方式：本地安装 / Shell 安装 / Rootless
是否使用 Docker：否
是否安装 NapCat TUI-CLI：是
代理：如果 GitHub 下载慢，可以选择脚本提供的代理；如果服务器访问 GitHub 正常，选择不使用代理
```

如果你不想走交互界面，也可以使用非交互命令：

```bash
sudo bash napcat.sh --docker n --cli y --proxy 0
```

如果 GitHub 下载很慢，可以把 `--proxy 0` 改成脚本提示的可用代理编号，例如：

```bash
sudo bash napcat.sh --docker n --cli y --proxy 1
```

#### 4. 确认安装目录

安装完成后检查：

```bash
ls -la ~/Napcat
```

如果你是 root 用户，等价于：

```bash
ls -la /root/Napcat
```

如果能看到 NapCat、QQ 或相关启动文件，说明安装完成。

#### 5. 启动 NapCat 并登录 QQ

安装脚本结束后，通常会在终端输出启动方式、WebUI 地址、token 或后续管理命令。优先按照安装脚本最后输出的提示启动。

如果安装了 NapCat TUI-CLI，可以使用它在 SSH 里管理 NapCat。不同版本命令名可能略有变化，常见方式是查看系统里是否存在 NapCat 相关命令：

```bash
ls /usr/local/bin | grep -i napcat
```

也可以搜索安装目录：

```bash
find ~/Napcat -maxdepth 3 -type f | grep -Ei 'napcat|launcher|qq$'
```

启动后，NapCat 会要求登录 QQ。一般有两种方式：

```text
1. 终端显示二维码，直接用 QQ 扫码登录。
2. 打开 NapCat WebUI，在 WebUI 里扫码登录。
```

#### 6. 打开 NapCat WebUI

NapCat WebUI 常见端口是 `6099`，具体以启动日志输出为准。

如果 WebUI 只监听服务器本机地址，推荐用 SSH 端口转发：

```bash
ssh -L 6099:127.0.0.1:6099 root@你的服务器IP
```

然后在你自己的电脑浏览器打开：

```text
http://127.0.0.1:6099
```

如果启动日志里给了 token，就按日志里的地址访问，例如：

```text
http://127.0.0.1:6099/webui/?token=日志里显示的token
```

不要长期把 NapCat WebUI 直接暴露到公网。如果必须临时开放，配置完 webhook 后建议立刻关闭公网访问。

#### 7. 让 NapCat 长期运行

NapCat 必须一直运行，后端才能持续收到群消息。

优先检查安装器是否已经创建 systemd 服务：

```bash
systemctl list-unit-files | grep -i napcat
```

如果存在 `napcat.service`，直接启用开机自启：

```bash
sudo systemctl enable --now napcat
sudo systemctl status napcat --no-pager
journalctl -u napcat -f
```

如果安装器没有创建服务，并且 NapCat 安装在 `/root/Napcat`，可以创建：

```bash
sudo nano /etc/systemd/system/napcat.service
```

写入：

```ini
[Unit]
Description=NapCat QQ
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=root
Environment=HOME=/root
WorkingDirectory=/root/Napcat/opt/QQ
ExecStart=/usr/bin/xvfb-run -a /root/Napcat/opt/QQ/qq --no-sandbox --disable-gpu
Restart=always
RestartSec=10
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

如果使用普通用户安装，把 `User`、`HOME`、`WorkingDirectory` 和 `ExecStart` 改为实际用户名和安装路径。保存后执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now napcat
sudo systemctl status napcat --no-pager
journalctl -u napcat -f
```

`screen` 只建议用于首次调试或查看交互式启动过程，不应作为正式的开机自启方案：

```bash
screen -S napcat
# 在 screen 中运行实际的 NapCat 启动命令
# Ctrl+A，然后按 D，临时退出会话
screen -r napcat
screen -ls
```

配置完成后必须执行一次 `sudo reboot` 验证 NapCat、QQ 登录和 HTTP Client 都能自动恢复。不要让旧服务器和新服务器上的 NapCat 同时登录同一个 QQ。

#### 8. 安装后的检查

确认 NapCat 端口：

```bash
sudo ss -lntp | grep -E '6099|napcat|qq'
```

确认 NapCat 进程：

```bash
ps aux | grep -Ei 'napcat|qq' | grep -v grep
```

确认后端也在：

```bash
sudo systemctl status qq-message
```

这两个都正常后，再继续配置下面的 HTTP Client webhook。

### 方案 A：NapCat 和后端在同一台服务器

这是最适合 24 小时自动总结的方式。

同一台服务器部署时，NapCat 不需要通过公网域名访问后端，直接走本机地址即可：

```text
http://127.0.0.1:8000/webhook/onebot?token=你的QQ_SUMMARY_WEBHOOK_TOKEN
```

其中 `token` 必须和 `.env` 里的 `QQ_SUMMARY_WEBHOOK_TOKEN` 完全一致。

#### 1. 先确认后端本机可访问

在服务器上执行：

```bash
curl http://127.0.0.1:8000/api/auth/status
```

如果你设置了 `QQ_SUMMARY_WEB_PASSWORD`，正常会返回类似：

```json
{"auth_required": true, "authenticated": false}
```

再检查后端服务：

```bash
sudo systemctl status qq-message
```

如果这里正常，说明 NapCat 可以通过 `127.0.0.1:8000` 把消息推给后端。

#### 2. 准备 webhook token

查看 `.env` 里的 token：

```bash
cd /opt/qq_message
grep QQ_SUMMARY_WEBHOOK_TOKEN .env
```

假设输出是：

```env
QQ_SUMMARY_WEBHOOK_TOKEN=abc123456
```

那么 NapCat webhook 地址就是：

```text
http://127.0.0.1:8000/webhook/onebot?token=abc123456
```

注意：这里使用的是 `QQ_SUMMARY_WEBHOOK_TOKEN`，不是 `QQ_SUMMARY_WEB_PASSWORD`。

#### 3. 打开 NapCat WebUI

在服务器上启动 NapCat 后，打开 NapCat WebUI。

如果你的 NapCat WebUI 只监听本机地址，通常需要用 SSH 端口转发从本地电脑打开：

```bash
ssh -L 6099:127.0.0.1:6099 root@你的服务器IP
```

然后在本地浏览器打开：

```text
http://127.0.0.1:6099
```

如果你的 NapCat WebUI 已经暴露在公网，也可以直接打开它的公网地址，但不建议长期把 NapCat WebUI 暴露到公网。

#### 4. 新增 HTTP Client

在 NapCat WebUI 里找到类似下面的位置：

```text
网络配置 / Webhook / HTTP Client / HTTP 上报
```

不同 NapCat 版本菜单名字可能略有不同，目标是新增一个“HTTP Client”或“HTTP 上报”配置。

推荐命名：

```text
qq-message-summary
```

字段按下面填写：

```text
启用 / enable: true
名称 / name: qq-message-summary
URL / url: http://127.0.0.1:8000/webhook/onebot?token=你的QQ_SUMMARY_WEBHOOK_TOKEN
上报格式 / messagePostFormat: array
是否上报自己消息 / reportSelfMessage: false
Token / token: 留空
```

如果界面里有这些选项，也建议这样设置：

```text
上报方式: POST
Content-Type: application/json
超时时间: 默认即可
重试: 默认即可
启用群消息上报: 开启
启用私聊消息上报: 可不开启
```

本项目只处理 QQ 群消息，私聊、通知、好友请求等事件会被后端忽略。

#### 5. 保存并测试

保存 HTTP Client 后，在任意一个 QQ 群里发一条新消息，或者等群里出现新消息。

然后在服务器上看后端日志：

```bash
journalctl -u qq-message -f
```

正常情况下会看到类似请求记录：

```text
"POST /webhook/onebot?token=xxx HTTP/1.1" 200
```

再打开网页：

```text
https://message.tanlb.xyz
```

登录后应该能看到新群或新的未读消息。

#### 6. 常见配置错误

如果 NapCat 显示上报失败，优先检查这些：

```text
1. 后端服务 qq-message 是否正在运行。
2. URL 是否写成了 http://127.0.0.1:8000/webhook/onebot?token=xxx。
3. token 是否和 .env 里的 QQ_SUMMARY_WEBHOOK_TOKEN 完全一致。
4. messagePostFormat 是否为 array。
5. NapCat 是否真的和后端在同一台服务器上。
```

如果后端日志出现：

```json
{"error": "Invalid webhook token"}
```

说明 webhook URL 里的 `token` 写错了。

如果后端日志一直没有任何 `/webhook/onebot` 请求，说明 NapCat 没有把事件推到后端。重点检查 NapCat HTTP Client 是否启用、URL 是否保存成功、NapCat 是否在线。

#### 7. 同机部署为什么不用公网域名

同机部署时推荐用：

```text
http://127.0.0.1:8000/webhook/onebot?token=xxx
```

而不是：

```text
https://message.tanlb.xyz/webhook/onebot?token=xxx
```

原因是本机地址更稳定，也不会经过 Cloudflare、Nginx、公网 DNS 或服务器安全组。即使公网域名临时访问异常，只要后端和 NapCat 都在同一台服务器上，NapCat 仍然可以继续把消息推给后端。

### 方案 B：NapCat 在另一台机器

如果 NapCat 在你的 Windows 电脑或另一台服务器上，webhook 地址填写：

```text
https://message.tanlb.xyz/webhook/onebot?token=你的QQ_SUMMARY_WEBHOOK_TOKEN
```

注意：这种方式下，NapCat 所在机器必须一直运行并保持 QQ 登录。否则服务器后端虽然在线，但不会收到新消息。

## 11. 登录网页并开启自动总结

打开：

```text
https://message.tanlb.xyz
```

输入 `.env` 里的：

```text
QQ_SUMMARY_WEB_PASSWORD
```

进入页面后：

1. 等待 NapCat 推送新群消息。
2. 左侧选择一个群。
3. 打开该群的“自动总结”开关。
4. 之后这个群未读消息达到 `QQ_SUMMARY_AUTO_SUMMARY_THRESHOLD` 后，后台会自动总结。

注意：`.env` 里的 `QQ_SUMMARY_AUTO_SUMMARY_ENABLED=true` 只是全局允许自动总结，具体哪些群能自动总结，还要在网页里手动打开。

### 手动总结、进度和断点恢复

当前手动总结支持填写 `1-5000` 条，留空默认按上限 5000 条处理。手动总结从最早的未读消息开始，自动总结仍固定每批最多 500 条。

手动总结使用后台任务：

```text
1. 网页创建任务后立即返回，不会持续占用一个 Nginx 请求。
2. 后台按分块并行调用 DeepSeek，网页显示已完成块数和百分比。
3. 刷新或关闭网页不会中断任务，重新进入该群会恢复进度显示。
4. 每个已完成分块都会写入 SQLite 检查点。
5. qq-message 服务重启后，未完成任务会自动重新入队，只补做未完成分块。
6. 最终总结、已读游标和任务完成状态在同一个 SQLite 事务中提交。
```

不要在任务运行时删除本批消息对应的历史记录。迁移、更新或重启前优先等待任务完成；无法等待时，必须完整备份并迁移 `qq_summary.sqlite3`，才能保留任务快照和检查点。

## 12. 部署后的检查清单

检查后端服务：

```bash
sudo systemctl status qq-message
```

检查后端接口：

```bash
curl http://127.0.0.1:8000/api/auth/status
```

检查 Nginx：

```bash
sudo nginx -t
sudo systemctl status nginx
```

检查网页认证状态：

```bash
curl https://message.tanlb.xyz/api/auth/status
```

检查数据库文件：

```bash
ls -lh /opt/qq_message/data/qq_summary.sqlite3
```

查看后端日志：

```bash
journalctl -u qq-message -n 200 --no-pager
```

实时查看后端日志：

```bash
journalctl -u qq-message -f
```

## 13. 更新代码流程

以后本地修改代码并推送到 GitHub 后，按下面流程更新。不要在本地修改尚未推送时直接操作服务器，否则 `git pull` 无法获得新代码。

进入项目目录并检查工作区：

```bash
cd /opt/qq_message
git status --short
git log -1 --oneline
```

如果 `git status --short` 有输出，先确认这些服务器本地修改的来源，不要直接执行 `git reset --hard`。

记录当前提交并在线备份 SQLite：

```bash
git rev-parse HEAD | tee /tmp/qq-message-old-commit

mkdir -p /opt/qq_message/backups
BACKUP="/opt/qq_message/backups/qq_summary_$(date +%F_%H-%M-%S).sqlite3"
sqlite3 /opt/qq_message/data/qq_summary.sqlite3 ".backup '$BACKUP'"
sha256sum "$BACKUP"
ls -lh "$BACKUP"
```

`.backup` 会生成一致的 SQLite 快照，不需要为了备份停止正在运行的后端。正在执行的手动总结任务及其分块检查点也会被保存。

拉取指定远程分支：

```bash
git pull --ff-only origin main
git log -1 --oneline
```

构建前端：

```bash
cd /opt/qq_message/frontend
npm ci
npm run build
```

运行后端测试：

```bash
cd /opt/qq_message
source .venv/bin/activate
python -m unittest discover -s tests
```

测试通过后重启后端。正在执行的手动总结任务会从 SQLite 检查点重新入队，已经完成的分块不会重复请求：

```bash
sudo systemctl restart qq-message
```

确认状态和启动日志：

```bash
sudo systemctl status qq-message --no-pager
sudo journalctl -u qq-message -n 200 --no-pager
```

数据库较大时，启动阶段的历史 CQ 消息修复可能需要一段时间。必须在日志中看到下面内容后，才算后端已经开始监听：

```text
QQ Message Summary running at http://127.0.0.1:8000
```

检查前端构建文件是否已经在线：

```bash
LOCAL_ASSET=$(grep -o 'assets/index-[^" ]*\.js' /opt/qq_message/frontend/dist/index.html | head -n 1)
REMOTE_ASSET=$(curl -s https://message.tanlb.xyz/ | grep -o 'assets/index-[^" ]*\.js' | head -n 1)
echo "local:  $LOCAL_ASSET"
echo "remote: $REMOTE_ASSET"
```

两边文件名应当一致。浏览器仍显示旧页面时使用 `Ctrl+F5` 强制刷新。

更新后出现问题时回退：

```bash
cd /opt/qq_message
OLD_COMMIT=$(cat /tmp/qq-message-old-commit)
git switch --detach "$OLD_COMMIT"

cd frontend
npm ci
npm run build

sudo systemctl restart qq-message
sudo systemctl status qq-message --no-pager
```

回退代码前不要直接覆盖当前数据库。新代码已经写入的新表通常会被旧代码忽略；只有确认数据库迁移导致问题时，才停止 NapCat 和后端，再恢复更新前的数据库备份。

## 14. 备份数据库

SQLite 数据库默认在：

```text
/opt/qq_message/data/qq_summary.sqlite3
```

使用 SQLite 在线备份接口，不要在后端持续写入时直接 `cp` 数据库文件：

```bash
mkdir -p /opt/qq_message/backups

BACKUP="/opt/qq_message/backups/qq_summary_$(date +%F_%H-%M-%S).sqlite3"
sqlite3 /opt/qq_message/data/qq_summary.sqlite3 ".backup '$BACKUP'"
chmod 600 "$BACKUP"

sqlite3 "$BACKUP" "PRAGMA quick_check;"
sha256sum "$BACKUP"
```

查看备份：

```bash
ls -lh /opt/qq_message/backups
```

`PRAGMA quick_check` 应输出 `ok`。建议后续加定时备份，例如每天凌晨备份一次，并定期删除很老的备份。备份文件包含聊天内容、总结和任务检查点，不能放到公开下载目录。

## 15. 数据增长和清理建议

消息、总结、群配置都保存在 SQLite 里。消息越多，`data/qq_summary.sqlite3` 会越大。

建议：

```text
1. 定期在网页里按日期删除不需要的历史消息。
2. 不要长期打开 QQ_SUMMARY_WEBHOOK_DEBUG。
3. 定期备份数据库。
4. 如果未来消息量非常大，再考虑迁移到 PostgreSQL。
```

查看数据库大小：

```bash
du -h /opt/qq_message/data/qq_summary.sqlite3
```

如果删除了大量历史消息，可以压缩 SQLite 数据库：

```bash
sqlite3 /opt/qq_message/data/qq_summary.sqlite3 "VACUUM;"
```

如果提示 `sqlite3` 不存在：

```bash
sudo apt install -y sqlite3
```

执行 `VACUUM` 前建议先备份数据库。

## 16. 安全建议

服务器部署时建议至少做到：

```text
1. 必须设置 QQ_SUMMARY_WEB_PASSWORD。
2. 必须设置 QQ_SUMMARY_WEBHOOK_TOKEN。
3. 对外访问使用 HTTPS。
4. QQ_SUMMARY_HOST 保持 127.0.0.1，由 Nginx 代理。
5. .env 权限设置为 600。
6. 不要把 .env 提交到 GitHub。
7. 不要长期开启 QQ_SUMMARY_WEBHOOK_DEBUG。
```

如果只允许自己访问，也可以用防火墙限制来源 IP。

例如只开放 SSH、HTTP、HTTPS：

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80
sudo ufw allow 443
sudo ufw enable
sudo ufw status
```

## 17. 常见问题

### 网页能打开，但没有群消息

检查：

```text
1. NapCat 是否在线。
2. QQ 群是否有 NapCat 启动之后的新消息。
3. NapCat webhook 地址是否正确。
4. webhook URL 的 token 是否和 QQ_SUMMARY_WEBHOOK_TOKEN 一致。
```

查看后端日志：

```bash
journalctl -u qq-message -f
```

### 打开网页后一直要求登录

确认 `.env` 里的 `QQ_SUMMARY_WEB_PASSWORD` 是你输入的密码。

如果刚重启过后端，之前的登录状态会失效，需要重新登录。当前项目的登录 session 保存在内存里，服务重启后会清空。

### 自动总结没有触发

检查：

```text
1. QQ_SUMMARY_AUTO_SUMMARY_ENABLED 是否为 true。
2. 网页里是否给这个群打开了自动总结。
3. 这个群未读消息数量是否达到 QQ_SUMMARY_AUTO_SUMMARY_THRESHOLD。
4. DeepSeek API Key 是否有效。
```

查看日志：

```bash
journalctl -u qq-message -f
```

如果触发成功，日志里会看到类似：

```text
Auto summarized 500 messages for group xxx as summary #1
```

### 总结失败

检查 DeepSeek 配置：

```bash
cd /opt/qq_message
source .venv/bin/activate
python -c "from app.config import load_settings; s=load_settings(); print(bool(s.deepseek_api_key), s.deepseek_model, s.deepseek_base_url)"
```

如果第一个输出是 `False`，说明 `DEEPSEEK_API_KEY` 没有加载成功。

### Nginx 502

通常说明后端没有运行。

检查：

```bash
sudo systemctl status qq-message
curl http://127.0.0.1:8000/api/auth/status
```

如果后端没有启动，查看日志：

```bash
journalctl -u qq-message -n 200 --no-pager
```

### 点击总结出现 504 或 `Unexpected token '<'`

当前手动总结接口只创建后台任务，应当快速返回 HTTP `202`。如果 `/api/groups/.../summarize` 仍然等待很久并返回 `504 Gateway Timeout`，通常说明线上仍在运行旧版同步后端。

如果页面显示：

```text
Unexpected token '<', "<!DOCTYPE "... is not valid JSON
```

说明浏览器使用的也是旧前端。新版前端会把非 JSON 响应显示为明确的 HTTP 错误，不会直接抛出 JSON 解析异常。

在服务器检查源码和进程启动时间：

```bash
cd /opt/qq_message
git log -1 --oneline
grep -n "MANUAL_SUMMARY_QUEUE" app/server.py
grep -n "getSummaryTask" frontend/src/services/api.js

sudo systemctl show qq-message -p MainPID -p ExecMainStartTimestamp -p ActiveState
sudo journalctl -u qq-message -n 200 --no-pager
sudo tail -n 100 /var/log/nginx/error.log
```

对比线上和本机构建文件：

```bash
grep -o 'assets/index-[^" ]*\.js' /opt/qq_message/frontend/dist/index.html
curl -s https://message.tanlb.xyz/ | grep -o 'assets/index-[^" ]*\.js'
```

如果不一致，重新执行第 13 节的 `npm ci`、`npm run build` 和 `systemctl restart qq-message`，然后用 `Ctrl+F5` 刷新浏览器。

### 前端页面不是最新的

重新构建前端并重启后端：

```bash
cd /opt/qq_message
git pull
cd frontend
npm ci
npm run build
sudo systemctl restart qq-message
```

## 18. 最终验收

部署完成后，按下面顺序验收：

1. `https://message.tanlb.xyz` 可以打开。
2. 输入网页登录密码可以登录。
3. NapCat 已经登录 QQ。
4. 群里发一条新消息，网页左侧能看到群。
5. 未读消息里能看到新消息。
6. 点击“总结未读”可以生成总结。
7. 给测试群打开自动总结。
8. 后端服务重启后能自动恢复：

```bash
sudo systemctl restart qq-message
sudo systemctl status qq-message
```

9. 服务器重启后服务能自动启动：

```bash
sudo reboot
```

重连服务器后检查：

```bash
sudo systemctl status qq-message
sudo systemctl status nginx
```

全部通过后，部署就完成了。
