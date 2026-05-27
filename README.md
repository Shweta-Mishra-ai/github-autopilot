<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:6366f1,50:8b5cf6,100:06b6d4&height=230&section=header&text=GitHub%20Autopilot&fontSize=58&fontColor=ffffff&fontAlignY=38&desc=Production-grade%20AI%20automation%20for%20every%20GitHub%20repo&descAlignY=60&descSize=18&animation=fadeIn" width="100%"/>

<br/>

[![Version](https://img.shields.io/badge/version-4.7-6366f1?style=for-the-badge&logo=github&logoColor=white)](https://github.com/Shweta-Mishra-ai/github-autopilot/releases)
[![Tests](https://img.shields.io/badge/tests-383%20passing-22c55e?style=for-the-badge&logo=pytest&logoColor=white)](https://github.com/Shweta-Mishra-ai/github-autopilot/actions)
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
[![Docs](https://img.shields.io/badge/docs-complete-8b5cf6?style=flat-square)](docs/)

<br/>

> **GitHub Autopilot** is a self-hosted GitHub App that installs in one click and gives every repository an AI co-pilot.
> It reviews PRs, triages issues, scans for secrets and vulnerabilities, fixes bugs, and responds to **26 slash commands** —
> all powered by a multi-provider LLM router with circuit breakers, hallucination detection, and zero cold-start cost.

<br/>

| 🚀 [Live Server](https://github-autopilot-1.onrender.com) | 🤖 [Install App](https://github.com/apps/ai-repo-manager) | 📊 [Health](https://github-autopilot-1.onrender.com/health) | 📖 [Docs](docs/) |
|:---:|:---:|:---:|:---:|

</div>

---

## 📌 Version History

| Version | What Was Built |
|---------|----------------|
| **v1.0** | 🧱 Flask webhook server · threading · bot-spam prevention · SHA-256 event deduplication |
| **v2.0** | 🤖 Multi-provider LLM router · per-provider circuit breakers · Gemini Flash fallback |
| **v3.0** | 🧠 Hallucination detection · confidence scoring · PR blast radius · `/impact` · `/secfull` · CI handler · retry + backoff · `/health` · repo snapshots · `/rollback` |
| **v4.0** | 📊 Analytics · `/report` · `/autofix` engine · `/perf` · `/arch` · vector context · learning system · **26 slash commands** · full security hardening · 35+ secret patterns · **383 tests** |

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
- Deduped alerts — zero spam
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

## 🔄 How It Works

```
┌──────────────────────────────────────────────────────────────────────┐
│                         YOU ON GITHUB                                │
│   Open PR · Create issue · Comment /fix · Push code                 │
└─────────────────────────────┬────────────────────────────────────────┘
                              │  GitHub sends webhook
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│                      SECURITY PIPELINE                               │
│                                                                      │
│  ① Size limit (25MB)     ② IP rate limit (100 req/min)              │
│  ③ HMAC-SHA256 verify    ④ Replay protection (Redis SET NX)         │
│  ⑤ Bot loop guard    →   ACK 202 in < 50ms                          │
└─────────────────────────────┬────────────────────────────────────────┘
                              │  Background thread (bounded pool, 6 workers)
            ┌─────────────────┼──────────────────┐
            ▼                 ▼                  ▼
      PR opened?        Issue created?    /command posted?
           │                  │                  │
           ▼                  ▼                  ▼
     Analyze PR          Triage issue      Permission check
     Blast radius         Auto-label       Rate limit check
     Code review         Welcome msg          Route to AI
     Test gaps           Questions                │
           │                  │                  │
           └──────────────────┴──────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│                          AI ROUTER                                   │
│                                                                      │
│  Groq 70B ──► Groq 8B ──► Gemini Flash ──► OpenRouter              │
│  Circuit breakers · Hallucination detection · Cost tracking          │
└─────────────────────────────┬────────────────────────────────────────┘
                              │  validated, confidence-scored response
                              ▼
                  Post comment to GitHub ✓
```

---

## 🏗️ Architecture

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                           GITHUB WEBHOOK                                    ║
║              POST /webhook  ·  X-Hub-Signature-256 verified                ║
╚═══════════════════════════════╤══════════════════════════════════════════════╝
                                │
                                ▼
╔══════════════════════════════════════════════════════════════════════════════╗
║                        server.py  (Flask)                                   ║
║                                                                              ║
║  ① Startup check  —  refuses to boot if GITHUB_WEBHOOK_SECRET not set       ║
║  ② HMAC-SHA256    —  fail closed on empty secret (not bypass)               ║
║  ③ IP rate limit  —  100 req/min · Redis sliding window                     ║
║  ④ Replay guard   —  SHA-256 fingerprint · Redis SET NX · 1h TTL           ║
║  ⑤ Bot detection  —  [bot] suffix · sender.type · own-app login set        ║
║  ⑥ ACK 202 immediately  →  ThreadPoolExecutor (6 workers · 50-job cap)     ║
╚═══════════════════════════════╤══════════════════════════════════════════════╝
                                │  async
           ┌────────────────────┼──────────────────────────┐
           ▼                    ▼                          ▼
   pull_request.py          comments.py               push.py
   issues.py                (26 commands)             ci.py
           │                    │                          │
           │        ┌───────────▼──────────────┐          │
           │        │    authorization.py        │          │
           │        │    check_command_          │          │
           │        │    permission()            │          │
           │        │    write/maintain/admin    │          │
           │        │    5-min cache · RLock     │          │
           │        └───────────┬──────────────┘          │
           └────────────────────┼──────────────────────────┘
                                │
                                ▼
╔══════════════════════════════════════════════════════════════════════════════╗
║                       AI Router  (app/ai/router.py)                         ║
║                                                                              ║
║   Task tier  ──►  fast · standard · deep · long-context                     ║
║                                                                              ║
║   ┌──────────────┐  ┌──────────────┐  ┌───────────────┐  ┌─────────────┐  ║
║   │  Groq 70B    │─►│  Groq 8B    │─►│ Gemini Flash  │─►│ OpenRouter  │  ║
║   │  primary     │  │  fast tasks  │  │ long context  │  │  emergency  │  ║
║   │  5K req/day  │  │  12K req/day │  │ 1.5K req/day  │  │ 200 req/day │  ║
║   └──────────────┘  └──────────────┘  └───────────────┘  └─────────────┘  ║
║                                                                              ║
║   Circuit Breaker  —  CLOSED ──► OPEN ──► HALF_OPEN ──► CLOSED             ║
║   3 failures → open · 60s cooldown · one test call to recover              ║
║                                                                              ║
║   Hallucination Detector  —  confidence score on every response             ║
║   < 0.50 confidence → retry next provider · never post junk                ║
╚═══════════════════════════════╤══════════════════════════════════════════════╝
                                │
           ┌────────────────────┼────────────────────────┐
           ▼                    ▼                        ▼
╔════════════════╗   ╔══════════════════════╗   ╔══════════════════════╗
║     Redis      ║   ║    GitHub REST API   ║   ║  Security Scanners   ║
║                ║   ║                      ║   ║                      ║
║  Idempotency   ║   ║  Issues · PRs        ║   ║  enhanced_secrets    ║
║  Circuit state ║   ║  Comments · Labels   ║   ║  35+ patterns        ║
║  Snapshots     ║   ║  Releases · Actions  ║   ║  Entropy gating      ║
║  Analytics     ║   ║  Security APIs       ║   ║  False-pos filter    ║
║  Rate limits   ║   ║  Collaborator perms  ║   ║  dependencies.py     ║
║  Budget track  ║   ╚══════════════════════╝   ║  scanner.py (CodeQL) ║
╚════════════════╝                              ╚══════════════════════╝
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

# 5. Test
python -m pytest -v   # 383 passing
```

Full setup guide → [`docs/guides/user-setup.md`](docs/guides/user-setup.md)

---

## 🤖 Slash Commands — 26 Total

Comment any command on a GitHub issue or PR to activate it.

### 🔧 Code Quality
| Command | What it does |
|---------|-------------|
| `/fix` | Root cause analysis + production-ready fix + verification test |
| `/autofix` | Creates fix branch · commits the fix · opens PR automatically |
| `/apply` | Auto-rewrites non-conventional commit messages across branch |
| `/improve` | Scored improvements — performance, security, readability, structure |
| `/refactor` | Structural refactor suggestions with before/after code |
| `/perf` | Time complexity analysis, N+1 detection, optimization suggestions |

### 🧠 Understanding
| Command | What it does |
|---------|-------------|
| `/explain` | Plain-English explanation: What · How · Why · Example · Pitfalls |
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
| `/runtests` | Triggers GitHub Actions workflow via workflow_dispatch |

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
| `/rollback` | Lists snapshots or restores repo state to a pre-bot snapshot |
| `/report` | Weekly analytics: PR velocity, issue resolution, quality grade |
| `/budget` | Live LLM token usage and cost per provider |
| `/notify` | Sends issue/PR to Discord with color-coded severity embed |

> 🔐 `/merge` `/rollback` `/release` `/autofix` `/secfull` require **write/maintain/admin** access.
> Rate limit: **10 commands per user per hour** per repo.

---

## 🔐 Security Model

```
Threat                    Mitigation
─────────────────────     ────────────────────────────────────────────────────
Forged webhooks           HMAC-SHA256 · fail closed on empty secret
                          RuntimeError at boot if secret not configured

Replay attacks            SHA-256 fingerprint + Redis SET NX (atomic)
                          Rejects events already seen within 1 hour

Webhook floods            IP rate limit: 100 req/min (Redis sliding window)
                          ThreadPoolExecutor cap: 6 workers, 50-job queue

Privilege escalation      Permission check before every restricted command
                          GitHub collaborator API · 5-min cache · fail closed

Prompt injection          Input sanitization · blocklist · 8,000 char limit

Secret leaks              35+ pattern scanner · entropy gating
                          Zero scannable literals in scanner source files

Bot feedback loops        sender.type == Bot · [bot] suffix · own-app set

Command spam              10 commands/user/hour · 150 AI calls/repo/day
```

---

## 📁 Project Structure

```
github-autopilot/
│
├── server.py                       # Entry point — webhook security + dispatch
├── .ai-repo-manager.yml            # Bot config — all 26 commands enabled
│
├── app/
│   ├── ai/
│   │   ├── router.py               # 4-provider LLM router + task classification
│   │   ├── circuit_breaker.py      # CLOSED/OPEN/HALF_OPEN per provider
│   │   ├── hallucination.py        # Confidence scoring before every post
│   │   └── providers/              # groq · gemini · openrouter
│   │
│   ├── core/
│   │   ├── authorization.py        # Command permission enforcement
│   │   ├── config.py               # YAML config loader (thread-safe cache)
│   │   ├── thread_pool.py          # Bounded ThreadPoolExecutor
│   │   ├── webhook_security.py     # Full webhook verification pipeline
│   │   ├── idempotency.py          # SHA-256 deduplication (Redis NX)
│   │   ├── analytics.py            # Usage tracking + /report
│   │   └── snapshot.py             # Repo snapshots + /rollback engine
│   │
│   ├── github/
│   │   ├── auth.py                 # JWT + installation token
│   │   ├── client.py               # HTTP + retry + backoff
│   │   └── notifications.py        # Discord/Slack embeds
│   │
│   ├── handlers/
│   │   ├── comments.py             # 26 slash commands dispatcher
│   │   ├── pull_request.py         # PR analysis + blast radius + review
│   │   ├── issues.py               # Triage + labels + welcome
│   │   ├── push.py                 # Secrets + deps + commit lint
│   │   ├── ci.py                   # CI failure analysis
│   │   └── autofix.py              # diff → branch → PR engine
│   │
│   ├── security/
│   │   ├── enhanced_secrets.py     # 35+ patterns · entropy · false-pos
│   │   ├── dependencies.py         # CVE scanner
│   │   └── scanner.py              # Dependabot + CodeQL APIs
│   │
│   └── intelligence/
│       ├── embeddings.py           # Code embeddings (local, no API)
│       └── retrieval.py            # Qdrant/ChromaDB vector search
│
├── docs/                           # Full technical documentation
│   ├── architecture/               # System design, webhook pipeline
│   ├── ai-system/                  # AI routing, hallucination, autofix
│   ├── security/                   # Threat model, secret scanning
│   ├── deployment/                 # Render setup, GitHub App config
│   ├── testing/                    # Test patterns, mocking guide
│   ├── observability/              # Health, metrics, logging
│   └── guides/                     # User setup guide
│
└── tests/                          # 383 tests · zero network calls
    ├── test_webhook_security.py    # 35 security tests
    ├── test_enhanced_secrets.py    # 26 scanner tests
    ├── test_push.py                # 25 tests (dedup regression)
    ├── test_pull_request.py        # 22 tests
    ├── test_issues.py              # 15 tests
    ├── test_ci.py                  # 18 tests
    └── ...12 more test files
```

---

## ⚙️ Environment Variables

| Variable | Required | Description |
|----------|:--------:|-------------|
| `GITHUB_APP_ID` | ✅ | Numeric App ID from GitHub App settings |
| `GITHUB_PRIVATE_KEY` | ✅ | Full PEM private key (including headers) |
| `GITHUB_WEBHOOK_SECRET` | ✅ | **App refuses to start without this** |
| `GROQ_API_KEY` | ✅ | Primary LLM — [console.groq.com](https://console.groq.com) (free) |
| `REDIS_URL` | ✅ | Redis connection string — Render provides this |
| `GEMINI_API_KEY` | ⚡ | Gemini Flash fallback — [aistudio.google.com](https://aistudio.google.com/app/apikey) |
| `OPENROUTER_API_KEY` | ⚡ | Emergency fallback — [openrouter.ai](https://openrouter.ai) |
| `DISCORD_WEBHOOK_URL` | 📢 | Discord notifications |
| `SLACK_WEBHOOK_URL` | 📢 | Slack notifications |
| `QDRANT_URL` | 🧠 | Vector DB for code context |
| `METRICS_AUTH_TOKEN` | 🔒 | Protects `/metrics` endpoint |
| `MAX_DISPATCH_WORKERS` | ⚙️ | Thread pool size (default: 6) |
| `REPO_DAILY_AI_LIMIT` | ⚙️ | Max AI calls per repo per day (default: 150) |

> ✅ Required &nbsp;·&nbsp; ⚡ Recommended &nbsp;·&nbsp; 📢 Optional &nbsp;·&nbsp; 🧠 Vector context &nbsp;·&nbsp; 🔒 Security &nbsp;·&nbsp; ⚙️ Tuning

---

## 🚀 Deploy to Render

```bash
# 1. Render → New Web Service → connect repo
#    Build:  pip install -r requirements.txt
#    Start:  gunicorn server:app --workers 2 --timeout 120 --bind 0.0.0.0:$PORT
#    Health: /health

# 2. Render → New → Redis (free) → set REDIS_URL

# 3. GitHub → Settings → Apps → New App
#    Webhook URL:   https://your-service.onrender.com/webhook
#    Permissions:   Contents R/W · Issues R/W · Pull requests R/W · Actions R/W
#    Events:        pull_request · issues · issue_comment · push · check_run
```

Full guide → [`docs/deployment/render-deploy.md`](docs/deployment/render-deploy.md)

---

## 🧪 Tests

```bash
python -m pytest -v                               # 383 passing
python -m pytest tests/test_push.py -v           # dedup regression
python -m pytest tests/test_webhook_security.py  # security pipeline
ruff check app/ --select E,F,W --ignore E501     # lint — matches CI
```

---

## 📊 Tech Stack

| Layer | Technology | Notes |
|-------|------------|-------|
| Web | Flask + Gunicorn | 2 workers · 120s timeout |
| Primary LLM | Groq Llama 3.3 70B | 5K req/day free |
| Fast LLM | Groq Llama 3.1 8B | 12K req/day free |
| Long context | Gemini Flash 1.5 | 1.5K req/day · 1M token ctx |
| Fallback | OpenRouter | 200 req/day free |
| State | Redis | Connection pool · FakeRedis fallback |
| Vector DB | Qdrant Cloud | 1GB free tier |
| Security | enhanced_secrets.py | 35+ patterns · entropy gating |
| Testing | pytest | 383 tests · zero network calls |
| Deploy | Render | Free tier |
| Lint | Ruff 0.8.0 | E, F, W rules |

---

## 📖 Documentation

| Document | What's inside |
|----------|--------------|
| [System Architecture](docs/architecture/system-architecture.md) | Components, request lifecycle, reliability model, tradeoffs |
| [Webhook Pipeline](docs/architecture/webhook-pipeline.md) | All 7 security stages with code and timing |
| [AI Routing](docs/ai-system/ai-routing.md) | Provider selection, circuit breakers, hallucination control |
| [Autofix Engine](docs/ai-system/autofix-engine.md) | Patch generation, safety guards, failure scenarios |
| [Threat Model](docs/security/threat-model.md) | 9 attack vectors, mitigations, residual risk |
| [User Setup Guide](docs/guides/user-setup.md) | Install, configure, first commands |
| [Testing Guide](docs/testing/testing-guide.md) | Patterns, mocking, known gotchas |
| [Observability](docs/observability/observability.md) | Health, metrics, Redis key reference |

---

## 📄 License

MIT — free to use, modify, distribute.

---

<div align="center">

**If this project helped you, a ⭐ means a lot.**

Built with ❤️ by [Shweta Mishra](https://github.com/Shweta-Mishra-ai)

[![GitHub stars](https://img.shields.io/github/stars/Shweta-Mishra-ai/github-autopilot?style=social)](https://github.com/Shweta-Mishra-ai/github-autopilot)
[![Follow](https://img.shields.io/github/followers/Shweta-Mishra-ai?label=Follow&style=social)](https://github.com/Shweta-Mishra-ai/github-autopilot)

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:06b6d4,50:8b5cf6,100:6366f1&height=120&section=footer" width="100%"/>

</div>
