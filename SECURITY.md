# Security Policy

TeamBoss stores ChatGPT workspace credentials (session tokens, API keys, Telegram tokens) in its data directory, so a vulnerability can mean full takeover of someone's workspace. **Please never report a security issue in a public issue, discussion or pull request.**

## Reporting

Use GitHub's private reporting: open the repository's **Security** tab and click **Report a vulnerability** (a private security advisory visible only to the maintainer). Include the affected version or commit, reproduction steps and impact. Do not include real tokens, sessions or personal data in the report.

## In scope

- Authentication or authorization bypass in the admin API or the self-service endpoints
- Leaking or exposing stored credentials to unauthorized parties
- Injection, path traversal or remote code execution in the backend or Docker setup
- Flaws in rate limiting, redemption or expiry logic that let someone gain or keep seats they should not have

## Out of scope

- Behaviors documented as deliberate in [the deployment security notes](docs/deployment.md#数据与凭据安全) (plaintext storage of credentials in the data volume, admin settings returning secrets to the admin)
- Deployments exposed without TLS / reverse proxy / firewall against [the deployment guidance](docs/deployment.md#https-与反向代理)
- Changes or breakage on OpenAI's side (TeamBoss uses unofficial, undocumented endpoints)

This is a solo-maintained project; expect a best-effort response rather than a fixed SLA.
