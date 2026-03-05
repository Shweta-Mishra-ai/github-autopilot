# AI Repo Manager

**Production-grade GitHub automation powered by large language models.**

Installs as a GitHub App. Processes webhook events in real time — no polling, no setup beyond configuration. Handles PR analysis, code review, issue triage, and repository health monitoring automatically.

[![Version](https://img.shields.io/badge/version-2.0.0-0066cc)](https://github.com/Shweta-Mishra-ai/github-autopilot)
[![Server](https://img.shields.io/badge/server-live-46E3B7?logo=render&logoColor=white)](https://github-autopilot-1.onrender.com)
[![GitHub App](https://img.shields.io/badge/GitHub%20App-ai--repo--manager-181717?logo=github)](https://github.com/apps/ai-repo-manager)
[![Model](https://img.shields.io/badge/model-Llama%203.3%2070B-F55036)](https://console.groq.com)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Made by](https://img.shields.io/badge/author-Shweta%20Mishra-ff69b4)](https://github.com/Shweta-Mishra-ai)

---

## Overview

| Resource | Link |
|----------|------|
| Live Server | [github-autopilot-1.onrender.com](https://github-autopilot-1.onrender.com) |
| GitHub App | [github.com/apps/ai-repo-manager](https://github.com/apps/ai-repo-manager) |
| Repository | [Shweta-Mishra-ai/github-autopilot](https://github.com/Shweta-Mishra-ai/github-autopilot) |

---

## What It Does

The system listens to GitHub webhook events and responds deterministically. Every automated action passes through guardrail checks before execution. AI responses are validated and sanitized before use.

| Capability | Behavior |
|------------|----------|
| **PR Analysis** | Rewrites titles to conventional commit format, generates structured descriptions, assigns risk level (low/medium/high), identifies review focus areas |
| **AI Code Review** | Scores each changed file 0–10, categorizes issues by severity (critical/major/minor/nit), suggests exact fixes |
| **Issue Triage** | Classifies by type and priority, applies labels, posts contextual acknowledgment, asks clarifying questions when needed |
| **Repo Health** | Grades repository A+ to F across six dimensions with actionable recommendations |
| **Commit Linting** | Detects non-conventional commits on push to protected branches, creates a structured alert |
| **Slash Commands** | Nine commands available in any PR or issue comment, processed within seconds |

---

## Commands

| Command | Description |
|---------|-------------|
| `/fix` | Root cause analysis, working code fix, and verification test |
| `/explain` | Plain-English explanation of code or error |
| `/improve` | Concrete improvements with before/after examples |
| `/test` | Test suite generation (pytest / jest / unittest) |
| `/docs` | Docstrings, usage examples, README sections |
| `/refactor` | Structural improvements with identical behavior guaranteed |
| `/health` | Repository health report graded A+ to F |
| `/version` | Tag history, release status, semantic versioning guide |
| `/merge` | Merge PR after all guardrail conditions are satisfied |

---

## Architecture

V2 is a modular four-layer system. Each layer has a single responsibility and communicates through well-defined interfaces.

```
ai-repo-manager/
│
├── server.py                    # Entry point — routing, signature verification, dispatch
├── .ai-repo-manager.yml         # Repo-level configuration
│
└── app/
    ├── core/                    # Foundation — no side effects, no external calls
    │   ├── config.py            # YAML config loader with safe defaults
    │   ├── guardrails.py        # Deterministic safety checks before every auto-action
    │   ├── idempotency.py       # SHA-256 event fingerprinting, TTL-based deduplication
    │   └── logger.py            # Structured logging with event context and timing
    │
    ├── github/                  # GitHub API layer
    │   ├── auth.py              # JWT generation, installation token caching
    │   ├── client.py            # HTTP client with retry, backoff, error classification
    │   └── rate_limit.py        # Header-based rate limit tracking with auto-wait
    │
    ├── ai/                      # AI layer
    │   ├── client.py            # Groq API calls, model fallback, timeout handling
    │   └── validator.py         # JSON schema validation and sanitization
    │
    └── handlers/                # Event handlers — one module per event type
        ├── pull_request.py
        ├── issues.py
        ├── comments.py
        └── push.py
```

### Design Decisions

**Idempotency** — Every webhook event is fingerprinted using SHA-256 over delivery ID, event type, action, and resource number. Duplicate deliveries are detected and dropped before any processing begins.

**Guardrails** — No automated action (merge, label, title update) executes without passing a deterministic rule check first. Guardrails are pure functions with no AI dependency — they cannot hallucinate.

**AI Validation** — Every AI response is parsed, type-checked, and sanitized before use. Invalid fields fall back to safe defaults. The system never crashes on a malformed AI response.

**Retry Strategy** — GitHub API calls use per-status-code retry logic. 5xx errors retry with exponential backoff. 429 responses respect the `Retry-After` header. 4xx client errors fail immediately without retry.

**Model Fallback** — Primary model is Llama 3.3 70B. On rate limit, the system automatically falls back to Llama 3.1 8B and continues processing.

---

## V1 → V2 Changes

| V1 | V2 |
|----|-----|
| Single monolithic file | Four-layer modular architecture |
| Raw AI JSON used directly | Full schema validation on every response |
| No duplicate event protection | SHA-256 fingerprint deduplication |
| No safety checks before actions | Seven deterministic guardrails |
| No retry on API failures | Exponential backoff on all external calls |
| No rate limit awareness | Header-based tracker with automatic wait |
| Print-based debugging | Structured logging with context and timing |
| No configuration system | Per-repo YAML configuration with safe defaults |
| `auto_merge` on by default | Disabled by default, opt-in with explicit config |

---

## Configuration

Place `.ai-repo-manager.yml` in your repository root to override defaults:

```yaml
pull_requests:
  auto_polish_title: true
  auto_fill_description: true
  code_review: true
  max_files_reviewed: 4

issues:
  auto_triage: true
  auto_label: true

auto_merge:
  enabled: false
  require_passing_checks: true
  require_no_blocking_reviews: true
  allow_protected_branches: false
  allowed_risk_levels:
    - low

push:
  enforce_conventional_commits: true
  create_issue_threshold: 3

commands:
  enabled:
    - fix
    - explain
    - improve
    - test
    - docs
    - refactor
    - health
    - version
    - merge
```

All keys are optional. The system applies safe defaults when the file is absent or a key is missing.

---

## Setup

### Prerequisites

- A GitHub account with permission to create GitHub Apps
- A deployment target (Render, Railway, Fly.io, or any platform that runs Python)
- A Groq API key — available at [console.groq.com](https://console.groq.com)

### 1. Deploy the Server

Fork this repository and deploy it as a web service. The expected start command is:

```bash
gunicorn server:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120
```

The `/` endpoint returns a health check response. The `/webhook` endpoint receives GitHub events.

### 2. Create a GitHub App

Navigate to [github.com/settings/apps/new](https://github.com/settings/apps/new) and configure:

- **Webhook URL:** `https://YOUR-SERVER-URL/webhook`
- **Webhook Secret:** A secret string — store it securely
- **Permissions:** Contents (Read), Issues (Read/Write), Pull requests (Read/Write), Metadata (Read)
- **Events:** Pull request, Issues, Issue comment, Push

Generate and download a private key from the app settings page.

### 3. Set Environment Variables

| Variable | Description |
|----------|-------------|
| `GITHUB_APP_ID` | Numeric App ID shown on the GitHub App settings page |
| `GITHUB_PRIVATE_KEY` | Full contents of the downloaded `.pem` file |
| `GITHUB_WEBHOOK_SECRET` | The webhook secret set during app creation |
| `GROQ_API_KEY` | API key from console.groq.com |

### 4. Install the App

GitHub App settings → **Install App** → select target repositories → **Install**.

The system begins processing events immediately on installation.

---

## Tech Stack

| Component | Technology |
|-----------|------------|
| Runtime | Python 3.14 |
| Web framework | Flask + Gunicorn |
| Primary AI model | Llama 3.3 70B via Groq API |
| Fallback AI model | Llama 3.1 8B via Groq API |
| Authentication | GitHub App JWT + Installation Tokens |
| Configuration | YAML |

---

## Self-Hosting

This system is designed to be self-hosted. Each installation operates independently with its own GitHub App credentials and AI API key. There is no central server, no telemetry, and no shared infrastructure.

To run your own instance:

1. Fork the repository
2. Deploy the server to any Python-compatible platform
3. Create a GitHub App under your own account
4. Configure environment variables
5. Install the app on your repositories

---

## Contributing

Contributions are welcome. Please follow conventional commit format for all commits:

```
feat: add new capability
fix: correct specific behavior
docs: update documentation
refactor: restructure without behavior change
test: add or update tests
```

---

## License

MIT — see [LICENSE](LICENSE) for details.

---

*Built by [Shweta Mishra](https://github.com/Shweta-Mishra-ai)*
