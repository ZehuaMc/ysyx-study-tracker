# 华南农业大学一生一芯学习打卡：公网部署

当前网页有两种运行方式：

## GitHub 与朋友服务器的关系

GitHub 只负责保存和分发代码，不能运行当前的 Python 共享服务。需要把项目上传到 GitHub，再让朋友的服务器拉取代码并启动 server.py。

当前版本包含学号+密码注册/登录、实名学习时间共享、管理员后台、账号密码重置、学生账号注销和学习时长 CSV 导出。打卡结束时可填写本次学习内容。普通用户密码只以哈希保存，后台不能查看明文密码，只能重置为 123456。

页面品牌图片统一放在 `assets/` 目录。当前登录页、学生主页和浏览器图标使用 `assets/scau-emblem.png`、`assets/YSYX_logo.png` 与 `assets/ICIL_logo.jpg`，三枚标识位于 `brand-logo-rail` 品牌栏中。

打开服务地址后首先显示登录页：学生使用已注册的数字学号和密码进入打卡页，姓名与班级仅在“注册新账号”中填写；注册时必须输入两次一致的密码。管理员用 `ADMIN_USERNAME` 与 `ADMIN_PASSWORD` 进入独立后台。

朋友服务器上的基本流程：

    git clone <你的 GitHub 仓库地址>
    cd <仓库目录>
    python3 server.py

服务器还需要开放 HTTP/HTTPS 端口，并建议用 Nginx 或 Caddy 反向代理到本服务的 8000 端口。这样同学访问的是朋友服务器的公网域名，而不是 GitHub 仓库页面。

如果只把 index.html 发布到 GitHub Pages，页面可以打开，但 /api/state 共享接口不会运行，提问、回复和多人打卡无法同步。

## 临时公网演示

先在项目目录启动本地服务：

```powershell
python server.py
```

再使用 Cloudflare Tunnel、ngrok 等隧道工具把 http://127.0.0.1:8000 暴露到公网。

这种方式依赖你的电脑一直开机，适合临时演示，不适合长期使用。

## 长期公网使用

推荐部署到带公网 IP 的云服务器（腾讯云轻量服务器、阿里云、Render、Railway 等）。

服务器启动命令：

```bash
python server.py
```

服务会自动读取平台提供的 PORT 环境变量，并监听 0.0.0.0。

管理员密码通过环境变量配置，生产环境不要使用默认值：

    ADMIN_USERNAME=admin ADMIN_PASSWORD=请替换为强密码 python server.py

未设置环境变量时仅用于本地演示的默认管理员账号是 `admin`，密码是 `admin123456`；公网部署必须立即替换为随机强密码。

Docker 启动：

```bash
docker build -t ysyx-checkin .
docker run -d --name ysyx-checkin -p 8000:8000 -v "$PWD/data:/data" ysyx-checkin
```

生产环境需要把数据文件放在持久化磁盘或数据库中，避免云平台重启后丢失。

## 访问与安全

部署完成后，给同学发送服务器的 HTTPS 地址，例如：

```text
https://checkin.example.com/
```

公网部署建议同时配置：

- 域名和 HTTPS 证书；
- 云服务器防火墙只开放 80/443；
- 定期备份 `ysyx_state.sqlite3`；首次启动会自动导入旧的 `ysyx_shared_state.json`；
- 账号登录和服务端权限校验。

学生账号以学号绑定个人权限，注册姓名需要唯一；服务器会阻止其他账号修改个人打卡和进度。普通学生可实名查看组内成员的学习时间，但看不到他人的学号。数据按账号、学习者、周状态和单次打卡分别保存在 SQLite 表中，旧 JSON 或旧版整体状态仅用于首次自动迁移。数据库启用 WAL 模式，适合长期持续写入。

学生主页提供时间打卡与历史记录；交流和独立随记功能已移除。每次结束打卡时可记录本次完成的内容，并和该条时间记录一起保存。

管理员详情接口和学习状态接口都要求有效的管理员登录令牌；详情响应不会返回学生密码或密码哈希。
