<div align="center">

# GitHub Autopilot

**A self-hosted GitHub App that automates code review, issue triage, and repository maintenance using AI.**

[![CI](https://github.com/Shweta-Mishra-ai/github-autopilot/actions/workflows/ci.yml/badge.svg)](https://github.com/Shweta-Mishra-ai/github-autopilot/actions)
[![Python](https://img.shields.io/badge/python-3.11+-3b82f6?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Flask](https://img.shields.io/badge/flask-3.x-000000?style=flat-square&logo=flask)](https://flask.palletsprojects.com)
[![License: MIT](https://img.shields.io/badge/license-MIT-a855f7?style=flat-square)](LICENSE)
[![code style: ruff](https://img.shields.io/badge/code%20style-ruff-ef4444?style=flat-square)](https://docs.astral.sh/ruff)

[**Live Demo**](https://github-autopilot-1.onrender.com) · [**Install**](https://github.com/apps/ai-repo-manager) · [**Documentation**](docs/)

</div>

---

## Overview

GitHub Autopilot is a self-hosted GitHub App that installs in minutes and acts as an AI-powered co-pilot across your repositories. It reacts to GitHub events automatically and responds to slash commands posted in issue and pull request comments.

**Automated on every event:**
- Pull requests — rewrites vague titles, fills empty descriptions, rates code quality (A–F), identifies test gaps, maps blast radius across system layers
- Issues — assigns priority and complexity labels, generates targeted follow-up questions, estimates resolution time
- Push — scans for exposed secrets across 35+ patterns, checks for known CVEs in dependencies

**On demand via slash commands:**
- 26 commands covering code quality, documentation, security, releases, and operations
- Rate-limited per user and per repository to prevent abuse
- Permission-gated so destructive operations require write access

---

## Free Tier Deployment

GitHub Autopilot is designed to run at zero cost on Render's free tier with Groq's free API.

| Resource | Limit |
|----------|-------|
| Concurrent webhook workers | 6 threads |
| AI requests (Groq free) | 14,400 per day |
| AI calls per repository | 150 per day (configurable) |
| Redis storage (Render free) | 25 MB |
| Server sleep | After 15 minutes of inactivity |

---

## Installation

### Prerequisites

- Python 3.11 or higher
- Redis instance (Render provides one for free)
- Groq API key — free at [console.groq.com](https://console.groq.com)
- A GitHub App (created during setup)

### Local setup

```bash
git clone https://github.com/Shweta-Mishra-ai/github-autopilot.git
cd github-autopilot
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                              # Fill in required values
flask --app server run --port 5000
```

### Deploy to Render

```bash
# 1. Push this repository to GitHub
# 2. On Render: New → Web Service → connect your repository
#    Build command: pip install -r requirements.txt
#    Start command: defined in render.yaml
#    Health check:  /ping
# 3. On Render: New → Redis → copy the connection string to REDIS_URL
# 4. On GitHub: Settings → Developer Settings → GitHub Apps → New
#    Webhook URL: https://your-service.onrender.com/webhook
#    Permissions:  Contents, Issues, Pull Requests, Actions (Read & Write)
#    Events:       pull_request, issues, issue_comment, push, check_run
```

Full step-by-step guide: [docs/deployment/render-deploy.md](docs/deployment/render-deploy.md)

---

## Slash Commands

Post any of these as a comment on a GitHub issue or pull request.

> Commands marked 🔐 require **write**, **maintain**, or **admin** access.  
> Rate limit: 10 commands per user per hour, per repository.

### Code Quality

| Command | Description |
|---------|-------------|
| `/fix` | Root cause analysis with a production-ready fix and a suggested verification test |
| `/autofix` 🔐 | Creates a branch, applies the fix, and posts a diff preview for review |
| `/apply` 🔐 | Opens a pull request from an autofix branch once you have reviewed the diff |
| `/improve` | Scored suggestions across performance, security, and readability |
| `/refactor` | Structural refactor recommendations with before/after examples |
| `/perf` | Time complexity analysis, N+1 query detection, and optimisation suggestions |

### Code Understanding

| Command | Description |
|---------|-------------|
| `/explain` | Plain-English explanation: what the code does, how it works, and common pitfalls |
| `/summarize` | Condenses a long pull request or issue thread into a concise summary |
| `/arch` | Architecture review highlighting coupling issues and layer violations |
| `/impact` | Blast radius map showing which system layers a change touches |
| `/gaps` | Test coverage gap analysis with risk-rated suggestions |
| `/ci` | CI failure root cause analysis with concrete fix steps |

### Documentation and Releases

| Command | Description |
|---------|-------------|
| `/docs` | Generates docstrings and a README section for the changed code |
| `/test` | Generates a pytest test suite for the changed code |
| `/changelog` | Produces a Keep a Changelog entry from recent commit history |
| `/release` 🔐 | Creates a GitHub draft release with AI-generated release notes |
| `/version` | Shows tag history and semantic versioning status |
| `/runtests` 🔐 | Triggers a GitHub Actions workflow via `workflow_dispatch` |

### Security and Health

| Command | Description |
|---------|-------------|
| `/security` | Scans the pull request diff for exposed secrets and vulnerable dependencies |
| `/secfull` 🔐 | Full security report: Dependabot alerts, CodeQL findings, Secret Scanning |
| `/health` | Repository health grade (A–F) with ranked improvement recommendations |

### Operations

| Command | Description |
|---------|-------------|
| `/merge` 🔐 | Merges the pull request after guardrails pass: CI green, reviews approved, no conflicts |
| `/rollback` 🔐 | Lists snapshots or restores repository state (requires two-step confirmation) |
| `/report` | Weekly analytics: pull request velocity, issue resolution time, quality grade |
| `/budget` | Live LLM token usage and estimated cost breakdown per provider |
| `/notify` | Sends an issue or pull request alert to Discord or Slack |

---

## How It Works

```
Incoming Webhook (POST /webhook)
           │
           ▼
  ┌─────────────────────────────────────┐
  │          Security Pipeline           │
  │  1. HMAC-SHA256 signature check     │
  │  2. IP rate limiting (Redis)        │
  │  3. Replay protection (Redis NX)    │
  │  4. Bot loop detection              │
  └──────────────────┬──────────────────┘
                     │  ACK 202 immediately
                     ▼
       Thread Pool — 6 workers, 50-job cap
       ┌──────────┬──────────┬──────────┐
       │    PR    │  Issues  │ Comments │  Push
       │  review  │  triage  │ commands │  scan
       └────┬─────┴────┬─────┴────┬─────┘
            └──────────┴──────────┘
                       │
                       ▼
                   AI Router
       ┌──────────────────────────────┐
       │  Groq 70B  — primary         │
       │  Groq 8B   — fast tasks      │
       │  Gemini    — long context    │
       │  OpenRouter — fallback       │
       │                              │
       │  Circuit breakers per-       │
       │  provider · Hallucination    │
       │  detection · Cost tracking   │
       └───────────────┬──────────────┘
                       │
                       ▼
              Post result to GitHub
```

---

## Security

| Threat | Mitigation |
|--------|------------|
| Forged webhooks | HMAC-SHA256 verification; server refuses to start without `GITHUB_WEBHOOK_SECRET` |
| Replay attacks | SHA-256 event fingerprint in Redis with SET NX; 1-hour TTL |
| Webhook floods | Per-IP rate limiting (100 req/min); bounded thread pool (6 workers) |
| Privilege escalation | GitHub collaborator API permission check before every restricted command |
| Prompt injection | Input sanitisation and 8,000-character limit per field |
| Secret exposure | 35+ regex patterns with Shannon entropy gating |
| Bot feedback loops | `sender.type` and `[bot]` suffix detection |
| Command abuse | 10 commands per user per hour; 150 AI calls per repository per day |

Full threat model: [docs/security/threat-model.md](docs/security/threat-model.md)

---

## Environment Variables

| Variable | Required | Purpose |
|----------|:--------:|---------|
| `GITHUB_APP_ID` | ✅ | Numeric App ID from GitHub App settings |
| `GITHUB_PRIVATE_KEY` | ✅ | RSA private key in PEM format, including headers |
| `GITHUB_WEBHOOK_SECRET` | ✅ | Server will not start without this value |
| `GROQ_API_KEY` | ✅ | Primary LLM — free at [console.groq.com](https://console.groq.com) |
| `REDIS_URL` | ✅ | Redis connection string |
| `GEMINI_API_KEY` | ⚡ | Gemini Flash fallback — [aistudio.google.com](https://aistudio.google.com) |
| `OPENROUTER_API_KEY` | ⚡ | Emergency LLM fallback — [openrouter.ai](https://openrouter.ai) |
| `DISCORD_WEBHOOK_URL` | 📢 | Discord notifications via `/notify` |
| `SLACK_WEBHOOK_URL` | 📢 | Slack notifications via `/notify` |
| `METRICS_AUTH_TOKEN` | 🔒 | Bearer token required to access `/health` detail endpoint |
| `MAX_DISPATCH_WORKERS` | ⚙️ | Thread pool size (default: `6`) |
| `REPO_DAILY_AI_LIMIT` | ⚙️ | Maximum AI calls per repository per day (default: `150`) |

> ✅ Required &nbsp;·&nbsp; ⚡ Recommended &nbsp;·&nbsp; 📢 Optional &nbsp;·&nbsp; 🔒 Security &nbsp;·&nbsp; ⚙️ Tuning

Copy `.env.example` to `.env` and fill in the required values to get started.

---

## Project Structure

```
github-autopilot/
├── server.py                    # Entry point — security pipeline and event dispatch
├── .env.example                 # All supported environment variables with descriptions
├── .ai-repo-manager.yml         # Per-repository bot configuration schema
│
├── app/
│   ├── ai/
│   │   ├── router.py            # Multi-provider LLM router with task classification
│   │   ├── circuit_breaker.py   # Per-provider circuit breakers (CLOSED / OPEN / HALF_OPEN)
│   │   ├── hallucination.py     # Response confidence scoring and placeholder detection
│   │   └── providers/           # Groq, Gemini, OpenRouter implementations
│   │
│   ├── core/
│   │   ├── webhook_security.py  # Full webhook verification pipeline
│   │   ├── authorization.py     # Command permission enforcement
│   │   ├── thread_pool.py       # Bounded ThreadPoolExecutor
│   │   ├── idempotency.py       # SHA-256 event deduplication via Redis
│   │   ├── analytics.py         # Usage tracking and /report data
│   │   └── snapshot.py          # Repository snapshots for /rollback
│   │
│   ├── github/
│   │   ├── auth.py              # JWT generation and installation token exchange
│   │   ├── client.py            # GitHub REST API client with retry and backoff
│   │   ├── helpers.py           # Shared utilities
│   │   └── notifications.py     # Discord and Slack message builder
│   │
│   ├── handlers/
│   │   ├── comments.py          # Slash command dispatcher — 26 commands
│   │   ├── autofix.py           # Automated fix engine: diff → branch → pull request
│   │   ├── pull_request.py      # PR analysis, blast radius mapping, review posting
│   │   ├── issues.py            # Issue triage, labelling, and first-response
│   │   ├── push.py              # Secret scanning and dependency checks on push
│   │   └── ci.py                # CI failure analysis
│   │
│   └── security/
│       ├── enhanced_secrets.py  # 35+ secret patterns with entropy gating
│       ├── dependencies.py      # CVE vulnerability scanner
│       └── scanner.py           # Dependabot and CodeQL API integration
│
├── tests/                       # Full test suite — no network calls required
├── docs/                        # Technical documentation
└── archive/                     # Inactive code retained for reference
```

---

## Troubleshooting

**Webhooks not being processed**
- Confirm `/ping` returns `{"status": "ok"}`
- Check Render logs for `webhook.rejected` — includes the rejection reason
- Verify `GITHUB_WEBHOOK_SECRET` in Render matches the value set in your GitHub App

**Commands not responding**
- Commands work on issues and pull requests only, not on commits or discussions
- Verify you have the required permission for restricted commands (🔐)
- Confirm the GitHub App is installed on the target repository

**LLM calls failing**
- Check the circuit breaker status at `/health` using `Authorization: Bearer <METRICS_AUTH_TOKEN>`
- Verify `GROQ_API_KEY` is set correctly in Render environment variables

**Redis errors in logs**
- `/report` and `/budget` require Redis — add `REDIS_URL` in Render environment variables
- Render free Redis: Dashboard → New → Redis → copy the connection string

---

## Documentation

| Document | Description |
|----------|-------------|
| [User Setup Guide](docs/guides/user-setup.md) | GitHub App creation, permissions, first install |
| [Slash Commands Reference](docs/guides/slash-commands.md) | All 26 commands with examples and permissions |
| [Render Deployment](docs/deployment/render-deploy.md) | Step-by-step production deployment |
| [AI Routing](docs/ai-system/ai-routing.md) | Multi-provider router and circuit breaker design |
| [Autofix Engine](docs/ai-system/autofix-engine.md) | How `/autofix` creates branches and pull requests |
| [Threat Model](docs/security/threat-model.md) | Security design and attack surface analysis |
| [Observability](docs/observability/observability.md) | Health endpoints, metrics, and monitoring setup |
| [Testing Guide](docs/testing/testing-guide.md) | Test patterns, mocking strategy, and CI setup |

---

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a pull request.

---

## License

Released under the [MIT License](LICENSE).

---

<div align="center">

Built by [Shweta Mishra](https://github.com/Shweta-Mishra-ai)

If this project is useful to you, a ⭐ is appreciated.

[![GitHub Stars](https://img.shields.io/github/stars/Shweta-Mishra-ai/github-autopilot?style=social)](https://github.com/Shweta-Mishra-ai/github-autopilot/stargazers)

</div>
