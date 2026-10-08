# 部署与运维

首次启动命令见[上手指南 §1](getting-started.md#1-第一次启动)。本页说明对外开放服务、存储和排障；配置项及默认值以 [`.env.example`](../.env.example) 为准，使用前请阅读[免责声明](disclaimer.md)。

## 部署前提

推荐使用 Docker Engine 和 Compose v2.24 及以上；较早版本不支持 `compose.yaml` 中 `env_file` 的长语法。裸机运行见[本地开发](development.md#启动前后端)。

**一个数据目录只运行一个后端进程，不要加 `--workers` 或启动多个实例。** 同步、巡检、到期移除和 Telegram 轮询都会随进程启动。多个进程会重复执行任务，并争抢同一个 Telegram bot 的长轮询，造成 409 冲突及重复操作。项目提供的 Compose 和 `run_server.py` 均按单进程运行。

## HTTPS 与反向代理

Compose 默认只在宿主机 `127.0.0.1:8080` 提供 HTTP，后端端口不发布到宿主机。对外访问时，在宿主机使用 Nginx / Caddy 终结 HTTPS，再回源到这个回环地址，并按需要限制访问来源。后台是 `/admin`，根路径 `/` 是成员自助页。

Docker 发布端口的规则可能绕过 UFW / firewalld 的宿主机过滤链，不能只靠这些防火墙保护公开绑定的端口。保留回环绑定；`AUTO_TEAM_BIND=0.0.0.0` 只适用于已有可靠访问控制的可信内网。

### 让后端收到真实访客 IP

登录锁定和自助接口限流依赖客户端 IP。经过两层反代时，每一跳都需要正确配置：

1. **宿主机反代**用真实连接来源覆盖访客传入的 `X-Real-IP`。Nginx 在 HTTPS 站点的代理配置中使用：

   ```nginx
   location / {
       proxy_pass http://127.0.0.1:8080;
       proxy_set_header Host $host;
       proxy_set_header X-Real-IP $remote_addr;
       proxy_set_header X-Forwarded-Proto $scheme;
       proxy_read_timeout 120s;
   }
   ```

   这段配置需放在已有证书的 HTTPS 站点内。Caddy 默认会透传访客自带的 `X-Real-IP`，需在 `reverse_proxy` 中加上 `header_up X-Real-IP {remote_host}` 覆盖它。若宿主机前面还有 CDN / 负载均衡，也需先按其实际来源配置可信代理，不能直接相信访客自带的头。

2. **容器内 Nginx**：保持 `AUTO_TEAM_BIND` 为回环地址（默认）时，容器启动时会自动采信来自私有网段的 `X-Real-IP`。此时能连到容器的只有宿主机本地进程（经 Docker 网桥网关转发），所以宿主机反代必须按上一步**覆盖**这个头。`AUTO_TEAM_BIND` 改成其他地址后默认不采信任何来源；若前面确实还有一层反代，用 `AUTO_TEAM_REAL_IP_FROM` 填它的实际连接来源 IP / CIDR（逗号分隔，`none` 表示都不信），改完需重建前端容器。生成逻辑见 [`docker/real-ip.sh`](../docker/real-ip.sh)。
3. **后端**：`AUTO_TEAM_TRUSTED_PROXIES` 只填写可信反代的连接来源 IP / CIDR。后端只采信这些来源发来的 IP 头。裸机 API 没有反代、直接接受客户端连接时，应设为空。

如果容器没有正确恢复客户端 IP，所有访客可能都显示成同一个网桥地址。后端会记录共享身份 warning，并采用全站冷却；这不能替代正确的代理配置，管理员也可能需要等冷却。

## 配置中容易混淆的地方

完整变量、默认值和填写示例见 [`.env.example`](../.env.example)，以下仅说明不同运行方式的边界：

- `AUTO_TEAM_ADMIN_PASSWORD` 和 `AUTO_TEAM_API_KEY` 只在首次初始化数据库时写入。之后在后台修改密码、轮换 Key；改 `.env` 不会重置已有凭据。首启格式要求见[上手指南 §1.1](getting-started.md#11-准备配置)。
- `AUTO_TEAM_PORT` / `AUTO_TEAM_BIND` 控制 Compose 对外 Web 监听；`AUTO_TEAM_BACKEND_HOST` / `AUTO_TEAM_BACKEND_PORT` 用于裸机后端，Compose 内部固定使用 `8000`。
- `AUTO_TEAM_DATA_DIR` 控制裸机数据目录；镜像内使用 `/app/data`。Compose 用 `AUTO_TEAM_DATA_VOLUME` 选择命名卷，默认名是 `auto_team_data`。换卷名会接入另一份数据，不会迁移旧卷。
- `VITE_API_BASE_URL` 是前端开发 / 构建期配置，不能在静态文件构建完成后靠修改环境变量更换。Compose 构建使用同源 `/api`，不读取这个值。
- 环境变量和卷名保留了项目早期的 `AUTO_TEAM_` / `auto_team_data` 命名，以兼容已有部署。

## 访问 chatgpt.com 与网络排障

后端发往 `chatgpt.com` 的请求（包括会话刷新和代理连接测试）使用 `curl_cffi` 模拟 Chrome 的 TLS / HTTP2 指纹。普通 `requests` 即使带 Chrome User-Agent，也可能收到 Cloudflare 的 HTML 403。

模拟目标由 [`chatgpt_client.py`](../backend/app/chatgpt_client.py) 的 `IMPERSONATE` 指定，没有环境变量或后台设置。依赖版本以 [`requirements.txt`](../backend/requirements.txt) 为准；包包含二进制组件，项目 Docker 镜像可直接安装。裸机平台安装失败时可改用 Compose。

浏览器指纹不能解决出口 IP 被拦截的问题。仍出现 403 / 验证页时，检查 Team 的代理和出口，操作见[上手指南 §2.4](getting-started.md#24-需要走代理时)；上游规则变化也可能导致请求失效。

只有 `chatgpt.com` 请求使用 Team 代理。Telegram（`api.telegram.org`）和汇率接口（`open.er-api.com`）使用普通 `requests` 直连，服务器需能直接访问；汇率获取失败时回退到内置静态表。

## 数据与凭据安全

SQLite、会话和备份放在数据目录中。ChatGPT 会话、API Key 和 Telegram token 是明文，持有数据卷或备份的人可能接管工作区。限制宿主机和备份的访问权限：数据目录 `700`，数据库、会话 JSON 和备份文件 `600`；离线副本同样需要保护。

`.env`、数据库、会话和备份已被 Git 与 Docker 构建上下文忽略。首次启动后，在后台修改初始密码；改密码会同时换掉 API Key，旧 Key 立即失效。管理员设置接口会向已认证管理员返回相关密钥，因此管理 Key 和已登录浏览器也必须妥善保管。

`docker compose down` 停服务但保留命名卷；`down -v` 或手动删卷会删除数据。备份、恢复和升级的操作步骤统一见[上手指南 §7](getting-started.md#7-备份)和[§8](getting-started.md#8-升级)。

日常查看状态和日志：

```bash
docker compose ps
docker compose logs -f
```

## 登录锁定与管理员恢复

来源按 IPv4 地址或 IPv6 的 /64 识别：

- 同一来源连续输错 5 次，锁定 15 分钟。
- 全站失败预算每轮只计每个来源的前 5 次失败；总数超过 10 次后，每次失败触发递增冷却，从 1 分钟翻倍至最多 15 分钟。距上次计入预算的失败满 1 小时后，新一轮预算重置。
- 最近 90 天密码登录成功过的来源不受全站冷却影响，但仍受按来源锁定。数据库保存最近最多 50 个来源。
- 无法区分访客的共享代理身份使用全站冷却：第 5 次连续失败开始递增，登录成功清零。先按[真实访客 IP 配置](#让后端收到真实访客-ip)修复代理。

被锁在后台外时：

1. 已登录的浏览器仍可使用保存的 API Key 访问后台，不经过密码登录。
2. 如果有服务器权限，可在自己的受控终端读取当前管理 Key：

   ```bash
   # Docker Compose
   docker compose exec backend python -c "import sqlite3; print(sqlite3.connect('/app/data/app.db').execute(\"SELECT value FROM settings WHERE key='admin_api_key'\").fetchone()[0])"
   # 裸机：使用实际数据目录下的 app.db
   sqlite3 backend/data/app.db "SELECT value FROM settings WHERE key='admin_api_key'"
   ```

   在新设备打开部署地址的 `/admin`，完成使用条款确认，在该站点的浏览器开发者工具控制台执行：

   ```javascript
   localStorage.setItem('auto_team_admin_api_key', '<当前管理 Key>')
   ```

   刷新页面即可进入后台。不要分享终端输出或含 Key 的截图。之后等冷却结束，在该网络用密码成功登录一次，才能进入可信来源名单；直接填 Key 不会登记该名单。
3. 重启后端会清空内存中的锁定、冷却和请求限流，数据库中的可信来源保留。持续的攻击会再次触发冷却，应优先使用现有管理 Key，并修复访问控制。

漏洞报告方式见 [SECURITY.md](../SECURITY.md)。
