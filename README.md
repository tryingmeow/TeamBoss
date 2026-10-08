# TeamBoss

**English** | [简体中文](README.zh.md)

[![CI](https://github.com/tryingmeow/TeamBoss/actions/workflows/ci.yml/badge.svg)](https://github.com/tryingmeow/TeamBoss/actions/workflows/ci.yml)

**One self-hosted admin panel for seats, members, expiry dates and billing across multiple ChatGPT Team / Business workspaces.**

TeamBoss is for admins who already run workspaces and need to invite members in bulk and manage how long each member keeps access. Before deploying, you need your own **Owner account and a workspace with purchased seats**.

> [!NOTE]
> The admin UI and the detailed guides under `docs/` are currently in Chinese. This README covers what TeamBoss does and how to get it running.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/dashboard-dark.png">
  <img alt="Multi-Team overview: seat usage, subscription period, cost and renewal status for each workspace" src="docs/images/dashboard.png">
</picture>

Screenshots use fictional demo data. [More screenshots](docs/screenshots.md) · [Run the offline demo](docs/development.md)

## Features

- **Multi-Team overview**: seat usage, members, subscriptions and billing in one place, so you can see free seats and upcoming renewals at a glance.
- **Bulk invites**: paste a list of emails and TeamBoss spreads them across the free ChatGPT seats in each Team. Individual members can be managed too.
- **Member self-service**: members join or renew with their email and a one-time redemption code, and can look up their own expiry date. Redemptions only use seats you have already paid for; they never buy extra seats.
- **Expiry management**: give members an access period and remove them automatically when it ends. Patrol can clean up members who joined outside TeamBoss, following rules you set.
- **Notifications and audit log**: get status and incident alerts in Telegram, and review every operation in the admin log.

<a id="production-test-scope"></a>

> [!CAUTION]
> **Production test scope**
>
> Only purchased, monthly ChatGPT Standard seats have been tested in production. Premium seats, annual plans and invites beyond your purchased seats have not been tested in production; billing accuracy and success are not guaranteed for them.

## Quick start

Requires **Docker Engine and Docker Compose ≥ 2.24**. Run the following on the host you deploy to.

### 1. Download and configure

```bash
git clone https://github.com/tryingmeow/TeamBoss.git
cd TeamBoss
cp .env.example .env
```

Edit `.env` and set:

- `AUTO_TEAM_ADMIN_PASSWORD`: a strong random password, at least **8 characters**.
- `AUTO_TEAM_API_KEY`: a random key that **starts with `atk_` and is at least 12 characters long**.

The first start fails if either value is left as the placeholder or does not meet these rules. Both are only written when the database is first created; change them from the admin panel afterwards. All other settings are documented in [`.env.example`](.env.example).

### 2. Start and check

```bash
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8080/api/health
```

The default port is `8080`. If you changed `AUTO_TEAM_PORT`, use that port here and in the URLs below.

### 3. Open the admin panel and connect a workspace

In a browser **on the deployment host**, open <http://127.0.0.1:8080/admin>, read and accept the terms, then log in with the admin password you just set.

- **`/admin` is the admin panel.**
- **`/` is the member self-service page** for redeeming codes, renewing and checking expiry.

TeamBoss only listens on loopback by default. To reach it from other devices or open it to members, first put an **HTTPS reverse proxy** in front of it as described in the [deployment guide](docs/deployment.md).

The admin panel starts empty. Next, follow [Connect your first Team](docs/getting-started.md#2-接入第一个-team) to import an Owner session; the guide also explains how to obtain the session, send your first invite and run your first redemption.

## Documentation

The guides below are written in Chinese.

| I want to… | Read |
| --- | --- |
| Connect Teams, invite members, issue codes, set expiry and patrol | [Getting started](docs/getting-started.md) |
| Set up HTTPS, proxies, security, upgrades, backup and restore | [Deployment and operations](docs/deployment.md) |
| See the UI first | [Screenshots](docs/screenshots.md) · [Offline demo and local development](docs/development.md) |
| Manage redemption codes over HTTP | [Admin API](ADMIN_API.md) |
| Report a security vulnerability | [Security policy](SECURITY.md) (English) |

Stack: FastAPI + SQLite · React + Vite · Nginx · Docker Compose.

<a id="disclaimer"></a>

## Before you use it

- **Unofficial endpoints**: TeamBoss uses undocumented endpoints of the ChatGPT web app. Upstream changes can break features at any time, and managing a workspace this way may violate the service provider's terms, which can lead to limits on or suspension of the workspace or account.
- **Credentials**: Owner sessions and keys are stored in plain text in the local data volume and in backups. Protect them accordingly.
- **Real changes and charges**: expiry cleanup removes members. Patrol only does dry runs until you turn it on; after that it really changes members and invites. When an admin invites someone or switches a seat, extra seats may be bought and charged if the overage policy allows it. Check your rules and target lists; the official ChatGPT billing is always authoritative.

TeamBoss is not affiliated with or endorsed by OpenAI. You run it at your own risk; the developers and contributors accept no liability for any resulting loss. Read the [full disclaimer](docs/disclaimer.md) (Chinese) before deploying.

## License

[MIT](LICENSE)
