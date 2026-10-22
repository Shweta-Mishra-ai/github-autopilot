# System Architecture

> The canonical description of how GitHub Autopilot is built and deployed, and
> why. Rewritten in 2026-10 from the code as it stands: the previous version
> described a 50-job in-memory pool as the queue, a 1-hour dedup TTL keyed on
> the delivery id, and "GitHub retries webhooks for 72 hours" — none of which
> was true any longer, and the last never was.

---

## 1. Deployment

Everything runs on free tiers, all of it in the cloud. There is no local model
in the standard deployment.

| Piece | What | Limits that shape the design |
|-------|------|------------------------------|
| Render web service (`render.yaml`) | Flask under gunicorn, **1 worker × 8 threads** | 512 MB RAM; sleeps when idle, so the request that wakes it waits for a cold start |
| Render Key Value (Redis), free | Queue, dedup, budgets, memory, snapshots | Small memory cap; **no disk persistence — Render may restart it at any time and everything in it is lost** |
| Groq | Primary LLM: `openai/gpt-oss-120b`, fallback `openai/gpt-oss-20b` (`LLM_PRIMARY_MODEL` / `LLM_FALLBACK_MODEL`) | Daily token and request quotas |
| Gemini | Long-context tasks and fallback: `gemini-flash-latest` (`LLM_GEMINI_MODEL`) | Daily quota |
| OpenRouter | Last-resort fallback (`LLM_OPENROUTER_MODEL`) | Small free daily allowance |
| GitHub Actions `keepalive.yml` | Pings `/ping` every 10 minutes | Best-effort: scheduled runs can be late or skipped |

The daily limits the router budgets against (`app/ai/routing_policy.py`) are
assumptions, not values read from the providers; set
`LLM_DAILY_LIMIT_<PROVIDER>_<TOKENS|REQUESTS>` to your account's plan.

Ollama is supported (`OLLAMA_HOST`, `LLM_LOCAL_ONLY`, `LLM_PREFER_LOCAL`) for
self-hosted deployments. With none of those set — the Render deployment — no
code path contacts it: the router and the gatekeeper check `OLLAMA_HOST`
before doing anything.

---

## 2. A webhook, end to end

```
GitHub ──POST /webhook──▶ verify ──▶ parse ──▶ dedup ──▶ enqueue ──▶ 202
                                                            │
             consumer threads (EVENT_QUEUE_CONSUMERS=2) ◀───┘
                    │
                    ▼
        _run_handler: record installation → check repo identity
                    → handler under the repo's daily AI budget
                    │
         OK ── done │ RETRY ── 30 s, 5 min, 30 min, then dead-letter
                    │ FAILED ── dead-letter
```

**Ingress** (`server.py:webhook`, `app/core/webhook_security.py`), in order:

1. Body size ≤ 25 MB (`413`).
2. Per-source-IP rate limit, 100/min by default (`429`). GitHub sends every
   webhook from a small IP range, so this bucket is shared by an installation.
3. HMAC-SHA256 over the raw body, constant-time, fail-closed when the secret
   is unset (`401`).
4. A timestamp check that applies only when a timestamp header is present.
   GitHub does not send one, so for GitHub deliveries replay protection is
   the dedup step below.
5. JSON object required (`400`); internal job names (`autopilot.*`) refused;
   bot senders skipped (`200`).
6. **Dedup**: a SHA-256 of the payload, `SET NX` in Redis for 24 hours
   (`app/core/idempotency.py`). The payload is what GitHub signs; the delivery
   id and event header are not, so they are not part of the key.
7. **Enqueue** on the Redis queue and answer `202`. A full queue (200 events)
   answers `503` and releases the dedup key; Redis unavailable or an envelope
   over 512 KB falls back to the in-process thread pool (6 workers, 50
   pending), which answers `503` when saturated.

**Processing** (`app/core/event_queue.py`): consumers `BLMOVE` from
`evq:pending` to `evq:processing`, run the handler and remove the envelope.
At-least-once: anything left in `evq:processing` by a crash is requeued at the
next boot. A handler raises a transient error (GitHub 5xx/429/secondary limit,
all providers down, config unreadable — `app/core/transient.py`) to get a
retry, and the queue retries it after 30 s, 5 min and 30 min before
dead-lettering it (`evq:dead`, last 50 kept).

Handlers: `pull_request` (report, review, gaps, risk), `issues` (triage),
`issue_comment` (slash commands), `push` (secret scan, dependency scan,
commit lint), `check_run` (CI failure analysis), and the internal
`autopilot.reply` job, which posts a command reply whose first attempt hit a
transient GitHub error (`app/github/deferred_reply.py`).

---

## 3. What the free tier costs, and what covers it

### Webhooks that fail are not retried by GitHub

GitHub records a delivery as failed when the server does not answer within
10 seconds or answers with an error, and **does not redeliver it on its own**
([GitHub docs](https://docs.github.com/webhooks/using-webhooks/handling-failed-webhook-deliveries)).
On this deployment that happens routinely: the delivery that wakes a sleeping
instance outlasts the 10 seconds, a full queue answers 503, the IP limit
answers 429.

`app/github/redelivery.py` does what GitHub recommends. 45 seconds after boot
(when a cold start has just lost a delivery) and every 10 minutes after, it
lists the App's deliveries from the last 6 hours and asks GitHub to redeliver
each event whose attempts all failed with no response, a 429 or a 5xx — at
most twice per event. Deliberate refusals (400/401/413) are not retried. A
redelivered event that had in fact been processed is dropped by the 24-hour
dedup. `/health` shows the last sweep; `WEBHOOK_REDELIVERY=0` turns it off.

### Redis can be emptied while the app runs

The free Key Value instance keeps nothing on disk. A restart loses the queue,
dedup keys, budgets, rollback snapshots, analytics and repository memory —
while the web process keeps running.

- **Memory** survives through the encrypted backup (`app/core/memory_backup.py`,
  configured by `MEMORY_BACKUP_KEY`, `MEMORY_BACKUP_REPO`, `MEMORY_BACKUP_TOKEN`).
  Memory is encrypted in-process before upload; the backup repository only
  ever holds ciphertext. A marker key records that this Redis has been
  reconciled with the backup; a wipe removes it. Every hour the maintenance
  tick merges the backup back in if the marker is missing — merging, never
  deleting what arrived since — and backs up once a day
  (`MEMORY_BACKUP_INTERVAL_HOURS`). While the marker is missing, backups
  refuse to run, so a half-empty Redis cannot replace a good backup.
- **Everything else** is lost on a Redis restart and rebuilt as events arrive:
  an event whose dedup key was lost can be processed a second time if GitHub
  (or the sweep) redelivers it, and `/rollback` has no snapshots from before
  the restart. A paid Key Value instance with persistence removes this class
  of loss entirely.

### Only one web process

The queue consumers, the thread pool, circuit breakers, the config and
permission caches and the rate-limit fallbacks are process-wide. `render.yaml`
and the `Procfile` pin `--workers 1`; `/health` reports `web_processes` and
warns when more than one registers. To scale out, run `worker.py` as its own
service and set `EVENT_QUEUE_CONSUMERS=0` on the web process.

---

## 4. The AI layer

`router.ask(system, user, task)` returns `(dict, LLMResponse)`; handlers never
pick a provider. Order (`app/ai/router.py:_select_provider`):

- long-context tasks (or > 6,000 prompt tokens): Gemini first;
- "fast" tasks (labels, commit lint, summaries): Groq small model, then
  Gemini, Groq large, OpenRouter;
- everything else: Groq large model while under 80 % of its daily budget;
  then, for "standard" tasks, Groq small → Gemini → OpenRouter, and for
  "deep" ones Gemini → Groq small → OpenRouter. With `LLM_QUALITY_FLOOR=high`
  a quality-sensitive task refuses rather than fall to a small model.

Each provider has a circuit breaker. A provider that refuses for a reason
retrying cannot fix (unknown model id, rejected key) is reported separately
as a configuration error on `/health`, and a retired model id is replaced
from the provider's own catalogue (`app/ai/model_catalog.py`) and reported as
a substitution. Every comment names the model(s) that wrote it.

Outputs are checked before they are posted: review findings must quote a
line of the diff (`app/handlers/pull_request/grounding.py`); answers about
code that was not shown say so, and files the answer names are checked
against the repository tree; triage shows only the fields the model actually
assessed; scores and percentages are computed from data or not shown.

Repository memory (`app/intelligence/memory.py`) is redacted when written
(code blocks removed, secret-shaped strings replaced) and recalled into
prompts — cloud prompts included, by default. `MEMORY_ALLOW_CLOUD=0` keeps it
out of every prompt. Recalled memory reaches the model inside the
untrusted-content block, labelled as recorded activity.

---

## 5. State in Redis

| Keys | What | Lifetime |
|------|------|----------|
| `evq:pending`, `evq:processing`, `evq:delayed`, `evq:dead` | The event queue | Until processed; dead list capped at 50 |
| `idem:<payload hash>` | Dedup | 24 h |
| `mem:<repo>`, `mem:hashes:<repo>`, `mem:repos` | Repository memory | Capped per repo (`MEMORY_MAX_ITEMS`) |
| `snapshot*:<repo>…` | Rollback snapshots | TTL'd |
| `analytics:<repo>:…`, `learn:<repo>:…` | Usage and autofix outcomes | 7 / 90 days |
| `limit:<repo>:ai_calls:<day>`, `llm:*` | Budgets | A day |
| `repo_id:<repo>` | Which repository a name belongs to | 400 days |
| `redelivery:*`, `maintenance:*`, `memory_backup:*` | Schedules, locks, last results | Short, or until the next run |

State is keyed by repository **name**, and a name can be reused by a
different repository. `app/core/repo_identity.py` records GitHub's numeric
repository id per name on every event and clears the name's state when the id
changes, before anything reads it.

`maxmemory-policy` is `noeviction`: when Redis is full, writes fail loudly
instead of silently evicting dedup keys.

---

## 6. Security boundaries

- Webhooks: HMAC, size and rate limits as above. `/health`, `/metrics`,
  `/graph.json` and `/setup/doctor` require `METRICS_AUTH_TOKEN`. When it is
  unset they refuse every request in production (`ENVIRONMENT=production`)
  and are open only in local development; `/ping` is always open. `/dashboard` and `/graph` are static
  pages with no data in them; they fetch the gated endpoints with a token you
  paste in. The MCP endpoint refuses everything without `MCP_API_KEY`.
- `/setup` (one-click App creation) binds its CSRF state to the browser with
  an HttpOnly cookie.
- Restricted commands (`/merge`, `/rollback`, `/release`, `/autofix`,
  `/apply`, `/secfull`, `/ignore`, `/runtests`, `/notify`) need write access,
  checked against GitHub's collaborator API and failing closed.
- Config is read from the default branch only, so a pull request cannot grant
  itself permissions by editing `.ai-repo-manager.yml`.
- Text from issues, PRs and comments is placed in delimited untrusted-content
  blocks with its own delimiters defanged; notifications escape it for Slack
  and send Discord messages with mentions disabled.

---

## 7. Known limits

1. Free Redis loses everything but backed-up memory on a restart (§3).
2. The redelivery sweep looks back 6 hours; an outage longer than that needs
   a manual redelivery from the App's settings (GitHub keeps 7 days).
3. Replay protection for GitHub deliveries is the 24-hour dedup: a captured
   signed body replayed later than that would be processed again.
4. Autofix commits generated code without running tests; review every
   autofix PR.
5. Permission lookups are cached for 5 minutes, so a revoked collaborator
   keeps access for up to that long.
