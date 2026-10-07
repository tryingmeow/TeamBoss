# TeamBoss

[![CI](https://github.com/tryingmeow/TeamBoss/actions/workflows/ci.yml/badge.svg)](https://github.com/tryingmeow/TeamBoss/actions/workflows/ci.yml)

<a id="production-test-scope"></a>

> [!CAUTION]
> **生产测试范围**：目前仅对已购买的月付 ChatGPT Standard 席位做过生产验证。Premium、年付及超出已购席位的加购操作仍属 Beta 阶段，请在不重要的 Team 上验证后再接入生产。

**TeamBoss** 是一个自托管的 ChatGPT Team / Business 工作区管理面板（FastAPI + React + SQLite）。只需使用 Owner 账号登录，即可一站式管理多个 Team 的席位占用、成员状态、到期清理与账单支出，并支持成员自助兑换及 Telegram 机器人通知。

> 第一次使用？请直接查阅 **[上手指南](docs/getting-started.md)**（包含 Session 提取、环境配置与首次接入步骤）。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/dashboard-dark.png">
  <img alt="Team 列表" src="docs/images/dashboard.png">
</picture>

## 免责声明

**部署和使用之前请务必读完这一节。** 第一次在浏览器里打开后台时，也会要求你逐条勾选确认这些内容（详见 [上手指南 1.3](docs/getting-started.md#13-第一次打开后台)）。

1. **风险自负：** 用不用、怎么用都是你自己的事，由此产生的一切后果由你自己承担。通过非官方逆向通道管理工作区本就不在官方支持范围内，封号、限流、工作区受限等风险请自行掂量，别拿输不起的重要账号折腾。
2. **依赖非官方接口，随时可能失效：** TeamBoss 调的是 ChatGPT 网页端未公开的私有接口，既没有官方 API，也没有任何文档保障。上游接口、交互逻辑和计费策略随时可能调整，功能随时可能暴毙或发生变化。
3. **账号凭据本地明文存储：** 工作区 Owner 的登录凭据（Access Token 与 Session Cookie）以明文形式保存在本地服务器上。任何拿到服务器权限、Docker 数据卷或备份文件的人，都能完整接管你的工作区。请务必做好机器安全与权限隔离。
4. **踢人和清理是真操作：** 到期清理和巡逻踢人会真实调用接口把成员移出工作区。手滑或者规则没设对，把人踢了可撤不回来，上线前一定要仔细核对触发规则和名单。
5. **上生产前先审代码：** 系统拿着你的 Owner 凭证，会自动加人、踢人、改席位，超员策略允许时甚至会自动加购席位刷信用卡扣费。在接入真实工作区前，请务必自己审查一遍源码（或者让 AI 替你审）。
6. **Premium 席位是 Beta 实验性功能：** 涉及 Premium 席位的分配、切换、兑换码和巡逻机制从未在生产环境中充分验证过。这类席位单价高、按月扣费，出错代价大，启用前请谨慎评估风险。
7. **作者概不负责：** 因使用本项目造成的任何直接或间接损失——包括但不限于意外扣费、多买了席位、误踢成员、数据丢失、账号被处置等——项目开发者与贡献者一概不承担任何责任。

**另外：** 本项目为独立开源工具，与 OpenAI 官方无任何隶属、合作或关联。系统展示的席位、账单、金额及到期时间等数据仅供参考，一切以 ChatGPT 官方后台为准。请遵守适用法律法规及服务条款，严禁用于非法用途。使用本项目即视为完全理解并接受本声明全部内容。

## 核心功能

- **多 Team 总览**：聚合查看所有工作区的成员数、席位配额（ChatGPT 蓝 / Codex 紫 / Premium 粉）、到期时间与续费状态。
- **成员与席位调度**：批量邀请、移除成员，支持按各 Team 空位自动分配，支持平滑切换席位类型。
- **超员保护机制**：每个 Team 可独立设置「禁止超员 / 超员需确认 / 自动加购」，彻底避免加人时误刷信用卡加购官方席位。
- **到期自动移出**：支持为成员指定到期时间，后台定时任务到点自动移出工作区。
- **自助兑换码**：生成一次性兑换码，成员凭邮箱自行加入、续期或查询到期日；兑换仅消耗已有付费空位，绝不触发超员加购。
- **巡逻与外部成员防盗**：定时巡检工作区，自动发现并清理绕过后台私自加入的外部成员（出厂默认为演练空跑，需手动激活）。
- **闲置提醒与财务汇总**：续费前 3 天自动提醒未分配的闲置计费席位；同步 Stripe 账单，汇总多币种支出与月付/年付账期。
- **Telegram Bot**：异常自动告警，支持通过指令查询状态或快捷执行常用管理操作。
- **开箱即用演示**：支持纯前端离线演示（`npm run demo`），无需后端与 ChatGPT 账号即可体验全套界面与交互。

## 界面预览

<details>
<summary>点击展开查看各功能模块截图</summary>

### 用户管理
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/users-dark.png">
  <img alt="用户管理" src="docs/images/users.png">
</picture>

### 账单明细
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/team-billing-dark.png">
  <img alt="账单明细" src="docs/images/team-billing.png">
</picture>

### 财务总览
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/finance-dark.png">
  <img alt="财务总览" src="docs/images/finance.png">
</picture>

### 兑换码管理
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/access-tokens-dark.png">
  <img alt="兑换码管理" src="docs/images/access-tokens.png">
</picture>

### 巡逻自动踢人
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/patrol-dark.png">
  <img alt="巡逻自动踢人" src="docs/images/patrol.png">
</picture>

### 成员自助页
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/self-service-dark.png">
  <img alt="成员自助页" src="docs/images/self-service.png">
</picture>

</details>

## 快速部署

推荐使用 **Docker Compose**（需要 Docker Engine 与 Compose v2.24+）。

### 1. 准备配置

```bash
cp .env.example .env
```

修改 `.env` 中的强密码和密钥：
- `AUTO_TEAM_ADMIN_PASSWORD`：管理员初始密码（≥8 位）
- `AUTO_TEAM_API_KEY`：后端 API Key（**必须 `atk_` 开头且 ≥12 位**）
- `AUTO_TEAM_PORT`：对外端口（默认 `8080`）

### 2. 启动服务

```bash
docker compose up -d --build
```

验证运行状态：
```bash
docker compose ps
curl -fsS http://127.0.0.1:8080/api/health
```

### 3. 打开后台

在浏览器打开 `http://127.0.0.1:8080/admin`，确认使用条款后输入密码登录。（根路径 `/` 为成员自助兑换页）。

### 部署关键须知

- **反代与安全**：服务默认仅监听 `127.0.0.1:8080`，严禁公网明文裸奔。对外提供服务时请配置 Nginx/Caddy 等反向代理开启 HTTPS，并配置 `proxy_set_header X-Real-IP $remote_addr;` 透传真实 IP 以保证限流正常。若有多层反代，请按 `docker/nginx.conf` 注释说明配置来源网段。
- **单进程运行**：内置的调度器与 Telegram 轮询在进程生命周期内运行，**切勿添加 `--workers` 参数**，否则会导致多进程并发争抢任务及 Telegram 报错。
- **日常维护**：
  ```bash
  docker compose logs -f                      # 查看日志
  git pull && docker compose up -d --build   # 更新并重新构建
  docker compose down                        # 停止（数据保存在命名卷中）
  ```

## 备份与恢复

### 数据备份
使用在线备份脚本保存 SQLite 数据库及会话目录：
```bash
# Docker Compose
docker compose exec backend python scripts/backup.py --keep-count 20

# 宿主机直接运行
python scripts/backup.py --keep-count 20
```

### 数据恢复
恢复前必须先停止后端写入：
```bash
# Docker Compose
docker compose stop backend
docker compose run --rm backend ./scripts/restore.sh /app/data/backups/<备份文件>.db --skip-service-check
docker compose start backend
```

<a id="本地开发"></a>
<a id="访问-chatgptcomcurl_cffi-浏览器指纹"></a>
## 本地开发与测试

### 依赖与运行环境
- 后端：Python 3.12。发往 chatgpt.com 的请求依赖 `curl_cffi` 模拟 Chrome 浏览器指纹（已包含在 requirements 中，开箱即用；若服务器 IP 被 Cloudflare 风控，请在后台为对应 Team 配置代理）。
- 前端：Node.js 20+。

```bash
# 后端启动
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example ../.env
python run_server.py # 默认监听 127.0.0.1:18087

# 前端开发（另开终端）
cd frontend
npm ci
npm run dev # 访问 http://localhost:5173
```

### 离线演示模式（无需后端与真实账号）
```bash
cd frontend
npm ci
npm run demo # 打开 http://localhost:5173/admin，使用任意密码即可进入体验
```

### 运行测试
```bash
# 后端测试
cd backend
pip install -r requirements-test.txt
AUTO_TEAM_DATA_DIR=$(mktemp -d) python -m unittest discover -s tests

# 前端构建验证
cd frontend
npm run build
```

<a id="安全须知"></a>
## 安全须知与应急处置

- **凭据保护**：ChatGPT 会话及 API Key 均明文保存在数据卷中。请将宿主机 `backend/data/` 目录权限限制为 `700`，文件权限限制为 `600`。
- **后台防爆破与应急登录**：系统内置登录防爆破与全站冷却机制。若因网络/IP 变动被误锁在后台外，可直接从数据库读取当前 API Key 并注入浏览器恢复访问：
  ```bash
  # 从数据库查询当前 API Key
  docker compose exec backend python -c "import sqlite3; print(sqlite3.connect('/app/data/app.db').execute(\"SELECT value FROM settings WHERE key='admin_api_key'\").fetchone()[0])"
  ```
  在浏览器控制台执行 `localStorage.setItem('auto_team_admin_api_key', '<查询到的Key>')` 刷新页面即可直接进入后台。

## 文档索引

- **[上手指南](docs/getting-started.md)**：包含首次接入、Session JSON 获取方式、代理配置与超员策略详解。
- **[管理员 API 文档](ADMIN_API.md)**：用于外部脚本和自动化对接的 HTTP 接口文档。
- **[安全策略](SECURITY.md)**：漏洞报告指引。

## 开源协议

本项目基于 [MIT License](./LICENSE) 开源。
