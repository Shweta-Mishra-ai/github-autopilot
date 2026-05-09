# AI Repo Manager V4

> Production-grade GitHub automation bot. Installs as a GitHub App — no polling, no setup beyond configuration.
> Handles PR analysis, code review, issue triage, auto-fix, security scanning, analytics, and repository health monitoring.

![version](https://img.shields.io/badge/version-4.0.0-blue)
![server](https://img.shields.io/badge/server-live-brightgreen)
![tests](https://img.shields.io/badge/tests-245%2B%20passing-brightgreen)
![GitHub App](https://img.shields.io/badge/GitHub-App-black)
![model](https://img.shields.io/badge/LLM-Groq%20%7C%20Gemini%20%7C%20OpenRouter-orange)
![license](https://img.shields.io/badge/license-MIT-green)
![author](https://img.shields.io/badge/author-Shweta%20Mishra-purple)

---

## Quick Links

| Resource | Link |
|----------|------|
| 🚀 Live Server | [github-autopilot-1.onrender.com](https://github-autopilot-1.onrender.com) |
| 🤖 GitHub App | [github.com/apps/ai-repo-manager](https://github.com/apps/ai-repo-manager) |
| 📦 Repository | [Shweta-Mishra-ai/github-autopilot](https://github.com/Shweta-Mishra-ai/github-autopilot) |
| 📊 Health Check | [github-autopilot-1.onrender.com/health](https://github-autopilot-1.onrender.com/health) |

---

## What V4 Adds Over V3

| Feature | Description |
|---------|-------------|
| 🧠 Multi-Provider LLM | Groq 70B → Groq 8B → Gemini Flash → OpenRouter fallback chain |
| 🔁 Auto-Fallback | Circuit breakers per provider — zero downtime when one fails |
| 🤖 Hallucination Detection | Validates AI responses before posting to GitHub |
| 📸 Snapshot & Rollback | `/rollback` restores repo state before bot actions |
| 🔧 Auto-Fix Engine | `/autofix` creates fix branch + PR automatically |
| 💥 Blast Radius | `/impact` shows which layers a PR affects |
| 📊 Analytics & Reporting | `/report` — PR velocity, issue resolution, code quality grade |
| 🔒 GitHub Security APIs | Dependabot + CodeQL + Secret Scanning via `/secfull` |
| 🔔 Rich Notifications | Color-coded Discord embeds with severity levels |
| 💾 API Caching | Redis-backed GitHub API cache — 3-5x fewer API calls |
| 🧵 Conversation Context | Bot remembers previous commands on same issue |
| 🛡️ Injection Prevention | Input sanitization on every LLM call |
| ♻️ Retry + Backoff | Auto-retry on GitHub 5xx errors |
| 🐛 Bot Spam Prevention | Dedup — bot never posts twice on same PR |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        GitHub Webhook                           │
│                  POST /webhook (signature verified)             │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                      server.py (Flask)                          │
│  • Signature verification (HMAC SHA-256)                       │
│  • IP rate limiting (100 req/min)                              │
│  • Idempotency check (SHA-256 fingerprint)                     │
│  • Dispatch to background thread (< 50ms ack)                  │
└──────────────┬──────────────────────────────────────────────────┘
               │ Thread (daemon=False)
               ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Event Handlers                             │
│                                                                 │
│  pull_request.py  →  PR analysis, blast radius, test gaps      │
│  issues.py        →  Issue triage, labeling, welcome           │
│  comments.py      →  22 slash commands                         │
│  push.py          →  Commit lint, dep scan, secret scan        │
│  ci.py            →  CI failure analysis                       │
└──────────────┬──────────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────────┐
│                       AI Router                                 │
│                                                                 │
│  Task Classification  →  fast / standard / deep / long        │
│                                                                 │
│  Provider Chain:                                                │
│  1. Groq Llama 70B   (primary — best quality)                  │
│  2. Groq Llama 8B    (fast tasks)                              │
│  3. Gemini Flash     (long context — up to 1M tokens)          │
│  4. OpenRouter       (emergency free fallback)                  │
│                                                                 │
│  Circuit Breaker per provider — auto skip if unhealthy         │
│  Safety sanitizer — injection detection before every call      │
│  Hallucination detector — validates response quality           │
└──────────────┬──────────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Redis (Free Tier)                            │
│                                                                 │
│  • Idempotency keys      • Circuit breaker state               │
│  • Snapshot storage      • Conversation context                │
│  • Analytics buckets     • API response cache                  │
│  • Rate limit tracking   • Budget tracking                     │
└─────────────────────────────────────────────────────────────────┘
```

---

## Project Structure

```
github-autopilot/
│
├── server.py                      # Webhook receiver + thread dispatcher
├── Procfile                       # gunicorn single-service config
├── render.yaml                    # Render deployment (web + Redis)
├── requirements.txt               # All dependencies
│
├── app/
│   │
│   ├── ai/                        # LLM Layer
│   │   ├── router.py              # Smart task router (4 providers)
│   │   ├── circuit_breaker.py     # Per-provider circuit breakers
│   │   ├── hallucination.py       # Response quality validator
│   │   ├── metrics.py             # Usage + cost tracking
│   │   ├── validator.py           # JSON schema validation
│   │   └── providers/
│   │       ├── base.py            # Abstract LLMProvider class
│   │       ├── groq.py            # Groq Llama 70B + 8B
│   │       ├── gemini.py          # Google Gemini Flash
│   │       └── openrouter.py      # OpenRouter free fallback
│   │
│   ├── core/                      # Foundation (no side effects)
│   │   ├── config.py              # YAML config with safe defaults
│   │   ├── analytics.py           # PR/issue/bot usage tracking
│   │   ├── cache.py               # Redis API response cache
│   │   ├── confidence.py          # Per-action confidence scoring
│   │   ├── context_manager.py     # Conversation context (Redis)
│   │   ├── guardrails.py          # Deterministic safety checks
│   │   ├── health_check.py        # System health + degraded mode
│   │   ├── idempotency.py         # SHA-256 event deduplication
│   │   ├── logger.py              # Structured event logging
│   │   ├── metrics.py             # In-memory counters
│   │   ├── safe_import.py         # Defensive import wrapper
│   │   └── snapshot.py            # Repo snapshot + rollback
│   │
│   ├── github/                    # GitHub API Layer
│   │   ├── auth.py                # JWT + installation tokens
│   │   ├── client.py              # HTTP client (retry + backoff)
│   │   ├── notifications.py       # Discord/Slack rich embeds
│   │   └── rate_limit.py          # Rate limit tracking + wait
│   │
│   ├── handlers/                  # Event Handlers
│   │   ├── autofix.py             # Auto-fix engine (creates PRs)
│   │   ├── ci.py                  # CI failure handler
│   │   ├── comments.py            # 22 slash commands
│   │   ├── issues.py              # Issue triage
│   │   ├── pull_request.py        # PR analysis + blast radius
│   │   ├── push.py                # Commit lint + security scan
│   │   └── schedule.py            # Scheduled tasks (weekly digest)
│   │
│   ├── intelligence/              # Vector Context Layer
│   │   ├── embeddings.py          # Code embedding (sentence-transformers)
│   │   ├── retrieval.py           # Qdrant/ChromaDB vector search
│   │   └── summarizer.py          # PR/issue summarization
│   │
│   ├── security/                  # Security Layer
│   │   ├── dependencies.py        # CVE vulnerability scanner
│   │   ├── licenses.py            # License compliance
│   │   ├── scanner.py             # GitHub Security APIs
│   │   └── secrets.py             # Secret detection in diffs
│   │
│   ├── queue/                     # Event Queue
│   │   ├── producer.py            # Enqueue webhook events
│   │   └── consumer.py            # Dequeue and process
│   │
│   └── storage/                   # Persistence
│       ├── events.py              # SQLite event log
│       └── fixtures.py            # Test fixture capture/replay
│
└── tests/                         # 245+ tests (no network required)
    ├── test_analytics.py
    ├── test_autofix.py
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

## Slash Commands (22 Total)

| Command | Description |
|---------|-------------|
| `/fix` | Root cause + production-ready fix + test |
| `/autofix` | Create fix branch + PR automatically |
| `/apply` | Auto-fix non-conventional commit messages |
| `/explain` | 5-section explanation: What/How/Why/Example/Pitfalls |
| `/improve` | Scored improvements with priority and before/after |
| `/test` | Generate pytest/jest test suite |
| `/docs` | Generate docstrings + README sections |
| `/refactor` | Structural improvements (behavior unchanged) |
| `/health` | Repo health graded A–F with recommendations |
| `/version` | Tag history + semantic versioning guide |
| `/merge` | Merge PR after guardrail conditions pass |
| `/summarize` | Summarize long PR/issue thread |
| `/ci` | Analyze CI failure — root cause + fix |
| `/security` | Security scan on PR changed files |
| `/secfull` | Full report: Dependabot + CodeQL + Secret Scanning |
| `/gaps` | Detect test coverage gaps in changed code |
| `/changelog` | Generate CHANGELOG entry from commits |
| `/budget` | Live LLM usage + cost per provider |
| `/rollback` | Show snapshots / restore repo state |
| `/impact` | Blast radius: which layers this PR affects |
| `/report` | Weekly analytics: PR velocity, quality grade |
| `/notify` | Send issue/PR to Discord |

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_APP_ID` | ✅ | Numeric App ID from GitHub App settings |
| `GITHUB_PRIVATE_KEY` | ✅ | Contents of `.pem` private key file |
| `GITHUB_WEBHOOK_SECRET` | ✅ | Webhook secret (set in GitHub App) |
| `GROQ_API_KEY` | ✅ | Primary LLM — [console.groq.com](https://console.groq.com) (free) |
| `REDIS_URL` | ✅ | Redis connection string (Render provides this) |
| `GEMINI_API_KEY` | ⚡ | Gemini Flash fallback — [aistudio.google.com](https://aistudio.google.com/app/apikey) (free) |
| `OPENROUTER_API_KEY` | ⚡ | Emergency fallback — [openrouter.ai](https://openrouter.ai) (free tier) |
| `DISCORD_WEBHOOK_URL` | 📢 | Discord notifications |
| `SLACK_WEBHOOK_URL` | 📢 | Slack notifications |
| `QDRANT_URL` | 🧠 | Vector DB for code context — [qdrant.tech](https://qdrant.tech) (free tier) |
| `QDRANT_API_KEY` | 🧠 | Qdrant Cloud API key |
| `METRICS_AUTH_TOKEN` | 🔒 | Protects `/metrics` endpoint |
| `REPO_DAILY_AI_LIMIT` | ⚙️ | Max AI calls per repo per day (default: 150) |

> ✅ Required · ⚡ Recommended · 📢 Optional · 🧠 Optional (enables vector context) · 🔒 Optional

---

## Local Setup

### Prerequisites
- Python 3.11+
- Redis (local or cloud)
- GitHub App credentials

### Step 1 — Clone & Install

```bash
git clone https://github.com/Shweta-Mishra-ai/github-autopilot.git
cd github-autopilot
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Step 2 — Environment

```bash
cp .env.example .env
# Edit .env with your credentials
```

`.env` file:
```env
GITHUB_APP_ID=your_app_id
GITHUB_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
GITHUB_WEBHOOK_SECRET=your_webhook_secret
GROQ_API_KEY=gsk_...
REDIS_URL=redis://localhost:6379/0
GEMINI_API_KEY=AIza...
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

### Step 3 — Run

```bash
# Development
flask --app server run --port 5000

# Production (same as Render)
gunicorn server:app --workers 2 --timeout 120 --bind 0.0.0.0:5000
```

### Step 4 — Create GitHub App

Go to [github.com/settings/apps/new](https://github.com/settings/apps/new):

| Setting | Value |
|---------|-------|
| **Webhook URL** | `https://YOUR-SERVER/webhook` |
| **Webhook Secret** | Your `GITHUB_WEBHOOK_SECRET` |
| **Permissions** | Contents (R/W), Issues (R/W), Pull requests (R/W), Metadata (R) |
| **Events** | Pull request, Issues, Issue comment, Push, Check run |

Download the private key → paste into `GITHUB_PRIVATE_KEY`.

### Step 5 — Install App

GitHub App → Install App → select your repositories → Install.

---

## Deploy to Render (Free Tier)

```bash
# 1. Fork/push repo to GitHub
# 2. Connect to Render: render.com → New Web Service → from GitHub

# 3. Settings:
#    Build Command: pip install -r requirements.txt
#    Start Command: gunicorn server:app --workers 2 --timeout 120 --bind 0.0.0.0:$PORT
#    Health Check:  /health

# 4. Add Redis:
#    Render Dashboard → New → Redis (free tier)
#    Copy connection string → REDIS_URL env var

# 5. Add all env vars in Render Dashboard → Environment
```

---

## Configuration File

Place `.ai-repo-manager.yml` in your repo root:

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
  create_issue_threshold: 3

confidence:
  thresholds:
    pr_title_rewrite: 0.85
    auto_merge: 0.95
    fix_command: 0.70

issues:
  enabled: true
  auto_label: true

commands:
  enabled:
    - fix
    - autofix
    - explain
    - improve
    - test
    - docs
    - refactor
    - health
    - merge
    - summarize
    - ci
    - security
    - secfull
    - gaps
    - changelog
    - budget
    - rollback
    - impact
    - report
    - notify
```

---

## Running Tests

```bash
# All tests (245+)
python -m pytest -v

# Specific modules
python -m pytest tests/test_router.py -v
python -m pytest tests/test_providers.py -v
python -m pytest tests/test_autofix.py -v
python -m pytest tests/test_analytics.py -v

# With coverage report
python -m pytest --cov=app --cov-report=term-missing tests/
```

---

## Tech Stack

| Component | Technology | Notes |
|-----------|------------|-------|
| Runtime | Python 3.11+ | |
| Web Framework | Flask + Gunicorn | |
| Primary LLM | Groq Llama 3.3 70B | Free tier: 5K req/day |
| Fast LLM | Groq Llama 3.1 8B | Free tier: 12K req/day |
| Long Context | Gemini Flash 1.5 | Free tier: 1.5K req/day |
| Emergency LLM | OpenRouter (free models) | Free tier: 200 req/day |
| Cache/State | Redis | Free tier on Render |
| Vector DB | Qdrant Cloud | Free tier: 1GB |
| Embeddings | sentence-transformers | local, no API |
| Scheduling | APScheduler | in-process |
| Testing | pytest | 245+ tests |
| Deployment | Render | free tier |

---

## Version History

| Version | Key Changes |
|---------|-------------|
| **V4.0** | Multi-provider LLM router, hallucination detection, snapshot/rollback, auto-fix engine, analytics, API caching, 22 slash commands, 245+ tests |
| V3.0 | Queue-based processing, embeddings, security scanning, 15 slash commands, 60+ tests |
| V2.1 | Async dispatch, metrics endpoint, 40+ tests |
| V2.0 | Modular architecture, guardrails, idempotency |
| V1.0 | Single file, basic PR analysis |

---

## License

MIT — free to use, modify, distribute.

Built by [Shweta Mishra](https://github.com/Shweta-Mishra-ai)

[![Star on GitHub](https://img.shields.io/github/stars/Shweta-Mishra-ai/github-autopilot?style=social)](https://github.com/Shweta-Mishra-ai/github-autopilot)