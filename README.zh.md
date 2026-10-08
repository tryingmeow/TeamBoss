# TeamBoss

[English](README.md) | **简体中文**

[![CI](https://github.com/tryingmeow/TeamBoss/actions/workflows/ci.yml/badge.svg)](https://github.com/tryingmeow/TeamBoss/actions/workflows/ci.yml)

**一个自托管后台，集中管理多个 ChatGPT Team / Business 工作区：一键导入、成员管理、定时人员变动核对、账单统计、到期自动移除与巡逻自动踢人。**

适合已经拥有工作区、需要批量邀请成员和管理使用期限的管理员。部署前需准备自己的 **Owner 账号与已购买席位的工作区**。

**关键词：** ChatGPT Team / Business 管理 · 自托管后台 · 一键导入 Session · 多工作区管理 · 成员管理 · 批量邀请 · 席位分配 · ChatGPT / Codex / Premium 席位 · 定时同步 · 人员变动核对 · 到期管理 · 到期自动踢人 · 巡逻自动踢人 · 演练预览 · Team 豁免 · 超员策略 · 兑换码管理 · 自助续期 · 账单统计 · 发票记录 · 多币种费用 · 续费提醒 · Telegram 机器人 · 独立代理 · 自动刷新 Token · 备份恢复 · Docker Compose 部署 · 管理 API。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/dashboard-dark.png">
  <img alt="多 Team 总览：查看各工作区的席位占用、订阅周期、费用与续费状态" src="docs/images/dashboard.png">
</picture>

截图使用虚构演示数据。 [查看更多界面](docs/screenshots.md) · [运行离线演示](docs/development.md)

## 能做什么

- **一键导入工作区**：粘贴 Owner 账号完整 Session JSON 即可接入；重新导入保留成员期限、备注与管理记录，会话有效期间自动刷新访问 Token。
- **多 Team 与席位总览**：集中查看各工作区的订阅、空位、ChatGPT / Codex / Premium 席位占用与续费时间。
- **成员管理与批量邀请**：单人或批量邀请，按空位分配批量 ChatGPT 邀请，管理待接受邀请、切换席位类型和移除成员。
- **席位类型与超员策略**：管理 ChatGPT、Codex 和 Premium（Beta）席位，设置默认邀请席位；付费席位不足时可选择禁止超员、确认后加购或自动加购。
- **定时同步与人员变动核对**：定期同步成员、待接受邀请和席位占用，持续核对邀请与移除是否已生效。
- **到期管理与自动移除**：设置或延长使用期限，发送续费提醒，并按设置自动移除到期成员。
- **巡逻自动踢人**：启用后按规则移除外部成员、撤销陌生邀请；支持演练预览和 Team 豁免，确认规则后再执行。
- **账单统计与发票**：查看预计月支出、累计与近期实付、发票状态和续费费用，按 Team 或付款卡汇总并对照不同币种。
- **兑换码与成员自助**：设置一次性兑换码的授予时长和兑换有效期，成员自行加入、续期和查询到期时间。兑换只使用已购席位，不会自动加购。
- **Telegram 机器人与操作记录**：接收状态、异常与续费提醒，通过机器人命令执行管理操作，并在后台查看操作日志。
- **独立代理与自托管运维**：按 Team 配置代理，使用 Docker Compose 部署，通过自带脚本备份恢复数据库和会话，并用管理 API 对接兑换码管理。

<a id="production-test-scope"></a>

> [!CAUTION]
> **生产测试范围**
>
> 目前仅对已购买的月付 ChatGPT Standard 席位做过生产测试。Premium、年付及超出已购席位的邀请尚未经过生产测试，不保证费用准确或操作成功。

## 快速开始

需要 **Docker Engine 和 Docker Compose ≥ 2.24**。以下命令在部署主机上执行。

### 1. 下载并准备配置

```bash
git clone https://github.com/tryingmeow/TeamBoss.git
cd TeamBoss
cp .env.example .env
```

编辑 `.env`，设置以下两项：

- `AUTO_TEAM_ADMIN_PASSWORD`：随机强密码，至少 **8 位**。
- `AUTO_TEAM_API_KEY`：随机密钥，必须以 **`atk_` 开头且总长至少 12 位**。

保留占位符或填写不合规的值会导致首次启动失败。这两项仅在首次初始化数据库时写入，之后通过后台修改。其他配置见 [`.env.example`](.env.example)。

### 2. 启动并检查

```bash
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8080/api/health
```

默认端口为 `8080`；若修改了 `AUTO_TEAM_PORT`，请同步替换检查和访问地址中的端口。

### 3. 打开后台，接入工作区

在**部署主机本机浏览器**打开 <http://127.0.0.1:8080/admin>，阅读并确认使用条款，再用刚设置的管理员密码登录。

- **`/admin` 是管理后台**。
- **`/` 是成员自助页**，用于兑换、续期和查询。

默认仅监听本机回环地址。需要从其他设备访问或向成员开放时，先按[部署指南](docs/deployment.md)配置 **HTTPS 反向代理**。

后台初始为空。接下来按[接入第一个 Team](docs/getting-started.md#2-接入第一个-team)导入 Owner 会话；指南也介绍了会话的获取方式、首次邀请和兑换流程。

## 文档

| 想做什么 | 去哪里 |
| --- | --- |
| 接入 Team、邀请成员、发兑换码、设置到期与巡逻 | [上手指南](docs/getting-started.md) |
| 配置 HTTPS、代理、安全、升级与备份恢复 | [部署与运维](docs/deployment.md) |
| 先看界面 | [截图](docs/screenshots.md) · [离线演示与本地开发](docs/development.md) |
| 对接兑换码管理接口 | [管理 API](ADMIN_API.md) |
| 报告安全漏洞 | [安全政策](SECURITY.md) |

技术栈：FastAPI + SQLite · React + Vite · Nginx · Docker Compose。

<a id="免责声明"></a>

## 使用须知

- **接口依赖**：使用 ChatGPT 网页端未公开的非官方接口，上游变更可能使功能失效；以这种方式管理工作区可能违反服务条款，存在工作区或账号受限、封禁的风险。
- **凭据保护**：Owner 会话与密钥以明文存储在本地数据卷和备份中，请妥善保护。
- **实际操作与费用**：到期清理会移除成员；巡逻默认仅演练，激活后会实际改变成员或邀请。管理员邀请、切换席位时，若超员策略允许，可能触发加购和扣费。请核对规则与目标名单，费用以官方账单为准。

本项目与 OpenAI 无隶属或合作关系。使用者自行承担运行风险，开发者和贡献者不承担由此造成的损失；部署前请阅读[完整免责声明](docs/disclaimer.md)。

## 许可证

[MIT](LICENSE)
