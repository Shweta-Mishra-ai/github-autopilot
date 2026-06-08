# Contributing to AI Repo Manager

Thank you for your interest in contributing! This guide will help you get started quickly.

---

## Table of Contents

- [Getting Started](#getting-started)
- [Project Structure](#project-structure)
- [Development Setup](#development-setup)
- [Making Changes](#making-changes)
- [Commit Convention](#commit-convention)
- [Pull Request Process](#pull-request-process)
- [Running Tests](#running-tests)
- [Good First Issues](#good-first-issues)

---

## Getting Started

1. **Fork** the repository
2. **Clone** your fork
   ```bash
   git clone https://github.com/YOUR_USERNAME/github-autopilot.git
   cd github-autopilot
   ```
3. **Create a branch**
   ```bash
   git checkout -b feat/your-feature-name
   ```

---

## Project Structure

```
github-autopilot/
│
├── server.py                  # Webhook ingestion entry point
├── worker.py                  # Background event processor
├── Procfile                   # Process definitions
├── requirements.txt           # Python dependencies
├── .ai-repo-manager.yml       # Bot configuration
│
├── app/
│   ├── core/                  # Foundation layer — no side effects
│   │   ├── config.py          # YAML config loader
│   │   ├── confidence.py      # Per-action confidence scoring
│   │   ├── guardrails.py      # Deterministic safety checks
│   │   ├── idempotency.py     # Event deduplication
│   │   ├── logger.py          # Structured logging (structlog)
│   │   └── metrics.py         # In-memory counters
│   │
│   ├── queue/                 # Event queue layer
│   │   ├── producer.py        # Enqueue events
│   │   └── consumer.py        # Dequeue and yield events
│   │
│   ├── storage/               # Persistence layer
│   │   ├── events.py          # SQLite event log
│   │   └── fixtures.py        # Replay test fixtures
│   │
│   ├── security/              # Security scanning
│   │   ├── secrets.py         # Secret detection in diffs
│   │   └── dependencies.py    # Vulnerability scanning (OSV.dev)
│   │
│   ├── github/                # GitHub API layer
│   │   ├── auth.py            # JWT + installation tokens
│   │   ├── client.py          # HTTP client with retry/backoff
│   │   ├── rate_limit.py      # Rate limit tracking
│   │   └── notifications.py   # Slack/Discord alerts
│   │
│   ├── ai/                    # AI layer
│   │   ├── client.py          # Groq API + model fallback
│   │   └── validator.py       # JSON validation + sanitization
│   │
│   └── handlers/              # Event handlers
│       ├── pull_request.py    # PR analysis + code review
│       ├── issues.py          # Issue triage
│       ├── comments.py        # Slash commands
│       └── push.py            # Commit linting + secret scan
│
└── tests/
    ├── test_guardrails.py
    ├── test_validator.py
    └── test_idempotency.py
```

---

## Development Setup

### Prerequisites

- Python 3.11+
- A GitHub account
- A Groq API key — [console.groq.com](https://console.groq.com)

### Install dependencies

```bash
pip install -r requirements.txt
```

### Environment variables

Create a `.env` file in the root:

```env
GITHUB_APP_ID=your_app_id
GITHUB_PRIVATE_KEY=your_private_key_contents
GITHUB_WEBHOOK_SECRET=your_webhook_secret
GROQ_API_KEY=your_groq_api_key
```

### Run locally

```bash
# Terminal 1 — Web server
python server.py

# Terminal 2 — Worker
python worker.py
```

---

## Making Changes

### Layer rules

Each layer has strict boundaries. Please follow them:

| Layer | Rule |
|-------|------|
| `app/core/` | No Streamlit, no external API calls, no side effects |
| `app/github/` | Only GitHub API calls, no AI calls |
| `app/ai/` | Only Groq API calls, always validate responses |
| `app/handlers/` | Orchestrate only — delegate to core/github/ai |
| `app/security/` | Pure functions where possible, no GitHub API calls |

### Adding a new slash command

1. Add command name to `ALL_COMMANDS` in `app/handlers/comments.py`
2. Add routing in the `handle()` function
3. Implement `_cmd_yourcommand()` function
4. Add to `DEFAULTS["commands"]["enabled"]` in `app/core/config.py`
5. Add to `.ai-repo-manager.yml` commands list
6. Write a test in `tests/`

### Adding a new security scanner

1. Create `app/security/yourscanner.py`
2. Implement `scan_X(content: str) -> list[Finding]`
3. Implement `format_findings(findings: list) -> str`
4. Hook into `app/handlers/push.py`

---

## Commit Convention

All commits must follow [Conventional Commits](https://www.conventionalcommits.org/):

```
type(scope): description
```

### Valid types

| Type | When to use |
|------|-------------|
| `feat` | New feature or command |
| `fix` | Bug fix |
| `docs` | Documentation only |
| `refactor` | Code restructure, no behavior change |
| `test` | Adding or updating tests |
| `chore` | Dependencies, config, tooling |
| `perf` | Performance improvement |
| `ci` | CI/CD changes |
| `security` | Security fix or scanner |

### Examples

```bash
feat(commands): add /changelog slash command
fix(push): resolve short SHA lookup in /apply
docs(readme): update setup instructions
test(security): add secret detection unit tests
security(secrets): add Groq API key pattern
```

---

## Pull Request Process

1. **Branch** from `main` with a descriptive name
2. **Write tests** for new functionality
3. **Run tests** locally before pushing
   ```bash
   pytest
   ```
4. **Fill out** the PR description template
5. **Request review** — the bot will automatically review your PR!

### PR checklist

- [ ] Tests pass locally
- [ ] New feature has tests
- [ ] Commit messages follow convention
- [ ] No secrets or API keys in code
- [ ] Layer boundaries respected

---

## Running Tests

```bash
# Run all tests
pytest

# Run specific module
pytest tests/test_guardrails.py -v
pytest tests/test_validator.py -v
pytest tests/test_idempotency.py -v

# Run with coverage
pytest --cov=app tests/
```

Tests run in full isolation — no network access required.

---

## Good First Issues

Look for issues labeled **`good first issue`** — these are well-scoped tasks perfect for first-time contributors:

- Adding a new secret detection pattern to `app/security/secrets.py`
- Adding a new slash command
- Improving AI prompts in any handler
- Adding tests for untested functions
- Improving error messages

---

## Questions?

Open an issue or start a discussion — contributions of all kinds are welcome!

Built by [Shweta Mishra](https://github.com/Shweta-Mishra-ai)

