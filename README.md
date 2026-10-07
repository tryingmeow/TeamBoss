# TeamBoss

[![CI](https://github.com/tryingmeow/TeamBoss/actions/workflows/ci.yml/badge.svg)](https://github.com/tryingmeow/TeamBoss/actions/workflows/ci.yml)

<a id="production-test-scope"></a>

> [!CAUTION]
> **生产测试范围**
>
> 目前仅对已购买的月付 ChatGPT Standard 席位做过生产测试。Premium、年付及超出已购席位的邀请尚未经过生产测试，不保证费用准确或操作成功。

**English:** TeamBoss is a self-hosted admin panel for ChatGPT Team / Business workspaces (FastAPI + React, SQLite, Docker Compose). It puts seats, members, expiry and billing of multiple workspaces in one dashboard, with a per-workspace overage policy, scheduled patrols, auto-removal of expired or unexpected members, self-service redemption codes and a Telegram bot. The UI and docs are in Chinese. It is unofficial and relies on ChatGPT's private, undocumented web endpoints, which can change or break at any time. Use it at your own risk: the authors accept no liability for any loss (charges, added seats, removed members, account restrictions). Review the code before using it in production. Premium operations, annual billing and invitations beyond already-purchased seats remain untested in production; see the production-test notice above. See the disclaimer (免责声明) below and the [getting-started guide](docs/getting-started.md) (Chinese) for a Docker Compose quickstart.

---

TeamBoss 是一个自托管的 ChatGPT Team / Business 工作区管理面板。用你自己的管理员账号登录后，它把多个 Team 的席位、成员、到期和账单集中到一个后台，并支持定时自动化与 Telegram 通知。后台会定时巡检各 Team，发现计划外加入的成员时按你设定的规则移除。

> 第一次用？跳到 **[上手指南](docs/getting-started.md)** —— 从 `.env` 到「后台里有一个能用的 Team、成员能自己兑换」，包含 session JSON 到底从哪里取。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/dashboard-dark.png">
  <img alt="Team 列表：Team 卡片网格，每张卡片按颜色区分显示各类席位（ChatGPT / Codex / Premium）的占用，并列出订阅周期、月费、付款卡后四位和续费状态" src="docs/images/dashboard.png">
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

会话和密钥怎么存、为什么必须放在 TLS 后面，见下文 [安全须知](#安全须知)。

## 功能

- **多 Team 总览** —— 一屏看所有 Team 的席位占用、成员数、到期和续费情况。席位按类型配色：ChatGPT 蓝、Codex 紫、Premium 粉；不认识的席位类型显示成灰色的「其他」，TeamBoss 不把它们算作空位，也不会邀请、切换或自动移除这些人。
- **成员管理** —— 邀请、移除、切换席位类型（ChatGPT / Codex / Premium，**Premium 为 Beta**）；右上角「添加 GPT 成员」可以一次粘贴一批邮箱，按各 Team 的空位自动分配 ChatGPT 席位。
- **每个 Team 自己的超员策略** —— 「禁止超员 / 超员需确认 / 超员自动」三选一（默认「超员需确认」），决定 ChatGPT / Premium 席位满了之后，加人或切换席位是直接拒绝、先问你、还是直接加（直接加会让 ChatGPT 自动加购席位并扣费）。见上手指南 [3.2](docs/getting-started.md#32-超员策略满了之后怎么办)。
- **到期与自动踢人** —— 给成员设到期时间，后台定时任务到点自动移除。
- **自助兑换** —— 生成一次性兑换码，分 ChatGPT 码和 Premium 码（**Beta**）；成员凭邮箱 + 兑换码自助加入 / 续期 / 查自己的状态，无需你手动操作。兑换只用已经付费的空位，**任何超员策略下都不会加购席位**；没有空位就兑换失败，码不消耗。
- **Session 管理** —— 导入 / 导出 ChatGPT 账号会话，access_token 过期自动用 session cookie 刷新。
- **账单与财务** —— Team 卡片上的「查看账单」打开已同步的 Stripe 账单；「财务」页汇总各队订阅、折扣、账单和汇率换算，支持标准与 Premium 的真实价格及月付／年付。金额与优惠的计算方式见[上手指南 §6](docs/getting-started.md#6-钱都花在哪了)。
- **巡检与告警** —— 定时巡检 Team 健康，异常通过 Telegram 告警；续费前 3 天内还有没人用的计费席位（ChatGPT / Premium）会提醒一次，卡片上同时出现「续费前可减 N 席」（只提醒，不改账单）。巡逻会按规则自动移除绕过 TeamBoss 加入的外部成员：ChatGPT 席位超员时移除多出来的那几个；占用 Premium 席位的外部成员不论是否超员都会移除（**Beta**）；陌生的待接受邀请会被撤销。Owner、开启时已在队里的人、TeamBoss 邀请过或凭兑换码加入的人受保护，开了 Codex 或设了豁免的 Team 不踢人，完整规则见上手指南 [4.2](docs/getting-started.md#42-巡逻自动踢人--它会真的把人移出你的工作区)。巡逻**出厂是空跑演练**，要在后台「TG 与巡逻」页手动激活后才会真的移除人。
- **Telegram 机器人** —— Team 状态与异常通知 + 常用命令（邀请 / 移除 / 查成员 / 查自己的到期等；管理命令仅限已配对管理员）。**默认关闭**，需填入 Bot Token 后在后台开启。
- **操作日志** —— 成员操作、Team 管理（含超员策略修改）、系统设置、代理、Telegram 配置、财务设置、巡检自动移除开关等写操作均留痕可查；密钥类字段只记掩码，不落明文。
- **离线演示** —— `npm run demo` 不需要后端和 ChatGPT 账号，用浏览器内存里的虚构数据把整个后台跑起来，见下文「本地开发」。

## 界面

用户管理：列出所有 Team 的成员和待接受邀请，一行看完所属 Team、状态、到期、席位类型和 Telegram 绑定。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/users-dark.png">
  <img alt="用户管理：成员表，每行显示成员、所属 Team / Owner、状态、加入时间、到期、按颜色区分的席位类型和 TG 绑定" src="docs/images/users.png">
</picture>

查看账单：Team 卡片付款卡那一行的「查看账单」图标，打开这个 Team 已同步的 Stripe 账单，只读。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/team-billing-dark.png">
  <img alt="账单对话框：顶部是累计实付、近 30 天实付、最新一期三个汇总，下方是逐期账单表，列出账期、状态、应付、实付、说明和跳到 Stripe 发票的链接" src="docs/images/team-billing.png">
</picture>

财务总览：各队订阅、折扣、余额按基准币种汇总，带支出趋势；月付和年付分别按月均展示，并列出年付全年金额及整期续费金额。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/finance-dark.png">
  <img alt="财务总览：顶部是月预计支出、折扣共省、30 天内续费、预警四个指标卡，下方是可切换时间范围的支出趋势折线图" src="docs/images/finance.png">
</picture>

一次性兑换码：发给成员自助加入 / 续期，分 ChatGPT 码和 Premium 码，完整兑换码只在生成时显示一次。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/access-tokens-dark.png">
  <img alt="兑换码页面：兑换码列表，每行显示兑换码前缀、席位类型、授予时长、状态（未使用 / 已使用 / 已过期 / 已停用）、备注、兑换截止和兑换时间" src="docs/images/access-tokens.png">
</picture>

巡逻自动踢人：默认关闭，可以先「演练空跑」算一遍会踢谁（名单直接列在按钮下面）；开启前会把当前成员一次性豁免。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/patrol-dark.png">
  <img alt="TG 与巡逻页面的「巡逻」标签页：自动踢人开关和状态、「演练空跑」按钮，以及按 Team 排列、可点击切换豁免的芯片，芯片用颜色区分已豁免、观察和超员风险" src="docs/images/patrol.png">
</picture>

成员自助页（部署地址的根路径）：填邮箱 + 兑换码即可加入或续期，也能只填邮箱查自己的到期。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/self-service-dark.png">
  <img alt="成员自助页的查询标签页：填入邮箱后显示加入状态、所属 Team 和到期时间" src="docs/images/self-service.png">
</picture>

> 以上截图来自离线演示模式（`npm run demo`）里的虚构数据，Team 名、邮箱、金额、卡号均为编造。

## 技术栈

FastAPI + SQLite（后端）· React + Vite（前端）· Nginx（静态托管 + API 反代）· Docker Compose（推荐部署方式）。

## 部署

### Docker Compose

如需自托管，可用 Docker Compose 自行部署。需要 Docker Engine + **Compose v2.24 及以上**（`compose.yaml` 用了 `env_file` 的 `path` / `required` 长语法，更早的 v2 会直接解析失败）。

1. 准备配置：

   ```bash
   cp .env.example .env
   ```

   **务必**把 `AUTO_TEAM_ADMIN_PASSWORD`（≥8 位）和 `AUTO_TEAM_API_KEY`（**必须 `atk_` 开头且 ≥12 位**）改成随机强值——占位符和不合规的值会让后端首次启动直接报错退出。它们只在首次初始化数据库时写入，之后在后台里改。可选 `AUTO_TEAM_PORT` 改对外端口（默认 8080）。

2. 构建并后台启动：

   ```bash
   docker compose up -d --build
   ```

3. 在服务器本机验证起来了：

   ```bash
   docker compose ps
   curl -fsS http://127.0.0.1:8080/api/health
   ```

4. 打开后台 `http://127.0.0.1:8080/admin`，先在条款确认页逐条勾选（内容即上面的「免责声明」），再用 `.env` 里的管理员密码登录。

   > 后台入口是 **`/admin`**。根路径 `/` 是给成员用的自助兑换页（填邮箱 + 兑换码），不是登录页。

5. 登进去是一个空的 Dashboard。接下来怎么把第一个 Team 接进来、session JSON 从哪里取、哪些自动化默认是关的，见 **[上手指南](docs/getting-started.md)**。

**默认只监听 `127.0.0.1`，外网访问不到——这是故意的。** 整条链路是明文 HTTP，直接挂公网等于把管理员密码、API Key 和 ChatGPT 会话裸奔。对外访问请在宿主机上用 Nginx / Caddy 配好 TLS 证书，再反代到 `127.0.0.1:8080`。

外层反代必须设置 `proxy_set_header X-Real-IP $remote_addr;`（Caddy 等写等价配置），用真实连接来源**覆盖**访客自己带的 `X-Real-IP`：后端优先采信这个头（且只在连接来自 `AUTO_TEAM_TRUSTED_PROXIES` 时才采信），用它做限流和登录锁定，原样透传访客的值等于让人自报 IP。

如果本容器要藏在另一层反代后面（比如上面说的宿主机 Nginx/Caddy），还需要改 `docker/nginx.conf`：把 `set_real_ip_from` / `real_ip_header` 两行取消注释并填上那层反代的来源网段（不是随手抄一个 CIDR——必须覆盖那层反代实际的连接来源地址）。不做这一步，后端看到的每一个访客都会是**同一个**网桥网关地址（Docker 用户态转发 docker-proxy 的结果），分不出谁是谁。后端能识别这种“访客身份坍缩”并打一条 warning 日志；此时它不按访客锁定——否则任何人发 5 次错误密码就能把所有人连同管理员一起锁在外面——而是对全站做递增冷却：连续答错 5 次后进入冷却，从 1 分钟开始逐次翻倍、封顶 15 分钟，登录成功即清零。这能把可被爆破的次数压到每天百次以内，但冷却是全站共享的：有人持续乱试时，管理员重新登录也要等。要按访客精确锁定，仍然得把 `set_real_ip_from` 配对，`docker/nginx.conf` 里有详细说明。

**宿主机防火墙（UFW/firewalld）挡不住这个端口**——Docker 发布端口是自己在 iptables 里加规则，绕过宿主机防火墙的过滤链，“防火墙兜底”是假安全感。`AUTO_TEAM_BIND=0.0.0.0` 只应该在可信的内网环境里使用；需要公网访问时，请在前面加一层反代终结 TLS 并回源到 `127.0.0.1`。

**后端必须单进程运行，不要加 `--workers`。** 定时任务（同步、巡检、自动踢人）**和 Telegram 机器人的轮询线程**都是在 FastAPI 的 lifespan 里随进程启动的，开 N 个 worker 就是 N 份并发跑的同一套定时任务、N 个抢同一个 bot token 的长轮询（Telegram 侧会 409 互踢，命令还会被重复执行）——不是限流精度打折扣，是真的会并发抢同一批 Team 做重复的邀请/踢人操作。自带的 `docker compose up` 和 `run_server.py` 都不加 `--workers`，照抄部署命令不会踩这个坑；自己另起进程管理器（gunicorn / systemd 多实例等）时不要加这个参数。

维护：

```bash
docker compose logs -f
git pull && docker compose up -d --build   # 升级
docker compose down                        # 停止（不删数据）
```

业务数据（SQLite、会话、备份）存在名为 `auto_team_data` 的 Docker 命名卷里；`docker compose down` 不会删，只有 `down -v` 或手动删卷才会清。可用 `AUTO_TEAM_DATA_VOLUME` 换卷名，便于并行测试或迁移。

### 访问 chatgpt.com（curl_cffi 浏览器指纹）

chatgpt.com 前面的 Cloudflare 会看 TLS 指纹：Python `requests` 的握手哪怕带着 Chrome 的 User-Agent，也会在订阅、席位、邀请和会话刷新这些接口上吃到 HTML 403（"Unable to load site"）。所以后端发往 chatgpt.com 的**所有**请求（包括会话刷新和代理的「测试连接」）都走 curl_cffi，模拟 Chrome 浏览器的 TLS / HTTP2 指纹。部署时要知道的：

- **不用配，也配不了。** 模拟目标在代码里写死为 `chrome`（`backend/app/chatgpt_client.py` 里的 `IMPERSONATE`，即所装 curl_cffi 版本自带的最新 Chrome 配置），没有对应的环境变量或后台设置。
- **它带二进制组件。** `backend/requirements.txt` 固定了 `curl_cffi==0.16.3`，这个包自带编译好的 libcurl。Docker 镜像（`python:3.12-slim`）里 `pip install` 就够了，不需要额外装系统包；裸机部署时如果你的平台装不上这个包，改用 Docker Compose。
- **指纹解决不了 IP 的问题。** 服务器 IP 本身被风控时照样会 403 或弹人机验证页，这时要给 Team 挂代理，见上手指南 [2.4](docs/getting-started.md#24-需要走代理时)。Cloudflare 的放行规则不归本项目控制，哪天不再认这个指纹，所有 Team 会一起开始报 403——这也是「免责声明」里说功能可能随时失效的原因之一。
- 只有发往 chatgpt.com 的请求走 curl_cffi 和 Team 代理。Telegram 机器人（`api.telegram.org`）和汇率接口（`open.er-api.com`）用普通 `requests` **直连**，不走任何代理，服务器需要能直接访问它们（汇率取不到时会退回内置的静态汇率表）。

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

使用 `scripts/restore.sh` 恢复备份。恢复时必须停止后端写入；脚本会先校验数据库完整性或归档路径，再原子替换现有数据。脚本默认检查 `auto-team.service` 是否在运行；服务名不同就用 `AUTO_TEAM_SERVICE=<服务名>` 指定，已确认停掉后端则加 `--skip-service-check` 跳过检查。

Docker Compose 部署（备份文件位于数据卷的 `/app/data/backups/`）：

```bash
docker compose stop backend
docker compose run --rm backend ./scripts/restore.sh \
  /app/data/backups/app-20260723T063420Z.db --skip-service-check
docker compose start backend
curl -fsS http://127.0.0.1:8080/api/health
```

不走 Docker、直接用进程管理器（systemd 等）跑后端时，把下面的 `<服务名>` 换成你自己的：

```bash
# 查看恢复用法
./scripts/restore.sh

# 恢复数据库备份
sudo systemctl stop <服务名>
./scripts/restore.sh <项目目录>/backend/data/backups/app-20260723T063420Z.db
sudo systemctl start <服务名>

# 恢复会话备份
sudo systemctl stop <服务名>
./scripts/restore.sh <项目目录>/backend/data/backups/sessions-20260723T063420Z.tar.gz
sudo systemctl start <服务名>
```

脚本会在替换前保留现有数据库或会话目录的恢复前副本。

## 配置项

环境变量、数据卷名沿用了项目早期的名字 `AUTO_TEAM_` / `auto_team_data`，改名会让现有部署的数据卷对不上，所以保留不动。

| 变量 | 说明 | 默认 |
|---|---|---|
| `AUTO_TEAM_ADMIN_PASSWORD` | 初始管理员密码（首启写库，≥8 位） | 必填 |
| `AUTO_TEAM_API_KEY` | 后端 API Key，**必须以 `atk_` 开头且 ≥12 位**（首启校验，不合规直接报错退出） | 必填 |
| `AUTO_TEAM_CORS_ORIGINS` | 允许的前端来源（逗号分隔） | 本地开发端口 |
| `AUTO_TEAM_TRUSTED_PROXIES` | 可信反向代理来源（逗号分隔 IP/CIDR）。只有来自这些地址的请求，其 `X-Real-IP`/`X-Forwarded-For` 才会被用作自助接口的限流身份；其余一律按直连 IP 计数，防止伪造请求头绕过限流。没有反代、端口直接暴露时设为空值 | 回环 + 内网私有段 |
| `AUTO_TEAM_PORT` | 对外 Web 端口 | `8080` |
| `AUTO_TEAM_BIND` | 对外 Web 端口的绑定地址，见上文「部署」 | `127.0.0.1` |
| `AUTO_TEAM_DATA_VOLUME` | 存放数据库 / 会话 / 备份的 Docker 命名卷名 | `auto_team_data` |
| `AUTO_TEAM_DATA_DIR` | 数据目录（裸机运行时用；镜像里已设成 `/app/data`） | `backend/data` |
| `AUTO_TEAM_BACKEND_HOST` / `AUTO_TEAM_BACKEND_PORT` | 后端监听地址与端口（**裸机运行必需**；Docker 里固定走容器内 8000，不读这两个值） | `127.0.0.1` / `18087` |
| `AUTO_TEAM_FRONTEND_PORT` / `AUTO_TEAM_FRONTEND_HOST` | `npm run dev` / `npm run demo` 的端口与绑定地址；Host 默认仅本机，设 `0.0.0.0` 才对外 | `5173` / `127.0.0.1` |
| `AUTO_TEAM_VERSION` | 带管理员凭据（`X-API-Key`）请求 `/api/health` 时返回的版本号（匿名请求不返回）；不设时回退到 `git rev-parse --short HEAD`，再回退到 `unknown`（镜像里没有 `.git` 时建议显式设置） | 空 |
| `VITE_API_BASE_URL` | 前端**构建期**变量，只影响本机 `npm run dev` / `npm run build`；Docker Compose 构建不读它（同源 `/api`，无需设置） | 空（`.env.example` 里给的是本机开发用的 `http://127.0.0.1:18087`） |

完整清单见 [`.env.example`](./.env.example)。

## 接口文档

- [`ADMIN_API.md`](./ADMIN_API.md) —— 一次性兑换码的管理方式：Telegram 指令出码、后台页面、以及写脚本时直接调的 HTTP 接口（需 `X-API-Key`）。
- 成员自助接口挂在 `/api/self-service` 下，无需认证，供根路径的自助页调用，也可以自己接前端：
  - `POST /api/self-service/redeem` —— 凭邮箱 + 兑换码加入或续期
  - `POST /api/self-service/query` —— 请求体字段是 `query`，填**邮箱或兑换码**均可，返回成员状态；兑换历史要另外用 `token` 出示一张这个邮箱自己用过的码，否则恒为空数组
  - `POST /api/self-service/status` —— 凭邮箱查当前是否还在 Team 里（自带的自助页没有调用它，留给自建前端）

  这三个接口按来源 IP 限流。挂在反代后面时务必配好 `AUTO_TEAM_TRUSTED_PROXIES`，否则所有访客会被算成同一个 IP。有效兑换码的兑换尝试还另有上限（每张码每小时 10 次、全站每 10 分钟 60 次），超出时返回 429，码不消耗；「请选择 Team」提示和服务端/上游故障（5xx）不占这张码的次数。

## 安全须知

- **必须放在 TLS / 反向代理 / 防火墙后面。** Docker Compose 部署默认只把 Web 端口绑到 `127.0.0.1:8080`，后端容器不发布任何宿主机端口；务必在宿主机配置 HTTPS 并限制访问来源后再对外。
- **会话和密钥在数据卷里是明文存储**（ChatGPT 账号会话、API Key、Telegram token）。这是这类工具的固有风险：任何拿到数据卷 / 备份的人即可完全接管被管理的工作区。**请严格限制数据目录权限**：
  - 数据目录（`backend/data/`）：权限 `700`（仅所有者可访问）
  - 数据库文件（`backend/data/app.db`）：权限 `600`（仅所有者可读写）
  - 会话文件（`backend/data/sessions/*.json`）：权限 `600`（仅所有者可读写）
  - 备份文件（`backend/data/backups/*`）：权限 `600`（仅所有者可读写）

  妥善保管备份文件，建议定期离线备份到安全位置。
- `.env`、数据库、会话文件、备份已被 git 和 Docker 构建上下文忽略，不会进仓库或镜像。
- 首次启动后请通过后台修改密码 / 轮换 API Key，不要长期使用 `.env` 里的初始值。
- 后台密码登录的防爆破（"来源" = IPv4 地址；IPv6 按 /64 计）：
  - 同一来源连续输错 5 次锁 15 分钟。
  - 全站失败预算：每个来源每轮最多算 5 次失败，全站算满 10 次（即至少 3 个来源在试）后，每次再错都触发冷却（1 分钟起翻倍，封顶 15 分钟），冷却期间陌生来源的密码登录一律要求稍后再试。距上一次计入预算的失败满 1 小时，这一轮清零。不管攻击者手里有多少地址，能试的密码总数都在每天几百次以内。
  - 最近 90 天内用密码登录成功过的来源不受全站冷却影响，只受上面按来源的锁定。名单存在数据库表 `admin_login_trusted_sources`（最多保留最近 50 个），陌生人只能把**没登录过的新设备 / 新网络**挡在冷却外面。
  - 反代把所有访客坍缩成同一个地址时（见上文"部署"），以上按来源的区分都不成立，只剩站点级递增冷却；请先把反代配对。
- **被挡在后台外面时怎么进去**（典型情况：换了新设备或新网络，同时有人从多个地址乱试密码）：
  1. 已经登录过的浏览器里存着 API Key，不走密码登录，照常可用。
  2. 在服务器上取出当前 API Key，粘到新设备的浏览器里：

     ```bash
     # Docker Compose 部署（数据在 auto_team_data 卷，容器内是 /app/data）
     docker compose exec backend python -c "import sqlite3; print(sqlite3.connect('/app/data/app.db').execute(\"SELECT value FROM settings WHERE key='admin_api_key'\").fetchone()[0])"
     # 直接运行的后端
     sqlite3 backend/data/app.db "SELECT value FROM settings WHERE key='admin_api_key'"
     ```

     在新设备上打开 `/admin`，在浏览器开发者工具的控制台执行 `localStorage.setItem('auto_team_admin_api_key', '<上面取到的 key>')`，刷新页面即可进入后台。之后在这台设备上用密码登录成功一次，它所在的网络就进了上面的名单。
  3. 重启后端会清空内存里的按来源锁定、全站冷却和请求限流（数据库里的已登录来源名单保留）。攻击还在继续时冷却很快会回来，所以优先用第 2 步。

## 本地开发

```bash
# 后端（Python 3.12）
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example ../.env        # 填好 AUTO_TEAM_ADMIN_PASSWORD / AUTO_TEAM_API_KEY
python run_server.py              # 读取仓库根目录 .env，默认监听 127.0.0.1:18087

# 前端（另开终端）
cd frontend
npm ci
npm run dev                       # http://localhost:5173
```

没有后端、也没有 ChatGPT 账号时，可以起一个离线演示：

```bash
cd frontend
npm ci
npm run demo                      # 即 vite --mode demo，打开 http://localhost:5173/admin
```

演示模式下 Vite 不配置 `/api` 代理，前端启动时把浏览器的 `fetch` 换成一个内存里的假后端：所有 `/api/*` 请求（哪怕设置了 `VITE_API_BASE_URL`）都由浏览器里的虚构数据应答，**一条都不会发到真后端，也不会碰到 ChatGPT**，所以不需要后端在跑，也不需要 `.env`。任意密码都能登录，所有改动只存在内存里，刷新即重置。演示数据覆盖了三种超员策略、月付、年付无优惠、真实 Premium 价格和未知价格，以及会话失效、超员、多币种账单等状态，适合先看界面或截图（上面的截图就来自这里）；邮箱、卡号、金额全部是编造的。这段演示代码只在 `demo` 模式下加载，`npm run build` 出来的正式构建里没有它。

`npm run dev` 起的 Vite 开发服务器会把 `/api` 请求代理到后端：目标地址优先取 `VITE_API_BASE_URL`，否则用 `http://AUTO_TEAM_BACKEND_HOST:AUTO_TEAM_BACKEND_PORT`（默认 `127.0.0.1:18087`），这些变量都从仓库根目录的 `.env` 读取。前端端口可用 `AUTO_TEAM_FRONTEND_PORT` 改。开发服务器默认只绑定 `127.0.0.1`；确需从其他机器访问时设置 `AUTO_TEAM_FRONTEND_HOST`（例如 `0.0.0.0`），注意这会把开发服务器暴露出去。

后端测试：**务必用 `AUTO_TEAM_DATA_DIR` 指向一个临时目录**，否则数据目录会回退到 `backend/data/`，在已部署的机器上那就是真实数据库。测试包自带保护（未设置该变量时会自动改用临时目录），但显式指定更稳妥：

```bash
cd backend
python -m pip install -r requirements-test.txt
AUTO_TEAM_DATA_DIR=$(mktemp -d) python -m unittest discover -s tests
```

提交 PR 前请确认后端测试通过、`cd frontend && npm run build` 通过。CI 跑的就是这两项。漏洞请按 [SECURITY.md](./SECURITY.md) 私下报告，不要开公开 issue。

## 成熟度

目前只有作者自己在生产环境跑，没有经过大范围真实使用的验证。涉及邀请、移除、到期、兑换的自动化都会直接改动你的工作区成员，超员策略允许时还会让 ChatGPT 加购席位并扣费，接入前请先用一个不重要的 Team 跑一遍，确认行为符合预期再接生产。

Premium 席位（内部值 `prolite`）是 Beta，生产操作的验证范围见[顶部红色提示](#production-test-scope)。真实工作区已验证价格读取和官方加购报价查询，查询前后已付席位数未变；查询成功不证明实际加购、扣款或成员变更成功。Premium 邀请、切换、兑换和巡逻移除仍需先在一个不重要的 Team 上验证；金额说明见[上手指南 §6](docs/getting-started.md#6-钱都花在哪了)。

TeamBoss 依赖的是 ChatGPT 的私有接口，任何一项功能都可能因为官方改动而突然失效，见「免责声明」。

## 许可证

[MIT](./LICENSE)。
