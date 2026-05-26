<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:6366f1,50:8b5cf6,100:06b6d4&height=220&section=header&text=GitHub%20Autopilot&fontSize=56&fontColor=ffffff&fontAlignY=38&desc=Production-grade%20AI%20automation%20for%20every%20GitHub%20repo&descAlignY=60&descSize=18&animation=fadeIn" width="100%"/>

<br/>

[![Version](https://img.shields.io/badge/version-4.0-6366f1?style=for-the-badge&logo=github&logoColor=white)](https://github.com/Shweta-Mishra-ai/github-autopilot)
[![Tests](https://img.shields.io/badge/tests-306%20passing-22c55e?style=for-the-badge&logo=pytest&logoColor=white)](https://github.com/Shweta-Mishra-ai/github-autopilot/actions)
[![Live](https://img.shields.io/badge/server-live-22c55e?style=for-the-badge&logo=render&logoColor=white)](https://github-autopilot-1.onrender.com)
[![Python](https://img.shields.io/badge/python-3.11+-3b82f6?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![Redis](https://img.shields.io/badge/redis-backed-ef4444?style=for-the-badge&logo=redis&logoColor=white)](https://redis.io)
[![License](https://img.shields.io/badge/license-MIT-a855f7?style=for-the-badge)](LICENSE)

<br/>

[![LLM](https://img.shields.io/badge/LLM-Groq%20%7C%20Gemini%20%7C%20OpenRouter-f97316?style=flat-square&logo=openai&logoColor=white)]()
[![Flask](https://img.shields.io/badge/Flask-2.x-000000?style=flat-square&logo=flask&logoColor=white)]()
[![GitHub App](https://img.shields.io/badge/GitHub-App-181717?style=flat-square&logo=github&logoColor=white)](https://github.com/apps/ai-repo-manager)
[![Ruff](https://img.shields.io/badge/linted-ruff-ef4444?style=flat-square)]()
[![Security](https://img.shields.io/badge/security-hardened-22c55e?style=flat-square&logo=shield&logoColor=white)]()
[![Sprints](https://img.shields.io/badge/sprints%20completed-8-8b5cf6?style=flat-square)]()

<br/>

> **GitHub Autopilot** is a self-hosted GitHub App that installs in one click and gives every repository an AI co-pilot.
> It reviews PRs, triages issues, scans for secrets and vulnerabilities, fixes bugs, and responds to **26 slash commands** —
> all powered by a multi-provider LLM router with circuit breakers, hallucination detection, and rate limiting.
>
> 🏆 Built across **8 sprints** from zero to production — fully solo, fully shipped.

<br/>

| 🚀 [Live Server](https://github-autopilot-1.onrender.com) | 🤖 [Install App](https://github.com/apps/ai-repo-manager) | 📊 [Health Check](https://github-autopilot-1.onrender.com/health) | 📖 [Docs](docs/) |
|:---:|:---:|:---:|:---:|

</div>

---

## 🏆 8-Sprint Journey — Zero to Production

> This project was built sprint-by-sprint, each adding a new layer of intelligence and reliability. Here's the full story:

| Sprint | What Was Built |
|--------|----------------|
| **Sprint 1** | 🧱 Foundation — Flask webhook server, threading, bot-spam prevention, event deduplication |
| **Sprint 2** | 🤖 Multi-provider LLM router, per-provider circuit breakers, Gemini Flash fallback |
| **Sprint 3** | 🧠 Hallucination detection, LLM quality scoring, `/fix` command v2 |
| **Sprint 4** | 💥 PR blast radius mapping, `/impact`, `/secfull`, CI failure handler |
| **Sprint 5** | 🔁 Retry + exponential backoff, `/health` endpoint, repo snapshots + `/rollback` |
| **Sprint 6** | 📊 Analytics system, `/report`, `/autofix` engine — auto-fix PRs end-to-end |
| **Sprint 7** | ⚡ `/perf`, `/arch`, vector context (Qdrant), learning system, **26 slash commands** total |
| **Sprint 8** | 🔐 Full security hardening — webhook fail-closed, HMAC verification, auth enforcement, bounded thread pool, 35+ secret patterns, entropy gating, **306 tests** |

---

## ✨ What It Does

<table>
<tr>
<td width="50%">

**🔍 Automatic PR Review**
- Rewrites vague PR titles
- Fills empty descriptions
- Rates code quality 1–10
- Detects test coverage gaps
- Shows blast radius by layer

</td>
<td width="50%">

**🐛 Issue Triage**
- Priority: critical → low
- Complexity + time estimate
- Auto-labels on open
- Personalized welcome message
- Asks targeted follow-up questions

</td>
</tr>
<tr>
<td width="50%">

**🔒 Security Scanning**
- 35+ secret patterns detected
- CVE vulnerability scan
- GitHub Security API integration
- Deduped alerts (zero spam)
- Entropy-gated false positives

</td>
<td width="50%">

**🤖 Slash Commands (26)**
- `/fix` `/autofix` `/explain`
- `/improve` `/test` `/docs`
- `/merge` `/release` `/rollback`
- `/perf` `/arch` `/secfull`
- ...and 14 more

</td>
</tr>
</table>

---

## 🏗️ Architecture

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                           GITHUB WEBHOOK                                    ║
║              POST /webhook  ·  X-Hub-Signature-256 verified                ║
╚══════════════════════════════╤═══════════════════════════════════════════════╝
                               │
                               ▼
╔══════════════════════════════════════════════════════════════════════════════╗
║                         server.py  (Flask)                                  ║
║                                                                              ║
║   ① Startup check — refuses to boot without GITHUB_WEBHOOK_SECRET           ║
║   ② HMAC-SHA256 signature  →  fail closed on empty secret                   ║
║   ③ IP rate limit  →  100 req/min  ·  Redis-backed sliding window           ║
║   ④ Replay protection  →  timestamp + idempotency fingerprint               ║
║   ⑤ Bot loop guard  →  [bot] suffix + sender type check                     ║
║   ⑥ ACK 202 immediately  →  dispatch to bounded ThreadPoolExecutor          ║
║                              (6 workers · 50-job queue · drops + logs)       ║
╚══════════════════════════════╤═══════════════════════════════════════════════╝
                               │  ThreadPoolExecutor  (max_workers=6)
          ┌────────────────────┼────────────────────────────┐
          ▼                    ▼                            ▼
  pull_request.py         comments.py                   push.py
  issues.py               (26 slash cmds)               ci.py
          │                    │                            │
          │         ┌──────────▼──────────────┐            │
          │         │  authorization.py        │            │
          │         │  • check_command_        │            │
          │         │    permission()          │            │
          │         │  • write/maintain/admin  │            │
          │         │  • 5-min cache (RLock)   │            │
          │         └──────────┬──────────────┘            │
          └──────────────────┬─┘────────────────────────────┘
                             │
                             ▼
╔══════════════════════════════════════════════════════════════════════════════╗
║                          AI Router  (app/ai/router.py)                      ║
║                                                                              ║
║   Task Classification  ──►  fast · standard · deep · long-context           ║
║                                                                              ║
║   ┌────────────────┐   ┌────────────────┐   ┌────────────────┐             ║
║   │  Groq 70B      │──►│  Groq 8B       │──►│  Gemini Flash  │──► OpenRouter║
║   │  (primary)     │   │  (fast tasks)  │   │  (long ctx)    │   (fallback) ║
║   │  5K req/day    │   │  12K req/day   │   │  1.5K req/day  │             ║
║   └───────┬────────┘   └───────┬────────┘   └───────┬────────┘             ║
║           └────────────────────┴─────────────────────┘                      ║
║                                │                                             ║
║   ┌─────────────────────────────────────────────────────────┐               ║
║   │  Per-provider Circuit Breaker                           │               ║
║   │  CLOSED ──► OPEN ──► HALF_OPEN ──► CLOSED              │               ║
║   │  5 failures → open · 60s cooldown · test one call      │               ║
║   └─────────────────────────────────────────────────────────┘               ║
║                                │                                             ║
║   ┌─────────────────────────────────────────────────────────┐               ║
║   │  Hallucination Detector                                  │               ║
║   │  Confidence score · Warning phrases · Placeholder check │               ║
║   │  Low confidence → fallback provider · never post junk  │               ║
║   └─────────────────────────────────────────────────────────┘               ║
╚══════════════════════════════╤═══════════════════════════════════════════════╝
                               │
          ┌────────────────────┼──────────────────────────────┐
          ▼                    ▼                              ▼
╔══════════════╗    ╔══════════════════════╗    ╔════════════════════════╗
║    Redis     ║    ║   GitHub REST API    ║    ║  enhanced_secrets.py   ║
║              ║    ║                      ║    ║                        ║
║ Idempotency  ║    ║  Issues · PRs        ║    ║  35+ patterns          ║
║ Circuit state║    ║  Comments · Labels   ║    ║  Entropy gating        ║
║ Snapshots    ║    ║  Releases · Actions  ║    ║  False-positive filter ║
║ Analytics    ║    ║  Security APIs       ║    ║  Severity: crit/hi/med ║
║ Rate limits  ║    ║  Webhooks            ║    ║  No scannable literals ║
║ Config cache ║    ╚══════════════════════╝    ╚════════════════════════╝
║ Budget track ║
╚══════════════╝
```

---

## ⚡ Quick Start

```bash
# 1. Clone
git clone https://github.com/Shweta-Mishra-ai/github-autopilot.git
cd github-autopilot

# 2. Install
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 3. Configure
cp .env.example .env   # fill in your keys

# 4. Run
flask --app server run --port 5000

# 5. Run tests
python -m pytest -v    # 306 passing
```

---

## 🤖 Slash Commands (26 Total)

Comment any command on a GitHub issue or PR to activate it.

### 🔧 Code Quality
| Command | What it does |
|---------|-------------|
| `/fix` | Root cause analysis + production-ready fix + verification test |
| `/autofix` | Creates fix branch + commits the fix + opens PR automatically |
| `/apply` | Auto-rewrites non-conventional commit messages across branch |
| `/improve` | Scored improvements — performance, security, readability, structure |
| `/refactor` | Structural refactor suggestions with before/after code |
| `/perf` | Time complexity analysis, N+1 detection, optimization suggestions |

### 🧠 Understanding
| Command | What it does |
|---------|-------------|
| `/explain` | 5-section plain-English explanation: What / How / Why / Example / Pitfalls |
| `/summarize` | Condenses long PR/issue discussion threads |
| `/arch` | Architecture review — layer violations, coupling, god classes |
| `/impact` | Blast radius map — which system layers this PR touches |
| `/gaps` | Test coverage gaps with risk-rated suggestions |
| `/ci` | CI failure root cause + concrete fix steps |

### 📄 Documentation & Release
| Command | What it does |
|---------|-------------|
| `/docs` | Generates docstrings + README section |
| `/test` | Generates pytest test suite for changed code |
| `/changelog` | AI-written CHANGELOG entry from commit history |
| `/release` | Creates GitHub draft release with AI release notes |
| `/version` | Tag history + semantic versioning status |

### 🔒 Security & Health
| Command | What it does |
|---------|-------------|
| `/security` | Scans PR diff for secrets + vulnerable dependencies |
| `/secfull` | Full report: Dependabot + CodeQL + Secret Scanning APIs |
| `/health` | Repo health grade A–F with ranked recommendations |

### ⚙️ Operations
| Command | What it does |
|---------|-------------|
| `/merge` | Merges PR after guardrails pass (CI green, reviews, no conflicts) |
| `/rollback` | Lists snapshots or restores repo state to pre-bot snapshot |
| `/runtests` | Triggers GitHub Actions test workflow via workflow_dispatch |
| `/report` | Weekly analytics: PR velocity, issue resolution, quality grade |
| `/budget` | Live LLM token usage and cost per provider |
| `/notify` | Sends issue/PR to Discord with color-coded severity embed |

> 🔐 `/merge` `/rollback` `/release` `/autofix` `/apply` `/secfull` require **write/maintain/admin** access.
> Rate limit: **10 commands per user per hour** per repo.

---

## 🔐 Security Model

```
Threat                    Mitigation
─────────────────────     ──────────────────────────────────────────────
Forged webhooks           HMAC-SHA256  ·  fail closed on empty secret
                          RuntimeError at boot if secret not configured

Replay attacks            Timestamp header check + SHA-256 delivery dedup
                          Rejects webhooks older than 5 minutes

Webhook floods            IP rate limit: 100 req/min (Redis sliding window)
                          ThreadPoolExecutor cap: 6 workers, 50 queue

Privilege escalation      Authorization check before every restricted command
                          GitHub collaborator API  ·  5-min permission cache
                          Fail closed on API error (deny if unsure)

Prompt injection          Input sanitization  ·  blocklist + heuristic scan
                          Max 2000 chars per context field

Secret leaks in code      35+ pattern scanner  ·  entropy gating
                          Zero scannable literals in source files
                          Test files / docs automatically skipped

Bot feedback loops        sender.type == Bot  ·  [bot] suffix check
                          Own app login set  ·  SKIP_AUTHORS list

Command spam              10 commands / user / hour via Redis counter
```

---

## 📁 Project Structure

```
github-autopilot/
│
├── server.py                      # Entry point — webhook security + bounded dispatch
├── Procfile                       # gunicorn config
├── render.yaml                    # Render deploy (web + Redis)
├── requirements.txt
│
├── app/
│   ├── ai/                        # LLM Layer
│   │   ├── router.py              # Task router (4 providers, smart classification)
│   │   ├── circuit_breaker.py     # Per-provider CLOSED/OPEN/HALF_OPEN state machine
│   │   ├── hallucination.py       # Response quality validator (confidence scoring)
│   │   ├── metrics.py             # Token usage + cost tracking
│   │   ├── validator.py           # JSON schema validation
│   │   └── providers/
│   │       ├── base.py            # Abstract LLMProvider + LLMResponse
│   │       ├── groq.py            # Groq Llama 70B + 8B
│   │       ├── gemini.py          # Google Gemini Flash
│   │       └── openrouter.py      # OpenRouter emergency fallback
│   │
│   ├── core/                      # Foundation — no side effects, no GitHub calls
│   │   ├── authorization.py       # ✨ Sprint 8: command permission enforcement
│   │   ├── config.py              # YAML config loader (thread-safe 5-min cache)
│   │   ├── thread_pool.py         # ✨ Sprint 8: bounded ThreadPoolExecutor
│   │   ├── webhook_security.py    # ✨ Sprint 8: full webhook verification pipeline
│   │   ├── analytics.py           # PR/issue/command usage tracking
│   │   ├── cache.py               # Redis API response cache
│   │   ├── confidence.py          # Per-action confidence scoring
│   │   ├── context_manager.py     # Conversation context (Redis-backed)
│   │   ├── guardrails.py          # Deterministic safety checks
│   │   ├── idempotency.py         # SHA-256 event deduplication
│   │   ├── logger.py              # Structured event logger
│   │   ├── redis_client.py        # Singleton connection pool + FakeRedis fallback
│   │   └── snapshot.py            # Repo snapshot store + rollback engine
│   │
│   ├── github/                    # GitHub API Layer
│   │   ├── auth.py                # JWT + installation token exchange
│   │   ├── client.py              # HTTP client (retry + exponential backoff)
│   │   ├── notifications.py       # Discord/Slack rich embed sender
│   │   └── rate_limit.py          # API quota tracking + wait logic
│   │
│   ├── handlers/                  # Event Handlers
│   │   ├── autofix.py             # Auto-fix engine (diff → branch → PR)
│   │   ├── ci.py                  # CI failure handler + pattern tracking
│   │   ├── comments.py            # 26 slash commands dispatcher
│   │   ├── issues.py              # Issue triage + labeling
│   │   ├── pull_request.py        # PR analysis + blast radius + code review
│   │   └── push.py                # Commit lint + dep scan + secret scan (deduped)
│   │
│   ├── intelligence/              # Vector Context Layer
│   │   ├── embeddings.py          # Code embedding (sentence-transformers, local)
│   │   ├── retrieval.py           # Qdrant/ChromaDB similarity search
│   │   └── summarizer.py          # Thread + PR summarization
│   │
│   ├── security/                  # Security Layer
│   │   ├── enhanced_secrets.py    # ✨ Sprint 8: 35+ patterns, entropy, false-pos guard
│   │   ├── dependencies.py        # CVE scanner (requirements.txt)
│   │   ├── licenses.py            # License compliance checker
│   │   └── scanner.py             # GitHub Security APIs (Dependabot, CodeQL)
│   │
│   └── storage/                   # Persistence
│       ├── events.py              # SQLite event log
│       └── fixtures.py            # Test fixture capture + replay
│
└── tests/                         # 306 tests — zero network calls required
    ├── test_webhook_security.py   # ✨ Sprint 8: 35 tests
    ├── test_enhanced_secrets.py   # ✨ Sprint 8: 26 tests
    ├── test_push.py               # ✨ Sprint 8: 25 tests (incl. dedup regression)
    ├── test_pull_request.py       # ✨ Sprint 8: 22 tests
    ├── test_issues.py             # ✨ Sprint 8: 15 tests
    ├── test_ci.py                 # ✨ Sprint 8: 18 tests
    ├── test_autofix.py            # 15 tests
    ├── test_analytics.py
    ├── test_comments.py
    ├── test_confidence.py
    ├── test_guardrails.py
    ├── test_hallucination.py
    ├── test_idempotency.py
    ├── test_providers.py
    ├── test_router.py
    ├── test_secrets.py
    ├── test_storage.py
    └── test_validator.py
```

---

## ⚙️ Environment Variables

| Variable | Required | Description |
|----------|:--------:|-------------|
| `GITHUB_APP_ID` | ✅ | Numeric App ID from GitHub App settings |
| `GITHUB_PRIVATE_KEY` | ✅ | Contents of `.pem` private key (paste full PEM) |
| `GITHUB_WEBHOOK_SECRET` | ✅ | Webhook secret — **app refuses to start without this** |
| `GROQ_API_KEY` | ✅ | Primary LLM — [console.groq.com](https://console.groq.com) (free) |
| `REDIS_URL` | ✅ | Redis connection string — Render provides this |
| `GEMINI_API_KEY` | ⚡ | Gemini Flash fallback — [aistudio.google.com](https://aistudio.google.com/app/apikey) |
| `OPENROUTER_API_KEY` | ⚡ | Emergency fallback — [openrouter.ai](https://openrouter.ai) |
| `DISCORD_WEBHOOK_URL` | 📢 | Discord notifications |
| `SLACK_WEBHOOK_URL` | 📢 | Slack notifications |
| `QDRANT_URL` | 🧠 | Vector DB — [qdrant.tech](https://qdrant.tech) free tier |
| `QDRANT_API_KEY` | 🧠 | Qdrant Cloud API key |
| `METRICS_AUTH_TOKEN` | 🔒 | Protects `/metrics` endpoint |
| `MAX_DISPATCH_WORKERS` | ⚙️ | Thread pool size (default: 6) |
| `REPO_DAILY_AI_LIMIT` | ⚙️ | Max AI calls per repo per day (default: 150) |

> ✅ Required &nbsp;·&nbsp; ⚡ Recommended &nbsp;·&nbsp; 📢 Optional &nbsp;·&nbsp; 🧠 Enables vector context &nbsp;·&nbsp; 🔒 Security &nbsp;·&nbsp; ⚙️ Tuning

---

## 🚀 Deploy to Render (Free Tier)

```bash
# 1. Push to GitHub

# 2. Render → New Web Service → connect repo
#    Build:  pip install -r requirements.txt
#    Start:  gunicorn server:app --workers 2 --timeout 120 --bind 0.0.0.0:$PORT
#    Health: /health

# 3. Render → New → Redis (free) → copy URL → REDIS_URL env var

# 4. Add all env vars in Render → Environment

# 5. Create GitHub App at github.com/settings/apps/new
#    Webhook URL:    https://YOUR-SERVICE.onrender.com/webhook
#    Permissions:    Contents R/W, Issues R/W, Pull requests R/W,
#                    Actions R/W, Metadata R
#    Events:         pull_request, issues, issue_comment, push, check_run
```

---

## 🛠️ Configuration (`.ai-repo-manager.yml`)

Place in repo root. All sections optional — safe defaults apply.

```yaml
pull_requests:
  auto_polish_title: true
  auto_fill_description: true
  code_review: true
  detect_test_gaps: true
  max_files_reviewed: 4

push:
  enforce_conventional_commits: true
  scan_secrets: true
  scan_dependencies: true
  create_issue_threshold: 3   # bad commits needed before creating issue

confidence:
  thresholds:
    pr_title_rewrite: 0.85
    auto_merge: 0.95
    fix_command: 0.70

issues:
  enabled: true
  auto_label: true

commands:
  permissions:
    maintainer_only:
      - merge
      - release
      - rollback
  enabled:
    - fix
    - autofix
    - explain
    - improve
    - test
    - merge
    - security
    - secfull
    - rollback
    - report
```

---

## 🧪 Running Tests

```bash
# Full suite
python -m pytest -v
# → 306 passed

# By module
python -m pytest tests/test_webhook_security.py -v   # 35 tests
python -m pytest tests/test_push.py -v               # 25 tests (incl. dedup regression)
python -m pytest tests/test_pull_request.py -v       # 22 tests
python -m pytest tests/test_enhanced_secrets.py -v   # 26 tests

# With coverage
python -m pytest --cov=app --cov-report=term-missing tests/

# Lint (matches CI exactly)
ruff check app/ --select E,F,W --ignore E501
```

---

## 📊 Tech Stack

| Layer | Technology | Notes |
|-------|------------|-------|
| Runtime | Python 3.11+ | |
| Web | Flask + Gunicorn | 2 workers, 120s timeout |
| Primary LLM | Groq Llama 3.3 70B | 5K req/day free |
| Fast LLM | Groq Llama 3.1 8B | 12K req/day free |
| Long Context | Gemini Flash 1.5 | 1.5K req/day free · 1M token ctx |
| Fallback LLM | OpenRouter | 200 req/day free |
| State / Cache | Redis | Connection pool · FakeRedis for dev |
| Vector DB | Qdrant Cloud | 1GB free tier |
| Embeddings | sentence-transformers | Runs locally — no API needed |
| Security | enhanced_secrets.py | 35+ patterns · entropy gating |
| Testing | pytest | 306 tests · zero network calls |
| Deploy | Render | Free tier |
| Lint | Ruff 0.8.0 | E,F,W rules |

---

## 📜 Version History — 8 Sprints, One Vision

| Version | Sprint | Highlights |
|---------|--------|------------|
| **v4.s8** | **Sprint 8** | 🔐 Full security hardening: webhook fail-closed, HMAC verification, auth enforcement, bounded thread pool, 35+ secret patterns, entropy gating, **306 tests** |
| **v4.7** | **Sprint 7** | ⚡ `/perf`, `/arch`, vector context (Qdrant + ChromaDB), learning system, **26 slash commands** total |
| **v4.6** | **Sprint 6** | 📊 Analytics dashboard, `/report`, `/autofix` engine — diff → branch → PR, fully automated |
| **v4.5** | **Sprint 5** | 🔁 Retry + exponential backoff, `/health` endpoint, repo snapshot store + `/rollback` |
| **v4.4** | **Sprint 4** | 💥 PR blast radius mapping, `/impact`, `/secfull`, CI failure handler + pattern tracking |
| **v4.3** | **Sprint 3** | 🧠 Hallucination detection, LLM confidence scoring, `/fix` v2 with verification tests |
| **v4.2** | **Sprint 2** | 🤖 Multi-provider LLM router, per-provider circuit breakers, Gemini Flash fallback |
| **v4.0** | **Sprint 1** | 🧱 Flask webhook server, threading, bot-spam prevention, SHA-256 event deduplication |

---

## 📄 License

MIT — free to use, modify, distribute.

---

<div align="center">

**If this project helped you, a ⭐ means a lot!**

Built with ❤️ across 8 sprints by [Shweta Mishra](https://github.com/Shweta-Mishra-ai)

[![GitHub stars](https://img.shields.io/github/stars/Shweta-Mishra-ai/github-autopilot?style=social)](https://github.com/Shweta-Mishra-ai/github-autopilot)
[![Follow](https://img.shields.io/github/followers/Shweta-Mishra-ai?style=social)](https://github.com/Shweta-Mishra-ai)

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:06b6d4,50:8b5cf6,100:6366f1&height=120&section=footer" width="100%"/>

</div>
