# Where we are & how to make it great

A forward-looking companion to the [reliability audit](reliability-audit.md).
The audit answers *"is it safe/stable today?"* — this answers *"what next, and
how do we get people to adopt it?"*

## Where V6 landed

Shipped and tested (726 passing, CI green):

- **Durable event queue** (Redis, bounded, at-least-once, dead-letter, fallback)
- **Security hardening** — fail-closed MCP auth, constant-time compares, tenant allowlist, boot-time config warnings
- **Local-LLM privacy mode** — code never leaves your infra (`LLM_LOCAL_ONLY`)
- **Private repo memory** — explainable ("knows *why*"), encrypted backup
- **Live ops dashboard**, **Claude Code plugin + marketplace**, **MCP registration**
- **Maintainability** — `mcp_server.py` split into `tools.py` / `handlers.py` / dispatch

## How to improve — prioritized

### P0 — before calling it production-grade
1. **Real secret scanning.** The regex + entropy scanner has false negatives.
   Shell out to `gitleaks` or `detect-secrets` for high assurance; keep the
   regex layer as a fast pre-filter.
2. **HA Redis.** Today Redis is a single point of failure. Move to a managed
   HA Redis (or Upstash) for any real SLA. The queue + backup cushion it, but
   it's still a SPOF.
3. **Secret rotation runbook.** Env secrets are the crown jewels — document and
   automate rotation of `GITHUB_PRIVATE_KEY`, `MCP_API_KEY`, provider keys.
4. **SAST in CI.** Add CodeQL/Semgrep (or the SonarQube already wired for local
   review) as a required check.

### P1 — reliability & scale
5. **Move per-process caches to Redis** (token/config/permission caches) so
   `gunicorn --workers > 1` becomes safe → real horizontal scale on one service.
6. **Alerting.** Wire the existing Slack/Discord notifiers to fire on
   `events.dropped`, `queue.dead`, `ratelimit.failopen`, all-providers-down.
7. **Dashboard hardening.** Add auth in front of `/dashboard` itself (not just
   its data), and surface `intelligence.index_failed` + queue depth trends.

### P2 — smarter brain
8. **Semantic memory.** Swap lexical recall for local Ollama embeddings
   (`/api/embeddings`) — still 100% local, much sharper retrieval.
9. **Auto-capture decisions.** When a maintainer merges or `/apply`s a fix,
   record a `decision` with the rationale automatically (feed `learning.py`).
10. **Feedback loop.** Track which suggestions get accepted and bias future
    confidence thresholds per repo.

### P3 — maintainability
11. Split the remaining long files: `handlers/comments/publisher.py` (570),
    `handlers/pull_request.py` (480), `handlers/autofix.py` (451).
12. Raise the coverage floor from 60% toward 80%; add integration tests for
    the Redis-down and queue-saturation paths.

## How to get others to use it (adoption)

- **One-click deploy that actually works end-to-end** — the Render button + a
  post-deploy checklist (set `MCP_API_KEY`, `METRICS_AUTH_TOKEN`). A boot page
  that shows "3/5 env vars set" would remove most setup friction.
- **A hosted free demo instance** people can try before self-hosting.
- **Publish the plugin** so `/plugin marketplace add …` is discoverable; list it
  wherever Claude Code plugins are indexed.
- **A short demo video/GIF** of `/fix` → PR in seconds (the static demo SVG is a
  start).
- **A docs site** (mkdocs) from the existing `docs/` tree.
- **"Good first issue" labels** + the existing `CONTRIBUTING.md` to invite PRs.

## "Nothing can fail or be infected" — the honest version

- **Infected:** the bot never executes untrusted repo code (no `eval`/`exec`/
  `subprocess`/`pickle`), so a malicious repo can't run code on the host. The
  only powerful capability is the GitHub App token, and it's gated. Keep it that
  way — never add a code-execution path without a real sandbox.
- **Fail:** it degrades instead of crashing (Redis down → thread-pool fallback,
  provider down → circuit breaker, queue full → 503 → GitHub retries). "Never
  fails" is not achievable; "fails safe and visibly" is — and that's the target.
  Every remaining silent path is now instrumented.
