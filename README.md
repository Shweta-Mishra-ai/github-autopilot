<div align="center">

# 🤖 GitHub Autopilot

**AI-powered GitHub automation. Fix bugs, review PRs, scan secrets — all from a comment.**

[![CI](https://github.com/Shweta-Mishra-ai/github-autopilot/actions/workflows/ci.yml/badge.svg)](https://github.com/Shweta-Mishra-ai/github-autopilot/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-22c55e.svg)](LICENSE)
[![Render](https://img.shields.io/badge/deploy-Render-46E3B7?logo=render)](https://render.com)

</div>

---

## What it does

Type a command in any GitHub issue or PR comment. The bot responds in seconds.

```
/fix          → AI generates a bug fix with root cause + test
/explain      → Plain-English explanation of the issue
/security     → Scan PR for secrets and vulnerable deps
/autofix      → Auto-apply safe code improvements
/merge        → Merge PR after all checks pass
/rollback 2   → Restore repo state to snapshot #2
/budget       → Show today's AI token usage
```

> **Secret scanning runs on every push to every branch** — not just main.

---

## Deploy in 10 minutes

### Step 1 — Create GitHub App

1. Go to **github.com/settings/apps** → New GitHub App
2. Set **Webhook URL**: `https://your-app.onrender.com/webhook`
3. Set **Webhook secret**: `python3 -c "import secrets; print(secrets.token_hex(32))"`
4. Permissions: Issues ✏️ · Pull requests ✏️ · Contents ✏️ · Actions ✏️
5. Subscribe to: Push · Pull request · Issue comment · Issues
6. Download the **private key** (`.pem` file)

### Step 2 — Deploy

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy)

Or manually: fork this repo → Render → New Blueprint → connect fork.

### Step 3 — Set environment variables

| Variable | Where to get it |
|----------|----------------|
| `GITHUB_APP_ID` | App settings page (numeric ID) |
| `GITHUB_PRIVATE_KEY` | Contents of `.pem` file |
| `GITHUB_WEBHOOK_SECRET` | The secret you set in Step 1 |
| `GROQ_API_KEY` | [console.groq.com](https://console.groq.com) — free |
| `REDIS_URL` | Render Redis add-on |
| `MCP_API_KEY` | `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `METRICS_AUTH_TOKEN` | Any strong random string |

### Step 4 — Install and verify

Install the GitHub App on your repositories, then:

```bash
curl https://your-app.onrender.com/ping
# → {"status": "ok", "version": "5.0.0"}
```

---

## Commands

| Command | Description | Who |
|---------|-------------|-----|
| `/fix` | AI bug fix with root cause + test | Anyone |
| `/explain` | Plain-English explanation | Anyone |
| `/improve` | Concrete improvement suggestions | Anyone |
| `/test` | Generate pytest test cases | Anyone |
| `/docs` | Generate docstrings + README section | Anyone |
| `/refactor` | Refactoring with before/after | Anyone |
| `/perf` | Performance analysis (O(n²), N+1, etc.) | Anyone |
| `/gaps` | Test coverage gap analysis | Anyone |
| `/arch` | Architecture review | Anyone |
| `/ci` | Analyze CI failure | Anyone |
| `/security` | Secret + dependency scan on PR | Anyone |
| `/secfull` | Full repo security scan | Anyone |
| `/health` | Repo health grade | Anyone |
| `/version` | Tags, releases, recent commits | Anyone |
| `/summarize` | Summarize issue thread | Anyone |
| `/budget` | Today's AI token usage | Anyone |
| `/report` | Weekly analytics | Anyone |
| `/changelog` | Generate CHANGELOG entry | Anyone |
| `/impact` | PR blast radius analysis | Anyone |
| `/merge` | Merge PR after checks pass | Maintainers |
| `/apply` | Open PR from autofix branch | Maintainers |
| `/rollback N` | Restore to snapshot N | Maintainers |
| `/release` | Draft GitHub release | Maintainers |
| `/runtests` | Trigger CI workflow | Maintainers |
| `/notify` | Send Discord/Slack alert | Maintainers |
| `/autofix` | Auto-apply code improvements | Maintainers |

---

## Run locally

```bash
git clone https://github.com/Shweta-Mishra-ai/github-autopilot.git
cd github-autopilot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # fill in your credentials
python server.py
```

## Run tests

```bash
pytest tests/ -v
```

---

## Architecture

```
Webhook → server.py → webhook_security.py  (HMAC-SHA256 + replay + IP rate limit)
                    → idempotency.py        (24h Redis dedup window)
                    → thread_pool.py        (bounded pool, backpressure on saturation)
                         │
                 ┌───────┴────────┐
           handlers/          handlers/
           push.py            comments/
           pull_request.py    ├── service.py    ← orchestration
           autofix.py         ├── generator.py  ← /fix /explain /perf
           issues.py          ├── reviewer.py   ← /health /ci /budget
                              ├── publisher.py  ← /merge /rollback
                              └── dispatcher.py ← rate limit + routing
                         │
                    ai/router.py          (Groq 70B → Gemini → OpenRouter fallback)
                    ai/circuit_breaker.py (per-provider, thread-safe)
                         │
                    core/redis_client.py  (connection pool singleton)
                    core/snapshot.py      (atomic state snapshots)
                    core/learning.py      (per-repo acceptance tracking)
```

**Key design decisions:**

- All webhook handlers run in a `ThreadPoolExecutor` — Flask thread only ACKs
- Idempotency keys live 24 hours, matching GitHub's webhook retry window
- Redis uses `noeviction` policy — idempotency keys are never silently evicted
- Secret scanning runs on **all branches**, not just main
- LLM provider failover is automatic: Groq → Gemini → OpenRouter

---

## Configuration

Add `.ai-repo-manager.yml` to your repository root to customise behaviour:

```yaml
push:
  scan_secrets: true
  scan_dependencies: true
  scan_all_branches: false

autofix:
  max_files: 5

commands:
  maintainer_only:
    - /merge
    - /rollback
    - /release

bot:
  footer: "\n\n---\n*Powered by GitHub Autopilot*"
```

---

## Security

- Webhook signatures verified with HMAC-SHA256 on every request
- Empty `GITHUB_WEBHOOK_SECRET` → all requests rejected at startup
- `MCP_API_KEY` unset → MCP endpoint rejects all requests
- IP rate limiting: 100 requests/min per IP (spoofing-resistant)
- Autofix restricted to safe file extensions; CI/CD files protected
- Bot-loop prevention on all event handlers

Found a vulnerability? Email rather than opening a public issue.

---

## Changelog

### V5.0.0
- `comments.py` refactored into `comments/` package — 5 focused modules
- Redis connection pooling with thread-safe singleton
- Secret scanning extended to all branches
- LLM provider circuit breakers with automatic failover
- MCP server for IDE integrations
- Per-repo config via `.ai-repo-manager.yml`
- 32 tests across all core modules

---

<div align="center">

Built by [Shweta Mishra](https://github.com/Shweta-Mishra-ai) · MIT License

</div>
