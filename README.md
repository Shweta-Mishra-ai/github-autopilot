<div align="center">

# GitHub Autopilot

**A self-hosted GitHub App that brings AI automation to every repository.**

[![CI](https://github.com/Shweta-Mishra-ai/github-autopilot/actions/workflows/ci.yml/badge.svg)](https://github.com/Shweta-Mishra-ai/github-autopilot/actions)
[![Python](https://img.shields.io/badge/python-3.11+-3b82f6?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Flask](https://img.shields.io/badge/flask-3.x-000000?style=flat-square&logo=flask&logoColor=white)](https://flask.palletsprojects.com)
[![Redis](https://img.shields.io/badge/redis-backed-ef4444?style=flat-square&logo=redis&logoColor=white)](https://redis.io)
[![License](https://img.shields.io/badge/license-MIT-a855f7?style=flat-square)](LICENSE)
[![Ruff](https://img.shields.io/badge/linted-ruff-ef4444?style=flat-square)](https://docs.astral.sh/ruff)

[Live Server](https://github-autopilot-1.onrender.com) · [Install App](https://github.com/apps/ai-repo-manager) · [Documentation](docs/)

</div>

---

## Overview

GitHub Autopilot is a self-hosted GitHub App that installs in one click and acts as an AI co-pilot across all your repositories. It responds to GitHub events automatically and to slash commands posted in issue and PR comments.

**Key capabilities:**

- **Automated PR review** — rewrites vague titles, fills empty descriptions, rates code quality, identifies test coverage gaps, and maps blast radius across system layers
- **Issue triage** — assigns priority and complexity labels on open, generates targeted follow-up questions, estimates resolution time
- **Security scanning** — detects 35+ secret patterns with entropy gating, scans for CVEs, integrates with Dependabot and CodeQL APIs
- **26 slash commands** — from `/fix` and `/autofix` to `/rollback`, `/release`, and `/runtests`
- **Multi-provider AI** — Groq → Gemini → OpenRouter fallback chain with per-provider circuit breakers and hallucination detection

---

## Free Tier Constraints

This project is designed to run at zero cost. The following limits apply on the default free-tier deployment:

| Resource | Limit |
|----------|-------|
| Concurrent webhook workers | 6 |
| Groq API requests | 14,400 / day |
| Redis storage | 25 MB |
| AI calls per repository | 150 / day (configurable) |
| Render free tier sleep | After 15 min of inactivity |

---

## Quick Start

**Prerequisites:** Python 3.11+, Redis, a Groq API key, a GitHub App.

```bash
# 1. Clone the repository
git clone https://github.com/Shweta-Mishra-ai/github-autopilot.git
cd github-autopilot

# 2. Create a virtual environment and install dependencies
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 3. Configure environment variables
cp .env.example .env
# Edit .env and fill in: GITHUB_APP_ID, GITHUB_PRIVATE_KEY,
# GITHUB_WEBHOOK_SECRET, GROQ_API_KEY, REDIS_URL

# 4. Start the server
flask --app server run --port 5000

# 5. Run the test suite
python -m pytest -v
```

For full installation instructions including GitHub App creation and webhook configuration, see the [User Setup Guide](docs/guides/user-setup.md).

---

## Slash Commands

Post any command as a comment on a GitHub issue or PR to invoke it.

> **Permission requirements:** Commands marked 🔐 require **write**, **maintain**, or **admin** access on the repository. All users are rate-limited to 10 commands per hour per repository.

### Code Quality

| Command | Description |
|---------|-------------|
| `/fix` | Root cause analysis with a production-ready fix suggestion and a verification test |
| `/autofix` 🔐 | Creates a fix branch, commits the change, and posts a diff for review |
| `/apply` 🔐 | Opens a pull request from an autofix branch after you have reviewed the diff |
| `/improve` | Scored improvement suggestions across performance, security, and readability |
| `/refactor` | Structural refactor recommendations with before/after code examples |
| `/perf` | Time complexity analysis, N+1 query detection, and optimization suggestions |

### Code Understanding

| Command | Description |
|---------|-------------|
| `/explain` | Plain-English explanation: what the code does, how it works, why it exists, and common pitfalls |
| `/summarize` | Condenses a long PR or issue discussion into a concise summary |
| `/arch` | Architecture review highlighting coupling issues, layer violations, and god classes |
| `/impact` | Blast radius map showing which system layers a PR touches |
| `/gaps` | Test coverage gap analysis with risk-rated suggestions |
| `/ci` | CI failure root cause analysis with concrete fix steps |

### Documentation and Releases

| Command | Description |
|---------|-------------|
| `/docs` | Generates docstrings and a README section for the changed code |
| `/test` | Generates a pytest test suite for the changed code |
| `/changelog` | Produces a Keep a Changelog entry from commit history |
| `/release` 🔐 | Creates a GitHub draft release with AI-generated release notes |
| `/version` | Shows tag history and semantic versioning status |
| `/runtests` 🔐 | Triggers a GitHub Actions workflow via `workflow_dispatch` |

### Security and Health

| Command | Description |
|---------|-------------|
| `/security` | Scans the PR diff for exposed secrets and vulnerable dependencies |
| `/secfull` 🔐 | Full security report: Dependabot alerts, CodeQL findings, and Secret Scanning results |
| `/health` | Repository health grade (A–F) with ranked improvement recommendations |

### Operations

| Command | Description |
|---------|-------------|
| `/merge` 🔐 | Merges the PR after all guardrails pass: CI green, required reviews, no conflicts |
| `/rollback` 🔐 | Lists available snapshots or restores repository state (requires two-step confirmation) |
| `/report` | Weekly analytics summary: PR velocity, issue resolution time, and code quality grade |
| `/budget` | Live LLM token usage and cost breakdown per provider |
| `/notify` | Sends an issue or PR alert to Discord or Slack with color-coded severity |

---

## Architecture

```
Incoming GitHub Webhook (POST /webhook)
            │
            ▼
    Security Pipeline
    ┌─────────────────────────────────────────┐
    │  ① HMAC-SHA256 signature verification   │
    │  ② IP-based rate limiting (100 req/min) │
    │  ③ Replay protection (Redis SET NX)     │
    │  ④ Bot loop detection                   │
    │  ⑤ ACK 202 immediately                  │
    └─────────────────────┬───────────────────┘
                          │
                          ▼
            Thread Pool (6 workers, 50-job cap)
            ┌─────────┬──────────┬────────────┐
            │  PR     │  Issues  │  Comments  │  Push
            │ review  │  triage  │  commands  │  scan
            └────┬────┴────┬─────┴─────┬──────┘
                 └─────────┴───────────┘
                           │
                           ▼
                       AI Router
              ┌──────────────────────────┐
              │  Groq 70B  (primary)     │
              │  Groq 8B   (fast tasks)  │
              │  Gemini    (long context)│
              │  OpenRouter (fallback)   │
              │                          │
              │  Circuit breakers        │
              │  Hallucination detection │
              │  Cost tracking           │
              └────────────┬─────────────┘
                           │
                           ▼
                    Post to GitHub
```

---

## Security Model

| Threat | Defence |
|--------|---------|
| Forged webhooks | HMAC-SHA256 verification; application refuses to start without `GITHUB_WEBHOOK_SECRET` |
| Replay attacks | SHA-256 event fingerprint stored in Redis with SET NX; 1-hour TTL |
| Webhook flood | Per-IP rate limit (100 req/min, Redis sliding window); bounded thread pool |
| Privilege escalation | GitHub collaborator API permission check before every restricted command |
| Prompt injection | Input sanitisation, blocklist patterns, 8,000-character limit |
| Secret exposure | 35+ regex patterns with entropy gating; no scannable literals in scanner source |
| Bot feedback loops | `sender.type` and `[bot]` suffix detection; own-app login blocklist |
| Command abuse | 10 commands per user per hour; 150 AI calls per repository per day |

Full threat model → [docs/security/threat-model.md](docs/security/threat-model.md)

---

## Environment Variables

| Variable | Required | Description |
|----------|:--------:|-------------|
| `GITHUB_APP_ID` | ✅ | Numeric App ID from GitHub App settings |
| `GITHUB_PRIVATE_KEY` | ✅ | Full RSA private key in PEM format, including headers |
| `GITHUB_WEBHOOK_SECRET` | ✅ | The server will not start without this value |
| `GROQ_API_KEY` | ✅ | Primary LLM provider — [console.groq.com](https://console.groq.com) (free tier available) |
| `REDIS_URL` | ✅ | Redis connection string — Render provides this automatically |
| `GEMINI_API_KEY` | ⚡ | Gemini Flash fallback — [aistudio.google.com](https://aistudio.google.com) |
| `OPENROUTER_API_KEY` | ⚡ | Emergency fallback — [openrouter.ai](https://openrouter.ai) |
| `DISCORD_WEBHOOK_URL` | 📢 | Discord notifications via `/notify` |
| `SLACK_WEBHOOK_URL` | 📢 | Slack notifications via `/notify` |
| `METRICS_AUTH_TOKEN` | 🔒 | Bearer token for the `/health` detail endpoint |
| `MAX_DISPATCH_WORKERS` | ⚙️ | Thread pool size (default: `6`) |
| `REPO_DAILY_AI_LIMIT` | ⚙️ | Maximum AI calls per repository per day (default: `150`) |

> ✅ Required &nbsp;·&nbsp; ⚡ Recommended &nbsp;·&nbsp; 📢 Optional &nbsp;·&nbsp; 🔒 Security &nbsp;·&nbsp; ⚙️ Tuning

---

## Project Structure

```
github-autopilot/
├── server.py                    # Application entry point — security pipeline and dispatch
├── .env.example                 # Environment variable reference
├── .ai-repo-manager.yml         # Per-repository bot configuration
│
├── app/
│   ├── ai/
│   │   ├── router.py            # Multi-provider LLM router with task classification
│   │   ├── circuit_breaker.py   # Per-provider circuit breakers (CLOSED/OPEN/HALF_OPEN)
│   │   ├── hallucination.py     # Response confidence scoring
│   │   └── providers/           # Groq, Gemini, OpenRouter provider implementations
│   │
│   ├── core/
│   │   ├── webhook_security.py  # Full webhook verification pipeline
│   │   ├── authorization.py     # Command permission enforcement
│   │   ├── thread_pool.py       # Bounded ThreadPoolExecutor wrapper
│   │   ├── idempotency.py       # SHA-256 event deduplication via Redis
│   │   ├── analytics.py         # Usage tracking and /report data
│   │   └── snapshot.py          # Repository snapshots for /rollback
│   │
│   ├── github/
│   │   ├── auth.py              # JWT generation and installation token exchange
│   │   ├── client.py            # GitHub REST API client with retry logic
│   │   ├── helpers.py           # Shared utilities (error formatting)
│   │   └── notifications.py     # Discord and Slack embed builder
│   │
│   ├── handlers/
│   │   ├── comments.py          # Slash command dispatcher (26 commands)
│   │   ├── autofix.py           # Automated fix engine: diff → branch → PR
│   │   ├── pull_request.py      # PR analysis, blast radius, and review
│   │   ├── issues.py            # Issue triage, labelling, and welcome messages
│   │   ├── push.py              # Secret scanning and dependency checks on push
│   │   └── ci.py                # CI failure analysis
│   │
│   └── security/
│       ├── enhanced_secrets.py  # 35+ secret patterns with entropy gating
│       ├── dependencies.py      # CVE vulnerability scanner
│       └── scanner.py           # Dependabot and CodeQL API integration
│
├── docs/                        # Technical documentation
├── tests/                       # Test suite — all tests run without network access
└── archive/                     # Inactive code retained for reference
```

---

## Deployment

Deploy to Render in approximately 10 minutes:

```bash
# 1. Push this repository to GitHub

# 2. On Render:
#    New → Web Service → connect your repository
#    Build command:  pip install -r requirements.txt
#    Start command:  (defined in render.yaml automatically)
#    Health check:   /ping

# 3. On Render:
#    New → Redis → copy the connection string to REDIS_URL

# 4. On GitHub:
#    Settings → Developer Settings → GitHub Apps → New GitHub App
#    Webhook URL:  https://your-service.onrender.com/webhook
#    Permissions:  Contents R/W, Issues R/W, Pull Requests R/W, Actions R/W
#    Events:       pull_request, issues, issue_comment, push, check_run
```

Full step-by-step guide → [docs/deployment/render-deploy.md](docs/deployment/render-deploy.md)

---

## Troubleshooting

**Webhooks not being received**
- Confirm `/ping` returns `{"status": "ok"}`
- Check Render logs for `webhook.rejected` entries
- Verify `GITHUB_WEBHOOK_SECRET` in Render matches the value in your GitHub App settings

**Commands not triggering**
- Commands must be posted on an issue or PR, not on a commit or discussion
- Verify you have the required permission level for restricted commands (🔐)
- Confirm the GitHub App is installed on the target repository

**LLM calls failing**
- Check circuit breaker state at `/health` (requires `Authorization: Bearer <METRICS_AUTH_TOKEN>`)
- Verify `GROQ_API_KEY` is set in Render environment variables

**Redis errors**
- `/report` and `/budget` require Redis — add `REDIS_URL` in Render environment variables
- Render free Redis: Dashboard → New → Redis → copy the connection string

---

## Documentation Index

| Document | Description |
|----------|-------------|
| [User Setup Guide](docs/guides/user-setup.md) | GitHub App creation, webhook configuration, first install |
| [Slash Commands Reference](docs/guides/slash-commands.md) | All 26 commands with examples and permission requirements |
| [Render Deployment](docs/deployment/render-deploy.md) | Step-by-step Render deployment guide |
| [AI Routing](docs/ai-system/ai-routing.md) | Multi-provider router, circuit breakers, task classification |
| [Autofix Engine](docs/ai-system/autofix-engine.md) | How `/autofix` creates branches and pull requests |
| [Threat Model](docs/security/threat-model.md) | Security design, attack surface, and mitigations |
| [Observability](docs/observability/observability.md) | Health checks, metrics, and logging |
| [Testing Guide](docs/testing/testing-guide.md) | How to write and run tests |

---

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

---

## License

MIT — free to use, modify, and distribute.

---

<div align="center">

Built by [Shweta Mishra](https://github.com/Shweta-Mishra-ai)

If this project was useful to you, a ⭐ is appreciated.

[![GitHub stars](https://img.shields.io/github/stars/Shweta-Mishra-ai/github-autopilot?style=social)](https://github.com/Shweta-Mishra-ai/github-autopilot)

</div>
