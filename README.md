# TeamBoss

**English** | [简体中文](README.zh.md)

[![CI](https://github.com/tryingmeow/TeamBoss/actions/workflows/ci.yml/badge.svg)](https://github.com/tryingmeow/TeamBoss/actions/workflows/ci.yml)

**One self-hosted admin panel for one-click workspace import, member management, scheduled member sync, billing statistics, expiry auto-removal and automatic patrol across multiple ChatGPT Team / Business workspaces.**

TeamBoss is for admins who already run workspaces and need to invite members in bulk and manage how long each member keeps access. Before deploying, you need your own **Owner account and a workspace with purchased seats**.

**Keywords:** ChatGPT Team / ChatGPT Business management · self-hosted admin panel · one-click Session import · multi-workspace management · member management · bulk invitations · seat allocation · ChatGPT / Codex / Premium seats · scheduled member sync · member-change tracking · expiry management · automatic member removal · automatic patrol · dry-run previews · Team exemptions · overage policies · redemption codes · self-service renewal · billing statistics · invoice history · multi-currency costs · renewal reminders · Telegram bot · per-Team proxies · automatic token refresh · backup and restore · Docker Compose · admin API.

> [!NOTE]
> The admin UI and the detailed guides under `docs/` are currently in Chinese. This README covers what TeamBoss does and how to get it running.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/dashboard-dark.png">
  <img alt="Multi-Team overview: seat usage, subscription period, cost and renewal status for each workspace" src="docs/images/dashboard.png">
</picture>

Screenshots use fictional demo data. [More screenshots](docs/screenshots.md) · [Run the offline demo](docs/development.md)

## Features

- **One-click workspace import**: paste the Owner account’s complete Session JSON to connect a workspace. Reimport a session without losing member expiry dates, remarks or management records; access tokens refresh automatically while the session remains valid.
- **Multi-Team and seat overview**: see workspace subscriptions, free seats, ChatGPT / Codex / Premium seat usage and upcoming renewals in one place.
- **Member management and bulk invites**: invite members individually or in bulk, distribute bulk ChatGPT invites across available Teams, manage pending invitations, switch seat types and remove members.
- **Seat types and overage policies**: manage ChatGPT, Codex and Premium (Beta) seats; set the default invitation seat to ChatGPT or Codex, and choose whether full paid seats block an invite, require purchase confirmation or allow automatic purchase.
- **Scheduled member-change tracking**: periodically sync members, pending invitations and seat usage, and check whether invitations and removals have taken effect.
- **Expiry management and automatic removal**: set or extend access periods, send renewal reminders and automatically remove expired members according to your settings.
- **Automatic patrol and member removal**: after activation, patrol can remove outside members and revoke unfamiliar invitations according to your rules. Preview changes in a dry run and exempt Teams before enabling enforcement.
- **Billing statistics and invoices**: review estimated monthly spending, cumulative and recent payments, invoice status and renewal costs; group billing by Team or payment card and compare currencies.
- **Redemption codes and member self-service**: issue one-time codes with an access duration and redemption deadline; members join, renew and query expiry dates themselves. Redemptions use purchased seats and never buy extra seats.
- **Telegram bot and audit logs**: receive status, incident and renewal reminders, use bot commands for administrative tasks, and review operation logs.
- **Per-Team proxies and self-hosted operations**: configure a separate proxy for each workspace, deploy with Docker Compose, back up and restore the database and sessions with the bundled scripts, and automate redemption-code management through the admin API.

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
