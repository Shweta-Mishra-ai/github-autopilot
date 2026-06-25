# Render Deployment Guide

> Complete guide to deploying GitHub Autopilot on Render's free tier.
> From zero infrastructure to a live, webhook-receiving bot in under 30 minutes.

---

## Table of Contents

1. [Architecture on Render](#1-architecture-on-render)
2. [Prerequisites](#2-prerequisites)
3. [Step 1 — Fork and Prepare the Repository](#3-step-1--fork-and-prepare-the-repository)
4. [Step 2 — Create Redis](#4-step-2--create-redis)
5. [Step 3 — Create the Web Service](#5-step-3--create-the-web-service)
6. [Step 4 — Environment Variables Reference](#6-step-4--environment-variables-reference)
7. [Step 5 — Create the GitHub App](#7-step-5--create-the-github-app)
8. [Step 6 — Verify the Deployment](#8-step-6--verify-the-deployment)
9. [Keeping the Service Warm](#9-keeping-the-service-warm)
10. [Render-Specific Behaviour](#10-render-specific-behaviour)
11. [Scaling Beyond Free Tier](#11-scaling-beyond-free-tier)
12. [Troubleshooting Render-Specific Issues](#12-troubleshooting-render-specific-issues)

---

## 1. Architecture on Render

```
┌─────────────────────────────────────────────────────────────────┐
│                        RENDER FREE TIER                         │
│                                                                 │
│  ┌───────────────────────────────────────┐                     │
│  │          Web Service                  │                     │
│  │                                       │                     │
│  │  gunicorn server:app                  │                     │
│  │  --workers 1 --threads 8              │  512MB RAM          │
│  │  --timeout 120                        │  0.5 CPU            │
│  │  --bind 0.0.0.0:$PORT                 │  Spins down after   │
│  │                                       │  15min inactivity   │
│  │  Worker 1 ──► ThreadPoolExecutor      │                     │
│  │  Worker 2 ──► ThreadPoolExecutor      │                     │
│  │               max_workers=6           │                     │
│  │               queue_cap=50            │                     │
│  └──────────────────┬────────────────────┘                     │
│                     │ Internal network                         │
│  ┌──────────────────▼────────────────────┐                     │
│  │          Redis (free)                 │                     │
│  │                                       │  25MB               │
│  │  Idempotency keys    Rate limits      │  No persistence SLA  │
│  │  Analytics           Snapshots        │  allkeys-lru policy │
│  │  LLM budget          CI patterns      │                     │
│  └───────────────────────────────────────┘                     │
└─────────────────────────────────────────────────────────────────┘
              │                           │
              │ Outbound HTTPS            │ Outbound HTTPS
              ▼                           ▼
         GitHub API                  LLM Providers
         (REST + webhooks)          (Groq, Gemini,
                                     OpenRouter)
```

**Key constraints on free tier:**
- Service spins down after 15 minutes with no traffic → 30–60s cold start on next request
- 512MB RAM — Python process + 2 Gunicorn workers + 6 threads ≈ 150–200MB baseline
- 0.5 CPU — LLM API calls are I/O bound so this is rarely the bottleneck
- Redis 25MB — sufficient for ~50,000 idempotency keys at 500 bytes each
- 750 service-hours/month free — enough for 24/7 operation

---

## 2. Prerequisites

- Render account — [render.com](https://render.com) (free, no credit card required for free tier)
- GitHub account
- Groq API key — [console.groq.com](https://console.groq.com) (free)
- Forked copy of the repository

---

## 3. Step 1 — Fork and Prepare the Repository

### Fork

```
https://github.com/Shweta-Mishra-ai/github-autopilot
→ Fork (top-right button)
→ Fork to: your-username/github-autopilot
```

### Verify the Procfile

The repository includes a `Procfile` that Render uses automatically:

```
web: gunicorn server:app --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT --worker-class gthread
```

| Flag | Value | Why |
|------|-------|-----|
| `--workers 1 --threads 8` | 1 worker, 8 threads | Safe for 512 MB RAM; threads handle concurrent requests |
| `--timeout 120` | 120 seconds | LLM calls + GitHub API can take up to 60s; 120s provides headroom |
| `--worker-class gthread` | Green threaded workers | The app uses threading internally — gthread workers are correct |
| `--bind 0.0.0.0:$PORT` | Render's dynamic port | Render injects the `PORT` env var automatically |

Do not change `--worker-class` to `gevent` or `eventlet` — the thread-based architecture requires sync workers.

---

## 4. Step 2 — Create Redis

1. Render dashboard → **New +** → **Redis**

2. Configure:

   | Setting | Value | Notes |
   |---------|-------|-------|
   | Name | `github-autopilot-redis` | Any name |
   | Region | Same as your web service | Reduces latency |
   | Plan | **Free** | 25MB, no credit card |
   | Max Memory Policy | `allkeys-lru` | Evicts oldest keys when full |

3. Click **Create Redis**

4. Wait for status to show **Available** (30–60 seconds)

5. Click on the Redis service → **Connect** tab

6. Copy the **Internal Redis URL** — it looks like:
   ```
   redis://red-cxxxxxxxxxxxxxxxxx:6379
   ```

> ⚠️ **Use the Internal URL, not the External URL.** The internal URL:
> - Routes traffic within Render's private network (faster)
> - Does not consume your bandwidth quota
> - Is not accessible from outside Render (more secure)
> - Looks like `redis://red-xxx:6379` (no hostname, just the Render internal ID)

---

## 5. Step 3 — Create the Web Service

1. Render dashboard → **New +** → **Web Service**

2. Connect repository:
   - Click **Connect a repository**
   - Authorise Render to access GitHub if prompted
   - Search for and select your forked repository

3. Configure the service:

   | Setting | Value |
   |---------|-------|
   | **Name** | `github-autopilot` |
   | **Region** | Closest to your primary users |
   | **Branch** | `main` |
   | **Root Directory** | *(leave blank)* |
   | **Runtime** | `Python 3` |
   | **Build Command** | `pip install -r requirements.txt` |
   | **Start Command** | `gunicorn server:app --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT --worker-class gthread` |

4. **Instance Type:** Free

5. **Health Check Path:** `/ping` (public liveness check — no auth required)
   - Render pings this path to verify the service is running
   - Returns 200 when healthy, 207 when degraded

6. Click **Create Web Service**

7. Watch the deploy logs — the first deploy takes 2–4 minutes:
   ```
   ==> Building...
   ==> Running pip install -r requirements.txt
   ==> Build successful
   ==> Starting service...
   ==> startup_check passed: GITHUB_WEBHOOK_SECRET is configured.
   [INFO] Starting gunicorn 21.2.0
   [INFO] Listening at: http://0.0.0.0:10000
   ```

8. Copy your service URL: `https://your-service-name.onrender.com`

> If the deploy fails with `RuntimeError: FATAL: GITHUB_WEBHOOK_SECRET is not set`, that is expected — the env vars are set in the next step, then the service redeploys.

---

## 6. Step 4 — Environment Variables Reference

Render → your web service → **Environment** tab → **Add Environment Variable**

After adding each variable, click **Save Changes**. Render redeploys automatically.

### Required (service will not start without these)

| Variable | Format | How to obtain |
|----------|--------|---------------|
| `GITHUB_WEBHOOK_SECRET` | Any string, 32+ chars recommended | Generate: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `GITHUB_APP_ID` | Integer, e.g. `123456` | GitHub App settings page → App ID |
| `GITHUB_PRIVATE_KEY` | Full PEM including headers | Generate in GitHub App settings → Generate a private key → copy full file contents |
| `GROQ_API_KEY` | `gsk_...` | [console.groq.com](https://console.groq.com) → API Keys → Create new |
| `REDIS_URL` | `redis://red-xxx:6379` | Render Redis service → Connect → Internal Redis URL |

### Pasting the Private Key

The PEM key is multi-line. Render accepts it two ways:

**Option A — Paste as-is (recommended):**
In Render's environment variable editor, click the value field and paste the entire PEM content including newlines. Render stores multi-line values correctly.

**Option B — Escape newlines:**
If Option A causes issues, replace newlines with `\n`:
```bash
cat your-private-key.pem | awk '{printf "%s\\n", $0}' | pbcopy
```
Then paste the single-line result.

### Recommended (significantly extends LLM capacity)

| Variable | Format | Benefit |
|----------|--------|---------|
| `GEMINI_API_KEY` | `AIza...` | Adds 1,500 LLM calls/day — Gemini Flash (1M token context) |
| `OPENROUTER_API_KEY` | `sk-or-...` | Adds 200 calls/day emergency fallback |

### Optional

| Variable | Default | Purpose |
|----------|---------|---------|
| `DISCORD_WEBHOOK_URL` | None | Discord channel for high-risk PR + secret alerts |
| `SLACK_WEBHOOK_URL` | None | Slack channel notifications |
| `QDRANT_URL` | None | Qdrant Cloud URL for vector code context |
| `QDRANT_API_KEY` | None | Qdrant Cloud API key |
| `METRICS_AUTH_TOKEN` | None | Bearer token to protect `/metrics` endpoint |
| `MAX_DISPATCH_WORKERS` | `6` | Thread pool worker count (integer) |
| `REPO_DAILY_AI_LIMIT` | `150` | Max AI calls per repo per day (integer) |
| `LOG_LEVEL` | `INFO` | Set to `DEBUG` for verbose logging |

---

## 7. Step 5 — Create the GitHub App

### Create at GitHub

Go to: `https://github.com/settings/apps/new`

Fill in:

| Field | Value |
|-------|-------|
| **GitHub App name** | `AI Repo Manager` (must be globally unique — add your username if needed) |
| **Homepage URL** | `https://your-service-name.onrender.com` |
| **Webhook URL** | `https://your-service-name.onrender.com/webhook` |
| **Webhook secret** | The exact value you set as `GITHUB_WEBHOOK_SECRET` in Render |
| **Webhook: Active** | ✅ Checked |
| **SSL verification** | ✅ Enable (Render provides valid TLS) |

### Repository Permissions

Set these exactly — wrong permissions cause silent failures:

| Permission | Level | Why required |
|-----------|-------|-------------|
| Contents | **Read & Write** | Autofix: create branches, commit files |
| Issues | **Read & Write** | Create issues (secret alerts, lint), post comments, add labels |
| Pull requests | **Read & Write** | Review PRs, post review comments, merge via `/merge` |
| Actions | **Read & Write** | Trigger test workflows via `/runtests` |
| Metadata | **Read** | Required by GitHub — cannot be removed |
| Checks | **Read** | CI failure analysis via `/ci` command |

### Event Subscriptions

Subscribe to exactly these events:

| Event | GitHub name | What it enables |
|-------|------------|----------------|
| ✅ Pull request | `pull_request` | Auto PR analysis when opened/updated |
| ✅ Issues | `issues` | Issue triage when created |
| ✅ Issue comment | `issue_comment` | All 26 slash commands |
| ✅ Push | `push` | Commit lint, secret scan, dep scan |
| ✅ Check run | `check_run` | CI failure analysis |

### Installation scope

**Only on this account** — safer, prevents the App being installed on repos you don't own.

Click **Create GitHub App**.

### Get App credentials

**App ID:** Shown on the App settings page. Set as `GITHUB_APP_ID` in Render.

**Private key:**
1. Scroll to **Private keys** section on the App settings page
2. Click **Generate a private key**
3. A `.pem` file downloads
4. Open it: the content starts with `-----BEGIN RSA PRIVATE KEY-----`
5. Copy the entire file contents (header + base64 lines + footer)
6. Paste as `GITHUB_PRIVATE_KEY` in Render

### Install the App

1. GitHub App settings → left sidebar → **Install App**
2. Click **Install** next to your account name
3. Select **Only select repositories**
4. Choose the repository/repositories to enable the bot on
5. Click **Install**

---

## 8. Step 6 — Verify the Deployment

### Check startup

Render → your service → **Logs** tab. Look for:
```
startup_check passed: GITHUB_WEBHOOK_SECRET is configured.
[INFO] Starting gunicorn 21.2.0
[INFO] Booting worker with pid: 7
[INFO] Booting worker with pid: 8
```

If you see:
```
RuntimeError: FATAL: GITHUB_WEBHOOK_SECRET is not set.
```
→ The environment variable is missing or empty. Check the Environment tab.

### Check the health endpoint

```bash
# Public liveness check (no auth)
curl https://your-service-name.onrender.com/ping

# Detailed health (requires METRICS_AUTH_TOKEN)
curl -H "Authorization: Bearer $METRICS_AUTH_TOKEN" https://your-service-name.onrender.com/health | python -m json.tool
```

Expected output:
```json
{
  "status": "ok",
  "version": "4.2.0",
  "checks": {
    "redis": "ok",
    "github_api": "ok",
    "llm_providers": {
      "groq_70b": {"state": "closed", "failures": 0},
      "groq_8b":  {"state": "closed", "failures": 0}
    }
  },
  "thread_pool": {
    "max_workers": 6,
    "pending_jobs": 0
  }
}
```

**If `redis` shows `unavailable`:**
- `REDIS_URL` env var is wrong or missing
- Using the External URL instead of Internal — switch to Internal
- Redis service is in a different Render region than the web service

**If `groq_70b.state` shows `open`:**
- `GROQ_API_KEY` is invalid or expired
- Groq API had a recent outage — check [status.groq.com](https://status.groq.com)

### Send a test webhook

1. Open any issue in an installed repository
2. Post a comment: `/health`
3. Wait up to 30 seconds

The bot should reply with a repo health grade. If no response:

Check GitHub App → **Advanced** → **Recent Deliveries**:
- Green → webhook delivered, check Render logs for handler errors
- Red with 401 → webhook secret mismatch — ensure `GITHUB_WEBHOOK_SECRET` matches exactly what's in GitHub App settings
- Red with 500 → Python error — check Render logs for stack trace
- Timeout → service was sleeping (cold start) — retry in 60 seconds

---

## 9. Keeping the Service Warm

Render free tier spins down after 15 minutes of inactivity. The first webhook after a spin-down triggers a cold start (30–60 seconds). GitHub may mark the delivery as failed and retry.

**Solution — UptimeRobot (free, no credit card):**

1. Create account at [uptimerobot.com](https://uptimerobot.com)
2. **New Monitor** → **HTTP(s)**
3. Configure:
   ```
   URL: https://your-service-name.onrender.com/health
   Check interval: 5 minutes
   Alert contacts: your email
   ```
4. Click **Create Monitor**

UptimeRobot pings `/ping` every 5 minutes, keeping the service warm indefinitely. It also emails you if the service goes down.

**Alternative — GitHub Actions keep-alive:**
```yaml
# .github/workflows/keepalive.yml
name: Keep Render Warm
on:
  schedule:
    - cron: '*/5 * * * *'   # every 5 minutes
jobs:
  ping:
    runs-on: ubuntu-latest
    steps:
      - run: curl -f https://your-service-name.onrender.com/health
```

---

## 10. Render-Specific Behaviour

### Auto-deploy on push

Render automatically redeploys the service when you push to the connected branch (`main`). Deploy time is 2–3 minutes. During redeploy:
- In-flight requests complete on the old workers
- New workers start with the updated code
- Redis state is preserved (Redis is not redeployed)
- No downtime for users — GitHub retries any webhooks that hit a deploying worker

### Environment variable changes trigger redeploy

Adding, changing, or removing an environment variable in Render's dashboard automatically triggers a redeploy. This is useful when rotating API keys.

### Log retention

Render retains logs for 7 days on free tier. For longer retention, set up log forwarding to Papertrail or Logtail (both have free tiers):

Render → your service → **Settings** → **Log Stream** → add a log drain URL.

### Render's X-Forwarded-For header

Render's load balancer adds `X-Forwarded-For` to every request with the original client IP. The bot's IP rate limiter reads this header:
```python
client_ip = (
    request.headers.get("X-Forwarded-For", request.remote_addr or "")
    .split(",")[0]
    .strip()
)
```
This correctly extracts the original sender IP even through Render's proxy layer.

### Outbound IP addresses

GitHub webhook payloads originate from GitHub's IP ranges (`192.30.252.0/22`, `185.199.108.0/22`, etc.). These are used by the signature check, not the IP rate limiter. The rate limiter targets inbound IP addresses to the Render service.

---

## 11. Scaling Beyond Free Tier

When free tier limits are reached, upgrade in this order:

### Step 1 — Upgrade Render Web Service to Starter ($7/month)
Eliminates cold starts. Service stays running 24/7. 512MB RAM → 512MB RAM (same), but adds always-on.

### Step 2 — Add more LLM API keys
Before paying for more compute, extend LLM capacity at zero cost:
- `GEMINI_API_KEY` → +1,500 calls/day
- `OPENROUTER_API_KEY` → +200 calls/day emergency
- Multiple Groq API keys (using different accounts) → multiply daily limits

### Step 3 — Upgrade Redis ($10/month)
25MB free Redis becomes a constraint at ~50 active repos with heavy usage. Render's paid Redis starts at $10/month with 100MB and persistence guarantees.

### Step 4 — Increase `MAX_DISPATCH_WORKERS`
Add to environment variables: `MAX_DISPATCH_WORKERS=10`. This allows more concurrent LLM calls — useful if you have more API quota than the default 6 workers can utilise.

### Step 5 — Add a task queue (when needed)
When jobs must not be lost across restarts, consider adding a dedicated task queue. The codebase has Celery wiring in `archive/tasks.py` that can be activated. This requires a separate worker process and a Redis broker instance.

---

## 12. Troubleshooting Render-Specific Issues

### Build fails with `pip install` errors

```
ERROR: Could not find a version that satisfies the requirement groq==0.9.0
```

**Cause:** Python version mismatch. Render's default Python may differ from your local version.

**Fix:** Add `.python-version` to your repo root:
```
3.11.9
```

### Service starts but immediately exits

Check Render logs for the exit reason:
```
RuntimeError: FATAL: GITHUB_WEBHOOK_SECRET is not set.
```
→ Missing required environment variable.

```
Address already in use
```
→ Render is trying to bind a port already in use. This is a Render infrastructure issue — try a manual redeploy.

### Webhooks arrive but no response is posted

1. Check Render logs for `dispatch.error` entries
2. Check for `GITHUB_APP_ID` being wrong (numeric, not the App slug)
3. Check `GITHUB_PRIVATE_KEY` — must include PEM headers, must be the key for the correct App ID
4. Verify the App is installed on the repository (GitHub → Settings → Installed Apps)

### "Secret scanning" alert from GitHub on the service code

GitHub Secret Scanning scans public repositories. The bot's source code contains credential-like patterns in the scanner itself and in test files.

**Already handled:** All credential patterns in `enhanced_secrets.py` are assembled via `_fp()` at runtime. All test credential strings are assembled via helper functions. GitHub Secret Scanning should not flag the source code.

If an alert appears:
1. Check which file and line triggered it
2. If it is in a test helper function → dismiss as "used in tests"
3. If it is a literal string → convert to runtime assembly via helper function and commit the fix

### Service shows 502/503 errors intermittently

**Cause:** Cold starts on free tier.

**Fix:** Use UptimeRobot to keep the service warm (see [§9](#9-keeping-the-service-warm)).

If 502s persist even with a warm service:
1. Check `thread_pool.saturation_pct` in `/health` — if > 80%, increase `MAX_DISPATCH_WORKERS`
2. Check Render service memory usage — if near 512MB, some workers may be OOM-killed
3. Check for memory leaks in handler code (uncommon but possible with large PR diffs)

### Redis memory full (25MB limit reached)

Check Redis usage:
```bash
redis-cli -u $REDIS_URL INFO memory | grep used_memory_human
```

If close to 25MB:
1. The `allkeys-lru` policy evicts oldest keys automatically — this is correct behaviour
2. If eviction is causing issues (idempotency keys evicted prematurely), upgrade to paid Redis
3. Consider reducing `TTL` on analytics keys (currently permanent — add `r.expire()` calls)
4. Clear old CI failure tracking keys: `redis-cli -u $REDIS_URL KEYS "ci:*" | xargs redis-cli -u $REDIS_URL DEL`

