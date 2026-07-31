# YSYX 学习追踪器

`ysyx-study-tracker` 是面向华南农业大学“一生一芯”学习小组的共享学习记录服务。学生注册后可记录学习时长和学习内容；管理员可查看成员状态、维护打卡记录并导出 CSV。

项目不依赖第三方 Python 包，使用 Python 标准库提供网页和 API，数据保存在 SQLite。

## 功能

- 学号、姓名、班级和密码注册，学生只能访问自己的学习数据。
- 开始和结束学习打卡，结束时必须填写本次学习内容。
- 以 Asia/Shanghai 时区按周统计；跨周的打卡会按周拆分计时。
- 查看个人历史记录和小组学习概览。
- 管理员登录、查看成员、重置密码、补录或删除记录、删除账号，以及导出全部已完成打卡的 CSV。

## 快速开始

要求：Python 3.12 或更高版本。

```bash
git clone <仓库地址>
cd ysyx-study-tracker
ADMIN_USERNAME=admin ADMIN_PASSWORD='替换为随机强密码' python3 server.py
```

打开 `http://127.0.0.1:8000/`。服务默认监听全部网络接口，因此同一局域网可以通过终端输出的地址访问。

首次启动会直接创建当前版本所需的空 SQLite 数据库和数据表。

仅为本地演示且未设置环境变量时，管理员账号为 `admin`，密码为 `admin123456`。不要在公网继续使用默认密码。

## Docker 部署

Dockerfile 已配置为：服务监听 `0.0.0.0:8000`，数据库写入容器内的 `/data/ysyx-study-tracker.sqlite3`，并将 `/data` 声明为数据卷。运行容器时必须挂载持久化目录或命名卷，否则删除容器后可能丢失数据。

构建镜像：

```bash
docker build -t ysyx-study-tracker:latest .
```

使用本机目录保存数据：

```bash
mkdir -p data
docker run -d \
  --name ysyx-study-tracker \
  --restart unless-stopped \
  -p 8000:8000 \
  -v "$(pwd)/data:/data" \
  -e ADMIN_USERNAME=admin \
  -e ADMIN_PASSWORD='替换为随机强密码' \
  ysyx-study-tracker:latest
```

或者使用 Docker 命名卷：

```bash
docker volume create ysyx-study-tracker-data
docker run -d \
  --name ysyx-study-tracker \
  --restart unless-stopped \
  -p 8000:8000 \
  -v ysyx-study-tracker-data:/data \
  -e ADMIN_USERNAME=admin \
  -e ADMIN_PASSWORD='替换为随机强密码' \
  ysyx-study-tracker:latest
```

查看运行状态和日志：

```bash
docker ps --filter name=ysyx-study-tracker
docker logs -f ysyx-study-tracker
```

更新应用时，保留原数据卷，重新构建并替换容器：

```bash
docker build -t ysyx-study-tracker:latest .
docker rm -f ysyx-study-tracker
# 使用上面的 docker run 命令再次启动，并保持原来的 -v 挂载。
```

## 配置项

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `HOST` | `0.0.0.0` | HTTP 服务监听地址。 |
| `PORT` | `8000` | HTTP 服务端口。容器端口映射应与其一致。 |
| `YSYX_DB_FILE` | 项目目录下的 `ysyx-study-tracker.sqlite3` | SQLite 数据库文件路径。Docker 镜像中默认是 `/data/ysyx-study-tracker.sqlite3`。 |
| `ADMIN_USERNAME` | `admin` | 管理员用户名。 |
| `ADMIN_PASSWORD` | `admin123456` | 管理员密码。公网部署必须覆盖。 |

`HOST` 和 `PORT` 也可作为 `server.py` 的前两个位置参数传入，但环境变量优先。

## 公网部署

建议将容器只暴露给本机，再由 Nginx 或 Caddy 提供 HTTPS：

```bash
docker run -d \
  --name ysyx-study-tracker \
  --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -v ysyx-study-tracker-data:/data \
  -e ADMIN_USERNAME=admin \
  -e ADMIN_PASSWORD='替换为随机强密码' \
  ysyx-study-tracker:latest
```

Nginx 站点配置示例：

```nginx
server {
    listen 80;
    server_name tracker.example.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

为域名配置 TLS 证书后，将访问地址发给成员。防火墙只需对公网开放反向代理使用的 `80` 和 `443` 端口；不要直接开放数据库文件或容器内部端口。

## 数据备份与恢复

生产数据位于 `YSYX_DB_FILE` 指定的 SQLite 文件。SQLite 使用 WAL 模式，备份时建议通过 SQLite 的在线备份命令生成一致性副本：

```bash
docker exec ysyx-study-tracker python -c "import sqlite3; src=sqlite3.connect('/data/ysyx-study-tracker.sqlite3'); dst=sqlite3.connect('/data/backup.sqlite3'); src.backup(dst); dst.close(); src.close()"
docker cp ysyx-study-tracker:/data/backup.sqlite3 ./ysyx-study-tracker-backup.sqlite3
```

恢复时先停止容器，用备份文件替换持久化目录或卷中的 `ysyx-study-tracker.sqlite3`，然后重新启动。请定期将备份复制到服务器之外。

## 安全与限制

- 管理员和学生会话令牌保存在 SQLite，服务重启后仍可在有效期内使用；修改管理员密码后应重启服务使新配置生效。
- 学生密码以 PBKDF2 哈希保存，管理员不能读取明文密码。后台重置密码后，初始密码为 `123456`。
- 这是一个单进程、基于 SQLite 的小组服务，适合单实例部署。不要同时运行多个容器并写入同一个数据库文件。
- 应通过 HTTPS 对公网提供服务，并设置强管理员密码和定期数据库备份。

## 测试

```bash
python3 -m unittest -v
```

测试覆盖当前 SQLite 存储、历史打卡导出、跨周计时、概览统计和状态接口校验。
