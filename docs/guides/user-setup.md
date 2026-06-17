# User Setup Guide

> Complete guide to installing, configuring, and using GitHub Autopilot.
> From zero to your first AI-powered PR review in under 20 minutes.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Step 1 — Deploy to Render](#2-step-1--deploy-to-render)
3. [Step 2 — Create Redis](#3-step-2--create-redis)
4. [Step 3 — Configure Environment Variables](#4-step-3--configure-environment-variables)
5. [Step 4 — Create Your GitHub App](#5-step-4--create-your-github-app)
6. [Step 5 — Install on Your Repository](#6-step-5--install-on-your-repository)
7. [Step 6 — Verify Everything Works](#7-step-6--verify-everything-works)
8. [Step 7 — Configure the Bot (Optional)](#8-step-7--configure-the-bot-optional)
9. [Using Slash Commands](#9-using-slash-commands)
10. [Troubleshooting](#10-troubleshooting)
11. [Updating the Bot](#11-updating-the-bot)
12. [Uninstalling](#12-uninstalling)

---

## 1. Prerequisites

**Required:**
- GitHub account
- Render account (free) — [render.com](https://render.com)
- Groq API key (free) — [console.groq.com](https://console.groq.com)

**Recommended (adds fallback LLM capacity):**
- Gemini API key (free) — [aistudio.google.com](https://aistudio.google.com/app/apikey)
- OpenRouter API key (free tier) — [openrouter.ai/keys](https://openrouter.ai/keys)

**Optional:**
- Discord server (for notifications)
- Qdrant Cloud account (for vector code context) — [qdrant.tech](https://qdrant.tech)

---

## 2. Step 1 — Deploy to Render

### Fork the repository

```
https://github.com/Shweta-Mishra-ai/github-autopilot
→ Fork → your-username/github-autopilot
```

### Create the Web Service

1. Go to [render.com](https://render.com) → **New** → **Web Service**
2. Connect your forked GitHub repository
3. Configure the service:

   | Setting | Value |
   |---------|-------|
   | **Name** | `github-autopilot` (or any name) |
   | **Region** | Closest to you |
   | **Branch** | `main` |
   | **Runtime** | `Python 3` |
   | **Build Command** | `pip install -r requirements.txt` |
   | **Start Command** | `gunicorn server:app --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT --worker-class gthread` |

4. **Plan:** Free
5. Click **Create Web Service**
6. Wait for the first deploy to complete (2–3 minutes)
7. Copy your service URL: `https://your-service-name.onrender.com`

> **Note:** On Render free tier, services spin down after 15 minutes of inactivity. The first webhook after a spin-down may take 30–60 seconds. This is normal. Consider [UptimeRobot](https://uptimerobot.com) (free) to ping `/health` every 5 minutes and keep the service warm.

---

## 3. Step 2 — Create Redis

1. Render dashboard → **New** → **Redis**
2. Configure:

   | Setting | Value |
   |---------|-------|
   | **Name** | `github-autopilot-redis` |
   | **Plan** | Free |
   | **Max Memory Policy** | `allkeys-lru` |

3. Click **Create Redis**
4. Once created, go to the Redis service → **Info** tab
5. Copy the **Internal Redis URL** — it looks like `redis://red-xxxxxxxxxxxx:6379`

> Use the **Internal** URL (not External). Internal URLs are faster and don't count against bandwidth limits.

---

## 4. Step 3 — Configure Environment Variables

In Render → your Web Service → **Environment** tab → **Add Environment Variable**:

### Required variables

| Variable | Value | Where to get it |
|----------|-------|----------------|
| `GITHUB_APP_ID` | Your app's numeric ID | GitHub App settings (step 4) |
| `GITHUB_PRIVATE_KEY` | Full PEM contents | Downloaded in step 4 |
| `GITHUB_WEBHOOK_SECRET` | A strong random string | Generate below |
| `GROQ_API_KEY` | `gsk_...` | [console.groq.com](https://console.groq.com) → API Keys |
| `REDIS_URL` | Internal Redis URL | From step 2 |

**Generate a webhook secret:**
```bash
python -c "import secrets; print(secrets.token_hex(32))"
# Example output: a3f9c2e1b4d8f7a6...
```
Copy this value. You will need it again in step 4 when creating the GitHub App.

### Recommended variables

| Variable | Value | Benefit |
|----------|-------|---------|
| `GEMINI_API_KEY` | From Google AI Studio | Adds 1,500 LLM calls/day fallback capacity |
| `OPENROUTER_API_KEY` | From OpenRouter | Emergency fallback (200 calls/day) |

### Optional variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `DISCORD_WEBHOOK_URL` | — | Discord notifications on high-risk events |
| `SLACK_WEBHOOK_URL` | — | Slack notifications |
| `QDRANT_URL` | — | Vector DB for code context enrichment |
| `QDRANT_API_KEY` | — | Qdrant Cloud authentication |
| `METRICS_AUTH_TOKEN` | — | Protects `/metrics` endpoint (any string) |
| `MAX_DISPATCH_WORKERS` | `6` | Thread pool size |
| `REPO_DAILY_AI_LIMIT` | `150` | Max AI calls per repo per day |

After adding all variables, click **Save Changes**. Render will automatically redeploy the service.

---

## 5. Step 4 — Create Your GitHub App

### Create the app

1. Go to `https://github.com/settings/apps/new`

2. Fill in the basic information:

   | Field | Value |
   |-------|-------|
   | **GitHub App name** | `AI Repo Manager` (must be unique globally) |
   | **Homepage URL** | `https://your-service-name.onrender.com` |
   | **Webhook URL** | `https://your-service-name.onrender.com/webhook` |
   | **Webhook secret** | The secret you generated in step 3 |
   | **Webhook: Active** | ✅ Checked |

3. Set **Repository permissions**:

   | Permission | Level | Why |
   |-----------|-------|-----|
   | Contents | Read & Write | Autofix creates branches and commits |
   | Issues | Read & Write | Create issues, add labels, post comments |
   | Pull requests | Read & Write | Analyze PRs, post reviews, merge |
   | Actions | Read & Write | Trigger workflows via `/runtests` |
   | Metadata | Read | Required by GitHub (cannot be disabled) |
   | Checks | Read | CI failure analysis via `/ci` |

4. Subscribe to **events**:

   | Event | Handler | What it enables |
   |-------|---------|----------------|
   | ✅ Pull request | `pull_request.py` | Auto PR review, title polish, test gaps |
   | ✅ Issues | `issues.py` | Issue triage, auto-labeling |
   | ✅ Issue comment | `comments.py` | All 26 slash commands |
   | ✅ Push | `push.py` | Commit lint, secret scan, dep scan |
   | ✅ Check run | `ci.py` | CI failure analysis |

5. **Where can this GitHub App be installed?** → Select **Only on this account** (safer) or **Any account** (for sharing)

6. Click **Create GitHub App**

### Get your App ID

On the App settings page, copy the **App ID** (a number like `123456`). Set this as `GITHUB_APP_ID` in Render.

### Generate and save the private key

1. Scroll down on the App settings page
2. Click **Generate a private key**
3. A `.pem` file downloads automatically
4. Open the file in a text editor — it looks like:
   ```
   -----BEGIN RSA PRIVATE KEY-----
   MIIEowIBAAKCAQEA...
   (many lines)
   -----END RSA PRIVATE KEY-----
   ```
5. Copy **the entire contents** including the header and footer lines
6. In Render → Environment → `GITHUB_PRIVATE_KEY` → paste the full PEM content

> **Important:** The private key must include the `-----BEGIN RSA PRIVATE KEY-----` header and `-----END RSA PRIVATE KEY-----` footer. Some Render configurations require newlines to be replaced with `\n` — if you see auth errors, try: `cat your-key.pem | tr '\n' '|' | sed 's/|/\\n/g'`

---

## 6. Step 5 — Install on Your Repository

1. On the GitHub App settings page, click **Install App** in the left sidebar
2. Click **Install** next to your account
3. Choose **Only select repositories** → select the repo(s) you want the bot on
4. Click **Install**

The bot is now active. It will respond to all events on the selected repositories.

---

## 7. Step 6 — Verify Everything Works

### Check the health endpoint

Open in your browser:
```
https://your-service-name.onrender.com/health
```

Expected response:
```json
{
  "status": "ok",
  "version": "4.2.0",
  "uptime_seconds": 142,
  "checks": {
    "redis": "ok",
    "github_api": "ok",
    "llm_providers": {
      "groq_70b":   {"state": "closed", "failures": 0},
      "groq_8b":    {"state": "closed", "failures": 0},
      "gemini":     {"state": "closed", "failures": 0},
      "openrouter": {"state": "closed", "failures": 0}
    }
  },
  "thread_pool": {
    "max_workers": 6,
    "pending_jobs": 0,
    "queue_capacity": 50,
    "saturation_pct": 0.0
  }
}
```

**What each field means:**

| Field | OK state | Action if wrong |
|-------|----------|----------------|
| `status` | `"ok"` | Check all env vars are set |
| `redis` | `"ok"` | Verify `REDIS_URL` is the Internal URL from Render |
| `github_api` | `"ok"` | Check `GITHUB_APP_ID` and `GITHUB_PRIVATE_KEY` |
| `llm_providers.*.state` | `"closed"` | Check `GROQ_API_KEY` is valid |
| `thread_pool.pending_jobs` | `0` or low | High value = bot is under load |

### Test with a live command

1. Open any issue in an installed repository
2. Post a comment: `/health`
3. Wait up to 30 seconds
4. The bot should reply with a repo health grade

If no response after 60 seconds:
- Check Render service logs for errors
- Verify the GitHub App webhook URL is correct
- Verify the webhook is marked "Active" in GitHub App settings
- Check the GitHub App → **Advanced** → **Recent Deliveries** tab to see if GitHub sent the webhook and what the response was

---

## 8. Step 7 — Configure the Bot (Optional)

Create `.ai-repo-manager.yml` in your repository root to customise behaviour. All settings are optional — safe defaults apply if the file is missing.

```yaml
# .ai-repo-manager.yml
# Full documentation: docs/guides/slash-commands.md

bot:
  enabled: true
  footer: "\n\n---\n*🤖 AI Repo Manager — your-repo-name*"

pull_requests:
  enabled: true
  auto_polish_title: true       # Rewrites vague PR titles
  auto_fill_description: true   # Fills empty PR descriptions
  code_review: true             # Posts code quality review on every PR
  detect_test_gaps: true        # Detects missing test coverage
  max_files_reviewed: 4         # Max files analysed per PR review

issues:
  enabled: true
  auto_triage: true             # Auto-triages every new issue
  auto_label: true              # Adds labels based on issue type

push:
  enabled: true
  enforce_conventional_commits: true  # Alerts on non-conventional commits
  create_issue_threshold: 3           # Bad commits before creating an issue
  scan_secrets: true                  # Scans every push for leaked credentials
  scan_dependencies: true             # Scans requirements.txt changes for CVEs

confidence:
  thresholds:
    pr_title_rewrite: 0.85    # Min confidence to rewrite PR title
    auto_merge: 0.95          # Min confidence for auto-merge (very conservative)
    fix_command: 0.70         # Min confidence for /fix suggestions
    code_review: 0.75         # Min confidence for code review comments

commands:
  permissions:
    maintainer_only:            # Only write/maintain/admin users can use these
      - merge
      - release
      - rollback
  enabled:                      # Commands active on this repo (all 26 by default)
    - fix
    - autofix
    - apply
    - improve
    - refactor
    - perf
    - explain
    - summarize
    - arch
    - impact
    - gaps
    - ci
    - docs
    - test
    - changelog
    - release
    - version
    - runtests
    - security
    - secfull
    - health
    - merge
    - rollback
    - report
    - budget
    - notify
```

Commit this file to your default branch. The bot picks it up within 5 minutes (config cache TTL).

**Common customisations:**

Disable code review on every PR (too noisy):
```yaml
pull_requests:
  code_review: false
```

Disable a command you don't want:
```yaml
commands:
  enabled:
    - fix
    - explain
    - health
    # (just list the ones you want)
```

Use a custom footer:
```yaml
bot:
  footer: "\n\n---\n*🤖 Powered by AI · [docs](https://github.com/your/repo/docs)*"
```

---

## 9. Using Slash Commands

Comment any command on a GitHub issue or PR. The bot responds within 30 seconds.

### Starting commands (use these first)

**Check repo health:**
```
/health
```
Gets an A–F grade for your repo with specific improvement recommendations.

**Understand a PR:**
```
/explain
```
Explains what a PR does in plain English.

**Fix a bug:**
```
/fix
```
Analyses the issue and suggests root cause + production-ready fix.

**Automated fix (creates a PR):**
```
/autofix
```
Goes further than `/fix` — actually creates a branch, commits the fix, and opens a PR.
> ⚠️ Requires **write** access. Always review the autofix PR before merging.

**Target a specific file:**
```
/autofix app/handlers/comments.py
```

### Understanding commands

```
/impact          → Blast radius: which layers does this PR touch?
/arch            → Architecture review: layer violations, coupling issues
/perf            → Performance analysis: O(n²), N+1 queries, caching opportunities
/gaps            → Test coverage gaps with risk ratings
/summarize       → Condense a long issue/PR discussion thread
/ci              → Analyse CI failure and suggest exact fix steps
```

### Documentation commands

```
/docs            → Generate docstrings + README section for this code
/test            → Generate pytest test suite for changed code
/changelog       → Write CHANGELOG entry from recent commits
/version         → Show tag history and current version status
```

### Security commands

```
/security        → Scan PR diff for secrets and vulnerable dependencies
/secfull         → Full security audit: Dependabot + CodeQL + Secret Scanning APIs
```
> `/secfull` requires **write** access.

### Operations commands (maintainer access required)

```
/merge           → Merge the PR (checks: CI green, reviews done, no conflicts)
/rollback        → List available snapshots or restore to a previous state
/release         → Create a GitHub draft release with AI-generated release notes
/runtests        → Trigger the test workflow via GitHub Actions workflow_dispatch
/report          → Weekly analytics: PR velocity, issue resolution, quality grade
/budget          → Show live LLM token usage and daily cost per provider
/notify          → Send this issue/PR to Discord with severity-coded embed
```

### Rate limits

- **10 commands per user per hour per repo**
- If you hit the limit: the bot will tell you and the limit resets after 1 hour
- Maintainers are subject to the same per-user limit

---

## 10. Troubleshooting

### Bot doesn't respond to commands

**Check 1 — Command is in the enabled list**

If the bot responds with "ℹ️ Command Disabled", the command is not in `commands.enabled` in your `.ai-repo-manager.yml`. Add it to the list.

**Check 2 — You have permission for restricted commands**

If you get "⛔ Permission Denied", the command requires write/maintain/admin access. `/merge`, `/rollback`, `/release` are maintainer-only by default.

**Check 3 — Bot is installed on this repo**

Go to GitHub → Settings → Installed GitHub Apps → confirm the bot is installed on the repository.

**Check 4 — Check Render logs**

Render → your service → **Logs** tab. Look for errors after the time you posted the command.

**Check 5 — Check GitHub webhook delivery**

GitHub App settings → **Advanced** → **Recent Deliveries**. Find the delivery for your comment event. Check the response code — it should be 202. If it is 401, your webhook secret doesn't match. If it is 500, check Render logs for the Python traceback.

---

### "AI temporarily unavailable" response

All LLM providers are down or rate-limited. Usually resolves within an hour when the daily limit resets.

**Check `/health`:** Look at `llm_providers.*.state`. Any provider showing `"open"` has tripped its circuit breaker.

**Add more API keys to increase capacity:**
- `GEMINI_API_KEY` adds 1,500 requests/day
- `OPENROUTER_API_KEY` adds 200 requests/day emergency fallback

---

### Redis shows "unavailable" in /health

1. Verify `REDIS_URL` is the **Internal** Redis URL from Render (not External)
2. Check the Redis service in Render — it may have been deleted or is in a different region
3. Redeploy the web service after fixing `REDIS_URL`

---

### Duplicate issue alerts for the same secret

Expected behaviour — the dedup system prevents duplicate alerts within 1 hour. If duplicates appear across different hours, this is intended (the secret was pushed multiple times). If duplicates appear within the same hour, check that Redis is connected (`/health` shows `redis: "ok"`). Without Redis, dedup falls back to in-memory (per-process) and Render's 2 workers may both process the event.

---

### Autofix says "Fix didn't change anything"

The LLM returned prose instead of JSON. This happens occasionally under load. Try:
1. Rephrase the issue title to be more specific about what needs fixing
2. Specify the file: `/autofix app/handlers/comments.py`
3. Wait a few minutes and retry (the LLM provider may have been under load)

---

### Webhook verification fails (401)

Mismatch between `GITHUB_WEBHOOK_SECRET` in Render and the Webhook Secret in your GitHub App settings. They must be identical.

1. Generate a new secret: `python -c "import secrets; print(secrets.token_hex(32))"`
2. Update `GITHUB_WEBHOOK_SECRET` in Render → Environment
3. Update Webhook Secret in GitHub App → General → Webhook Secret
4. Wait for Render to redeploy (automatic)

---

## 11. Updating the Bot

Updates deploy automatically when you push to the connected branch:

```bash
# Pull latest changes from upstream
git remote add upstream https://github.com/Shweta-Mishra-ai/github-autopilot.git
git fetch upstream
git merge upstream/main
git push origin main
```

Render detects the push and redeploys automatically (2–3 minutes). No downtime — Render uses rolling deploys.

**To roll back a deploy:** Render → your service → **Deploys** tab → find a previous deploy → **Rollback to this deploy**.

---

## 12. Uninstalling

**Remove from repository:**
GitHub → Settings → Installed GitHub Apps → Configure → Uninstall → select the repo → Uninstall

**Delete Render services:**
Render → Web Service → Settings → Delete Service
Render → Redis → Settings → Delete Redis

**Delete GitHub App:**
GitHub → Settings → Developer Settings → GitHub Apps → Edit → Delete GitHub App (at the bottom)

All data (Redis state, analytics, snapshots) is automatically deleted when the Redis instance is deleted.

