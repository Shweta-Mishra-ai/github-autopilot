# AI Repo Manager V3

Production-grade GitHub automation platform powered by large language models.

Installs as a GitHub App. Processes webhook events asynchronously — no polling, no setup beyond configuration. Handles PR analysis, code review, issue triage, security scanning, and repository health monitoring automatically.

![version](https://img.shields.io/badge/version-3.0.0-blue)
![server](https://img.shields.io/badge/server-live-brightgreen)
![GitHub App](https://img.shields.io/badge/GitHub-App-black)
![model](https://img.shields.io/badge/model-Llama%203.3%2070B-orange)
![license](https://img.shields.io/badge/license-MIT-green)
![tests](https://img.shields.io/badge/tests-60%2B%20passing-brightgreen)
![author](https://img.shields.io/badge/author-Shweta%20Mishra-purple)

---

## Overview

| Resource | Link |
|----------|------|
| Live Server | [github-autopilot-1.onrender.com](https://github-autopilot-1.onrender.com) |
| GitHub App | [github.com/apps/ai-repo-manager](https://github.com/apps/ai-repo-manager) |
| Repository | [Shweta-Mishra-ai/github-autopilot](https://github.com/Shweta-Mishra-ai/github-autopilot) |

---

## What's New in V3

| Feature | Description |
|---------|-------------|
| 🧠 Embedding-based context | ChromaDB vector store — AI reviews code with full repo awareness |
| 🔒 Secret detection | Scans every push diff for API keys, tokens, private keys |
| 📦 Dependency scanning | Checks requirements.txt against OSV.dev vulnerability database |
| ⚖️ License compliance | Flags copyleft licenses in permissive projects |
| 🎯 Confidence scoring | AI actions blocked below configurable confidence threshold |
| ⚡ Queue-based processing | Redis Streams with in-memory fallback — no dropped webhooks |
| 📊 Structured logging | structlog JSON logs — queryable and machine-readable |
| 🗓️ Scheduled tasks | Auto stale detection, monthly health reports, weekly dep reports |
| 💬 Slack/Discord alerts | Critical events posted to your team channel |
| 🔁 Replay fixtures | Capture real webhooks as test fixtures for regression testing |

---

## Capabilities

| Capability | Behavior |
|------------|----------|
| PR Analysis | Rewrites titles to conventional format, generates descriptions, assigns risk level, uses repo context |
| AI Code Review | Scores each file 0–10, categorizes issues by severity, suggests exact fixes with codebase awareness |
| Issue Triage | Classifies by type and priority, applies labels, asks clarifying questions |
| Repo Health | Grades A+ to F across six dimensions with actionable recommendations |
| Commit Linting | Detects non-conventional commits on push, creates structured alert |
| Secret Detection | Scans push diffs for API keys, tokens, private keys — alerts immediately |
| Dependency Scanning | Weekly vulnerability scan via OSV.dev — no API key required |
| Slash Commands | 15 commands in any PR or issue comment |
| Scheduled Tasks | Stale issues, health reports, dependency reports on cron |

---

## Slash Commands

| Command | Description |
|---------|-------------|
| `/fix` | Root cause analysis, working code fix, verification test |
| `/apply` | Auto-fix non-conventional commit messages |
| `/explain` | Plain-English explanation of code or error |
| `/improve` | Concrete improvements with before/after examples |
| `/test` | Test suite generation (pytest / jest / unittest) |
| `/docs` | Docstrings, usage examples, README sections |
| `/refactor` | Structural improvements with identical behavior guaranteed |
| `/health` | Repository health report graded A+ to F |
| `/version` | Tag history, release status, semantic versioning guide |
| `/merge` | Merge PR after all guardrail conditions are satisfied |
| `/summarize` | Summarize long PR or issue discussion thread |
| `/ci` | Analyze CI failure logs — root cause + fix |
| `/security` | Run security scan on PR changed files |
| `/gaps` | Detect test coverage gaps in changed code |
| `/changelog` | Generate CHANGELOG entry from recent commits |

---

## Architecture

V3 adopts a queue-based architecture to decouple webhook ingestion from event processing.

```
github-autopilot/
│
├── server.py                    # Webhook ingestion only — enqueues events
├── worker.py                    # Event processor + APScheduler
├── Procfile                     # web + worker processes
│
├── app/
│   ├── core/                    # Foundation — no side effects
│   │   ├── config.py            # YAML config with safe defaults
│   │   ├── confidence.py        # Per-action confidence scoring (NEW)
│   │   ├── guardrails.py        # Deterministic safety checks
│   │   ├── idempotency.py       # SHA-256 event deduplication
│   │   ├── logger.py            # Structured logging (structlog)
│   │   └── metrics.py           # In-memory counters
│   │
│   ├── queue/                   # Event queue (NEW)
│   │   ├── producer.py          # Enqueue webhook events
│   │   └── consumer.py          # Dequeue and yield events
│   │
│   ├── storage/                 # Persistence (NEW)
│   │   ├── events.py            # SQLite event log
│   │   └── fixtures.py          # Replay test fixtures
│   │
│   ├── intelligence/            # AI context layer (NEW)
│   │   ├── embeddings.py        # Code embedding via sentence-transformers
│   │   ├── retrieval.py         # ChromaDB vector retrieval
│   │   └── summarizer.py        # PR/discussion summarization
│   │
│   ├── security/                # Security scanning (NEW)
│   │   ├── secrets.py           # Secret detection in diffs
│   │   ├── dependencies.py      # Vulnerability scanning (OSV.dev)
│   │   └── licenses.py          # License compliance
│   │
│   ├── github/
│   │   ├── auth.py              # JWT + installation tokens
│   │   ├── client.py            # HTTP client with retry/backoff
│   │   ├── rate_limit.py        # Rate limit tracking
│   │   └── notifications.py     # Slack/Discord alerts (NEW)
│   │
│   ├── ai/
│   │   ├── client.py            # Groq API + model fallback
│   │   └── validator.py         # JSON validation + sanitization
│   │
│   └── handlers/
│       ├── pull_request.py      # PR analysis + code review
│       ├── issues.py            # Issue triage
│       ├── comments.py          # 15 slash commands
│       ├── push.py              # Commit linting + security scan + indexing
│       └── schedule.py          # Scheduled maintenance tasks (NEW)
│
└── tests/                       # 60+ tests — no network required
    ├── test_guardrails.py
    ├── test_validator.py
    ├── test_idempotency.py
    ├── test_confidence.py       # NEW
    ├── test_secrets.py          # NEW
    ├── test_queue.py            # NEW
    └── test_storage.py          # NEW
```

---

## Version History

| Version | Changes |
|---------|---------|
| V1 | Single monolithic file, no retry, no validation, no guardrails |
| V2 | Modular four-layer architecture, guardrails, idempotency, AI validation |
| V2.1 | Async webhook dispatch, metrics endpoint, 40+ tests |
| V3 | Queue-based processing, embeddings, security scanning, confidence scoring, 15 slash commands, scheduled tasks, 60+ tests |

---

## Configuration

Place `.ai-repo-manager.yml` in your repository root:

```yaml
push:
  enforce_conventional_commits: true
  scan_secrets: true
  scan_dependencies: true

confidence:
  thresholds:
    pr_title_rewrite: 0.85
    auto_merge: 0.95
    fix_command: 0.70

commands:
  enabled:
    - fix
    - apply
    - explain
    - improve
    - test
    - docs
    - refactor
    - health
    - version
    - merge
    - summarize
    - ci
    - security
    - gaps
    - changelog
```

All keys are optional — safe defaults applied when missing.

---

## Setup

### Prerequisites
- GitHub account with permission to create GitHub Apps
- Deployment target (Render, Railway, Fly.io)
- Groq API key — [console.groq.com](https://console.groq.com)

### 1. Deploy the Server

```bash
gunicorn server:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120
```

### 2. Create a GitHub App

Navigate to `github.com/settings/apps/new`:

- **Webhook URL:** `https://YOUR-SERVER-URL/webhook`
- **Permissions:** Contents (Read), Issues (Read/Write), Pull requests (Read/Write), Metadata (Read)
- **Events:** Pull request, Issues, Issue comment, Push

### 3. Environment Variables

| Variable | Description |
|----------|-------------|
| `GITHUB_APP_ID` | Numeric App ID |
| `GITHUB_PRIVATE_KEY` | Contents of `.pem` file |
| `GITHUB_WEBHOOK_SECRET` | Webhook secret |
| `GROQ_API_KEY` | Groq API key |
| `REDIS_URL` | Optional — Redis for production queue |
| `SLACK_WEBHOOK_URL` | Optional — Slack alerts |
| `DISCORD_WEBHOOK_URL` | Optional — Discord alerts |
| `SCHEDULED_REPO` | Optional — repo for scheduled tasks |
| `SCHEDULED_INSTALLATION_ID` | Optional — installation ID for scheduler |

### 4. Install the App

GitHub App settings → Install App → select repositories → Install.

---

## Testing

```bash
# Run all tests
pytest

# Run specific modules
pytest tests/test_guardrails.py -v
pytest tests/test_secrets.py -v
pytest tests/test_confidence.py -v

# With coverage
pytest --cov=app tests/
```

---

## Tech Stack

| Component | Technology |
|-----------|------------|
| Runtime | Python 3.11+ |
| Web framework | Flask + Gunicorn |
| Event queue | Redis Streams / in-memory fallback |
| Vector store | ChromaDB (local) |
| Embeddings | sentence-transformers |
| Logging | structlog |
| Scheduling | APScheduler |
| Primary AI | Llama 3.3 70B via Groq |
| Fallback AI | Llama 3.1 8B via Groq |
| Testing | pytest |

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup guide, layer rules, and commit conventions.

---

## Support

If this project saved you time or taught you something useful, consider giving it a star.

[![Star on GitHub](https://img.shields.io/github/stars/Shweta-Mishra-ai/github-autopilot?style=social)](https://github.com/Shweta-Mishra-ai/github-autopilot)

---

## License

MIT License — free to use, modify, and distribute.

Built by [Shweta Mishra](https://github.com/Shweta-Mishra-ai)
