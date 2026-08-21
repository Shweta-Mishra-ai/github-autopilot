# Reliability, Scalability & Isolation Audit

A grounded look at whether the system is safe, durable, and scalable — and where
the real edges are. Written against the V6 codebase.

## 1. Isolation — can a connected repo infect the host?

**No remote-code-execution path exists.** A scan of `app/` for `eval`, `exec`,
`subprocess`, `os.system`, `shell=True`, and `pickle` returns **zero matches**.
The bot never clones or runs a target repo's code — it reads content via the
GitHub API and calls LLMs. So a malicious repository, issue, or PR **cannot
execute code on the server**; no OS-level sandbox is required for that class of
attack because untrusted code is never run.

What *is* powerful is the **GitHub App installation token** (Contents/PR/Actions
write). Its abuse is gated:

| Path | Guard |
|------|-------|
| Forged webhook | HMAC-SHA256; no secret → rejected |
| Restricted commands (`/merge` `/autofix` `/apply` `/rollback`) | maintainer-only, fail-closed permission check |
| `/autofix` file writes | blocked from CI/`.env`/security modules + path-traversal guard; requires human `/apply` then a merge |
| Prompt injection (issue/PR text) | biases LLM *text* only — no execution; sanitizer strips known patterns |
| MCP `run_command` | `MCP_API_KEY` required (fail-closed 503) + optional `MCP_ALLOWED_INSTALLATIONS` |

**Real residual risk:** the env secrets (`GITHUB_PRIVATE_KEY`, API keys). If the
host is compromised they leak. Mitigation: rotate them, never log them, keep the
Render service private.

## 2. Silent failures — is anything failing invisibly?

Audited every `except Exception: pass`. They split cleanly:

- **Critical paths fail closed and log** — webhook verification, dispatch
  (`server._run_handler` catches, logs, and increments error metrics), auth,
  and the event queue. Nothing critical is swallowed.
- **Optional enhancements degrade silently by design** — metrics, cache,
  analytics, audit-log writes, triage-context lookups. Swallowing here is
  correct: telemetry must never break a request.
- **Fixed gap:** repo file-indexing (`push._index_changed_files`) previously
  swallowed with *zero* logging — a repo that never indexed was undiagnosable.
  It now logs at debug and increments `intelligence.index_failed`, so it's
  observable on `/metrics` and the dashboard.
- **Fail-open guards are observable:** when Redis is down the per-user rate limit
  fails open (bot stays usable) but increments `ratelimit.failopen` and logs a
  warning — the bypass is visible, not hidden.

**Verdict:** no dangerous silent failures. The one truly-silent optional path is
now instrumented.

## 3. Scalability

Runtime: `gunicorn --workers 1 --threads 8` → durable Redis queue → in-process
consumer group (2 threads) → bounded `ThreadPoolExecutor` (6) as fallback.

Everything is **bounded** — no unbounded growth is possible:

| Limit | Value | Purpose |
|-------|-------|---------|
| Event queue length | 200 | over → 503 → GitHub redelivers |
| Envelope size | 512 KB | oversized → direct dispatch, not Redis |
| Webhook payload | 25 MB | reject oversized bodies |
| Per-IP rate limit | 100 / min | flood protection |
| Per-user cmd limit | 10 / hr | abuse protection |
| Memory per repo | 500 items | bounds free-tier Redis |
| Dead-letter | 50 | bounded debugging buffer |

**Vertical** scaling fits the free tier. **Horizontal** scaling needs *zero code
changes*: run [`worker.py`](../../worker.py) as a separate Render worker and set
`EVENT_QUEUE_CONSUMERS=0` on the web service — the producer/consumer split is
already there. `workers=1` is intentional (the in-memory caches are per-process);
raising it requires moving those caches to Redis first — documented in
`thread_pool.py`.

## 4. Durability

- **Events** survive restart/deploy/crash: parked in Redis before the `202` ACK;
  work stranded in `evq:processing` is requeued at boot (`recover_stale`); poison
  events dead-letter after 2 attempts.
- **Idempotency** keys live 24 h — matches GitHub's retry window; Redis runs
  `noeviction` so they're never silently dropped.
- **Memory** ("the brain") has an encrypted client-side backup
  (`memory_backup.py`, Fernet, key never leaves the process), exported on a
  15-day schedule by `app/core/maintenance.py` and restored at boot **only when
  memory is empty**. The asymmetry is deliberate: export is safe to automate,
  restore overwrites live data, so restore is gated on a condition that makes
  it non-destructive by construction rather than by being careful about when it
  is called. Nothing on a timer can reach the restore path — a test asserts the
  maintenance module does not so much as name it. A `python -m
  app.core.memory_backup export|restore` CLI covers deliberate migrations.
- The 15-day cadence is stored in Redis as a **due time**, not a `sleep()`. On
  a free tier that restarts on deploy and on idle, a thread sleeping for 15
  days would never fire; the due time survives restarts, is advanced *before*
  the work starts so a crashed pass costs one cycle rather than retrying
  hourly, and is claimed with `SET NX` so only one of N gunicorn workers runs
  it. Visible on `/health` as `maintenance.next_run_at` / `maintenance.overdue`.

## 5. Stability

Per-provider circuit breakers, GitHub client ret/backoff on 5xx, graceful
SIGTERM drain of both the consumer group and the thread pool. Config/permission
values are validated on load and fall back to defaults instead of crashing.

## Known residual risks (honest list)

1. **Single Redis instance** = SPOF. Acceptable on free tier (fallback + backup
   cushion it); upgrade to an HA Redis for production SLAs.
2. **Regex secret scanner / prompt-injection filter** have false negatives —
   they are a signal, not a guarantee. Pair with `gitleaks`/`detect-secrets` for
   high assurance.
3. **Env secrets are the crown jewels** — rotate on a schedule.
4. **Two deploy-config gaps** (now warned at boot): unset `METRICS_AUTH_TOKEN`
   leaves `/health` + `/metrics` public; unset `MCP_API_KEY` disables the plugin.
