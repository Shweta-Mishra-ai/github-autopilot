# Contributing to GitHub Autopilot

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
- [Licensing of Contributions](#licensing-of-contributions)

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

This reflects the actual current layout — verify against `find app -name
"*.py"` before trusting a stale copy of this section (this one was out of
date until V6.1: it described an `app/queue/` and `app/storage/` that never
existed in this form, and an `app/ai/client.py` that has since been removed
as dead code).

```
github-autopilot/
│
├── server.py                   # Flask entry point — webhook + /health /mcp /dashboard routes
├── worker.py                   # Standalone consumer process (paid-tier scale-out)
├── requirements.txt            # Runtime dependencies
├── requirements-dev.txt        # + pytest, ruff, pytest-cov
├── .ai-repo-manager.yml        # Example per-repo bot configuration
│
├── app/
│   ├── core/                   # Foundation layer — no GitHub/AI API calls
│   │   ├── config.py           # Per-repo YAML config loader + validation
│   │   ├── confidence.py       # Per-action confidence scoring
│   │   ├── guardrails.py       # Deterministic safety checks (auto-merge, etc.)
│   │   ├── event_queue.py      # Durable Redis webhook queue + consumer group
│   │   ├── thread_pool.py      # Bounded fallback dispatcher (Redis down)
│   │   ├── idempotency.py      # Webhook delivery-id dedup
│   │   ├── webhook_security.py # HMAC verify, rate limit, bot-loop guard, boot checks
│   │   ├── authorization.py    # Maintainer-only command permission checks
│   │   ├── memory_backup.py    # Encrypted (Fernet) memory export/import
│   │   ├── learning.py         # Fix-acceptance tracking — NOT currently wired
│   │   │                       #   into any handler; see testing-guide.md §11
│   │   ├── redis_client.py     # Connection pool + in-memory dev fallback
│   │   └── logger.py           # Structured stdlib logging
│   │
│   ├── intelligence/            # Repo memory ("the brain")
│   │   └── memory.py            # Per-repo recall + privacy guard (local-only by default)
│   │
│   ├── security/                # Security scanning
│   │   ├── secrets.py           # Regex + entropy secret detection in diffs
│   │   ├── dependencies.py      # Vulnerability scanning (OSV.dev)
│   │   └── scanner.py           # Reads GitHub's own Security APIs
│   │
│   ├── github/                  # GitHub API layer
│   │   ├── auth.py              # JWT + installation token caching
│   │   ├── client.py            # HTTP client with retry/backoff
│   │   ├── rate_limit.py        # Rate limit tracking
│   │   └── notifications.py     # Slack/Discord alerts
│   │
│   ├── ai/                      # AI layer
│   │   ├── router.py            # Provider selection, fallback, privacy modes
│   │   ├── providers/           # groq.py, gemini.py, openrouter.py, ollama.py
│   │   ├── circuit_breaker.py   # Per-provider failure tracking
│   │   └── validator.py         # JSON validation + sanitization
│   │
│   ├── mcp/                     # MCP (Model Context Protocol) server
│   │   ├── tools.py             # Tool schema definitions
│   │   ├── handlers.py          # Tool implementations
│   │   └── mcp_server.py        # Transport/dispatch, fail-closed auth
│   │
│   ├── dashboard.py              # Self-contained HTML for GET /dashboard
│   │
│   └── handlers/                 # Event handlers
│       ├── pull_request.py       # PR analysis + code review
│       ├── issues.py              # Issue triage
│       ├── push.py                # Commit linting + secret/dependency scan
│       ├── comments.py            # Deprecated shim — re-exports comments/
│       └── comments/               # Slash-command package
│           ├── constants.py        # ALL_COMMANDS, rate limits
│           ├── dispatcher.py       # Command extraction, memory augmentation
│           ├── service.py          # handle_comment_event() — the real entry point
│           ├── generator.py        # /fix /explain /improve /test /docs ...
│           ├── reviewer.py         # /health /version /summarize /budget ...
│           └── publisher.py        # /merge /apply /rollback /security ...
│
├── plugin/                       # Claude Code plugin (.claude-plugin/, commands/)
└── tests/                         # 38 files, 816 tests — see docs/testing/testing-guide.md
```

---

## Development Setup

### Prerequisites

- Python 3.10+ (CI matrix covers 3.10, 3.11, 3.12)
- A GitHub account
- A Groq API key — [console.groq.com](https://console.groq.com) (free tier), or an [Ollama](https://ollama.com) install for local-only development

### Install dependencies

```bash
pip install -r requirements-dev.txt   # includes pytest, ruff, pytest-cov
```

### Environment variables

Copy `.env.example` to `.env` and fill in your credentials — it's kept
up to date with every environment variable the app reads, including the
optional V6 features (event queue tuning, local-LLM privacy mode, memory).

```bash
cp .env.example .env
```

### Run locally

```bash
python server.py
# Optional — only for the paid-tier scale-out path (see event_queue.py):
python worker.py
```

---

## Making Changes

### Layer rules

Each layer has strict boundaries. Please follow them:

| Layer | Rule |
|-------|------|
| `app/core/` | No GitHub/AI API calls, no side effects beyond Redis |
| `app/github/` | Only GitHub API calls, no AI calls |
| `app/ai/` | Only LLM provider calls (Groq/Gemini/OpenRouter/Ollama via `router.py`), always validate responses |
| `app/handlers/` | Orchestrate only — delegate to core/github/ai |
| `app/security/` | Pure functions where possible, no GitHub API calls (`scanner.py` is the exception — it reads GitHub's own Security APIs) |

### Adding a new slash command

The command table lives in `app/handlers/comments/`, not the old
`comments.py` monolith (that file is now a 2-line backward-compat shim).

1. Add the command name to `ALL_COMMANDS` in `app/handlers/comments/constants.py`
2. Implement `cmd_yourcommand()` in `generator.py` (AI content), `reviewer.py`
   (read-only analysis), or `publisher.py` (GitHub writes) — whichever fits
3. Add a `case "/yourcommand":` branch in `_dispatch()` in
   `app/handlers/comments/service.py`
4. Add to `DEFAULTS["commands"]["enabled"]` in `app/core/config.py`
5. Add to `.ai-repo-manager.yml`'s commands list
6. Write a test in `tests/test_comments_service_integration.py` (routing) and
   alongside the command's own module

### Adding a new security scanner

1. Create `app/security/yourscanner.py`
2. Implement `scan_X(content: str) -> list[Finding]`
3. Implement `format_findings(findings: list) -> str`
4. Hook into `app/handlers/push.py` (diff scanning) or `app/security/scanner.py`
   (if it reads a GitHub Security API instead of scanning content directly)

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

### AI output evals

The unit suite tests the plumbing; [`evals/`](evals/) tests the AI output
itself (planted bugs, must-mention checks) through the real code paths.
Run them before merging any change to prompts, provider order, or
sanitization — they need a real `GROQ_API_KEY` and spend quota:

```bash
python -m evals.run
```

---

## Good First Issues

Look for issues labeled **`good first issue`** — these are well-scoped tasks perfect for first-time contributors:

- Adding a new secret detection pattern to `app/security/secrets.py`
- Adding a new slash command
- Improving AI prompts in any handler
- Adding tests for untested functions
- Improving error messages

---

## Licensing of Contributions

GitHub Autopilot is dual-licensed under **MIT OR Apache-2.0** (see
[LICENSE](LICENSE)). Contributions are accepted under those same terms —
inbound licence matches outbound licence.

By opening a pull request you confirm that:

1. You wrote the contribution, or have the right to submit it under this licence.
2. You agree it may be distributed under **both** MIT and Apache-2.0, so that
   downstream users keep the choice between them.

There is no CLA to sign. Opening the PR is the agreement.

> Why both licences: MIT is short and widely pre-approved, while Apache-2.0
> carries an explicit patent grant that some corporate legal teams require.
> Accepting contributions under both keeps that choice available to everyone
> downstream — which only works if every contribution is dual-licensed too.

### Credit

Contributors are listed in the [README](README.md#contributors) once their pull
request is merged. Add yourself in your PR — the list is maintained by hand, not
generated by a bot.

---

## Questions?

Open an issue or start a discussion — contributions of all kinds are welcome!

Built by [Shweta Mishra](https://github.com/Shweta-Mishra-ai)

