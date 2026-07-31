# TeamBoss

TeamBoss 是一个自托管的 ChatGPT Team / Business 工作区管理面板。用你自己的管理员账号登录后，它把多个团队的席位、成员、到期和账单集中到一个后台，并支持定时自动化与 Telegram 通知。

## 免责声明

1. 本项目仅供学习与技术研究，请勿用于任何非法用途。
2. 本项目与 OpenAI 无任何关联，并非其官方产品，相关商标归各自所有者所有。
3. 本项目所依赖的接口、成员操作方式及计费策略，均可能被官方随时调整，作者不保证功能的可用性、准确性与持续性。
4. 项目内展示的席位、账单、金额、到期时间等数据仅供参考，一切以官方后台为准。
5. 使用者应自行遵守所在地法律法规，以及与服务提供方之间的服务条款，并对自身使用行为负责。
6. 因使用本项目而产生的任何直接或间接后果，包括但不限于账号异常、财务损失、数据丢失，均由使用者自行承担，作者不承担任何责任。
7. 使用本项目即视为已阅读并同意本声明全部内容；若不同意，请停止使用并删除相关文件。

## 功能

- **多团队总览** —— 一屏看所有团队的席位使用（ChatGPT / Codex）、成员数、到期情况。
- **成员管理** —— 邀请、移除、切换席位类型（ChatGPT / Codex）。
- **到期与自动踢人** —— 给成员设到期时间，后台定时任务到点自动移除。
- **自助兑换** —— 生成一次性 access token，成员凭邮箱 + token 自助加入 / 续期 / 查自己的状态，无需你手动操作。
- **Session 管理** —— 导入 / 导出 ChatGPT 账号会话，access_token 过期自动用 session cookie 刷新。
- **财务** —— 订阅、账单、汇率换算展示。
- **巡检与告警** —— 定时巡检团队健康，异常通过 Telegram 告警。
- **Telegram 机器人** —— 通知 + 常用命令（邀请 / 移除 / 查成员 / 查自己的到期等；管理命令仅限已配对管理员）。
- **操作日志** —— 成员操作、团队管理、系统设置、代理、Telegram 配置、财务设置、巡逻武装等写操作均留痕可查；密钥类字段只记掩码，不落明文。

## 技术栈

FastAPI + SQLite（后端）· React + Vite（前端）· Nginx（静态托管 + API 反代）· Docker Compose（推荐部署方式）。

## 部署

### Docker Compose

如需自托管，可用 Docker Compose 自行部署。需要 Docker Engine（含 Compose v2）。

1. 准备配置：

   ```bash
   cp .env.example .env
   ```

   **务必**把 `AUTO_TEAM_ADMIN_PASSWORD` 和 `AUTO_TEAM_API_KEY` 改成随机强值——它们只在首次初始化数据库时写入，之后在后台里改。可选 `AUTO_TEAM_PORT` 改对外端口（默认 8080）。

2. 构建并后台启动：

   ```bash
   docker compose up -d --build
   ```

3. 打开 `http://<服务器地址>:8080`，用管理员密码登录。检查：

   ```bash
   docker compose ps
   curl -fsS http://127.0.0.1:8080/api/health
   ```

维护：

```bash
docker compose logs -f
git pull && docker compose up -d --build   # 升级
docker compose down                        # 停止（不删数据）
```

业务数据（SQLite、会话、备份）存在名为 `auto_team_data` 的 Docker 命名卷里；`docker compose down` 不会删，只有 `down -v` 或手动删卷才会清。可用 `AUTO_TEAM_DATA_VOLUME` 换卷名，便于并行测试或迁移。

## 备份与恢复

### 自动备份

使用 `scripts/backup.py` 定期备份数据库和会话目录。脚本使用 SQLite 在线备份机制确保一致性，并会自动使用 `AUTO_TEAM_DATA_DIR`；未设置时使用 `backend/data`。

Docker Compose 部署：

```bash
docker compose exec backend python scripts/backup.py
docker compose exec backend python scripts/backup.py --keep-count 20
```

本机 Python 部署：

```bash
# 单次备份（使用默认参数）
python scripts/backup.py

# 自定义参数
python scripts/backup.py --keep-count 20 --backup-dir /path/to/backups
```

配置定期备份（cron，每天早上 2 点）：

```bash
0 2 * * * cd <项目目录> && python scripts/backup.py >> /var/log/auto_team_backup.log 2>&1
```

### 恢复数据

使用 `scripts/restore.sh` 恢复备份。恢复时必须停止后端写入；脚本会先校验数据库完整性或归档路径，再原子替换现有数据。

Docker Compose 部署（备份文件位于数据卷的 `/app/data/backups/`）：

```bash
docker compose stop backend
docker compose run --rm backend ./scripts/restore.sh \
  /app/data/backups/app-20260723T063420Z.db --skip-service-check
docker compose start backend
curl -fsS http://127.0.0.1:8080/api/health
```

本机 systemd 部署：

```bash
# 查看恢复用法
./scripts/restore.sh

# 恢复数据库备份
sudo systemctl stop auto-team.service
./scripts/restore.sh <项目目录>/backend/data/backups/app-20260723T063420Z.db
sudo systemctl start auto-team.service

# 恢复会话备份
sudo systemctl stop auto-team.service
./scripts/restore.sh <项目目录>/backend/data/backups/sessions-20260723T063420Z.tar.gz
sudo systemctl start auto-team.service
```

脚本会在替换前保留现有数据库或会话目录的恢复前副本。

## 配置项

| 变量 | 说明 | 默认 |
|---|---|---|
| `AUTO_TEAM_ADMIN_PASSWORD` | 初始管理员密码（首启写库，≥8 位） | 必填 |
| `AUTO_TEAM_API_KEY` | 后端 API Key，保留 `atk_` 前缀便于识别 | 必填 |
| `AUTO_TEAM_CORS_ORIGINS` | 允许的前端来源（逗号分隔） | 本地开发端口 |
| `AUTO_TEAM_TRUSTED_PROXIES` | 可信反向代理来源（逗号分隔 IP/CIDR）。只有来自这些地址的请求，其 `X-Real-IP`/`X-Forwarded-For` 才会被用作自助接口的限流身份；其余一律按直连 IP 计数，防止伪造请求头绕过限流。没有反代、端口直接暴露时设为空值 | 回环 + 内网私有段 |
| `AUTO_TEAM_PORT` | 对外 Web 端口 | `8080` |
| `VITE_API_BASE_URL` | 前端 API 基址，留空用 Vite 代理 | 空 |

完整清单见 [`.env.example`](./.env.example)。

## 安全须知

- **必须放在 TLS / 反向代理 / 防火墙后面。** 生产环境中后端只监听 127.0.0.1:18087（本地环回），由 Nginx 反向代理对外暴露；务必配置 HTTPS 并限制访问来源。
- **会话和密钥在数据卷里是明文存储**（ChatGPT 账号会话、API Key、Telegram token）。这是这类工具的固有风险：任何拿到数据卷 / 备份的人即可完全接管被管理的工作区。**请严格限制数据目录权限**：
  - 数据目录（`backend/data/`）：权限 `700`（仅所有者可访问）
  - 数据库文件（`backend/data/app.db`）：权限 `600`（仅所有者可读写）
  - 会话文件（`backend/data/sessions/*.json`）：权限 `600`（仅所有者可读写）
  - 备份文件（`backend/data/backups/*`）：权限 `600`（仅所有者可读写）

  妥善保管备份文件，建议定期离线备份到安全位置。
- `.env`、数据库、会话文件、备份已被 git 和 Docker 构建上下文忽略，不会进仓库或镜像。
- 首次启动后请通过后台修改密码 / 轮换 API Key，不要长期使用 `.env` 里的初始值。

## 本地开发

Windows 可用一键脚本 `start.ps1`（或 `start.bat`）；或手动：后端 `uvicorn app.main:app`，前端 `npm run dev`。详见脚本内注释与 `.env.example`。

## 许可证

[MIT](./LICENSE)。
