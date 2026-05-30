# GitHub Autopilot — Documentation

> Complete technical documentation for GitHub Autopilot v4.7.
> 8 directories · 10 documents · research-grade engineering detail.

---

## Where to Start

| Your goal | Start here |
|-----------|-----------|
| Understand how the system works | [System Architecture](architecture/system-architecture.md) |
| Understand the security model | [Webhook Pipeline](architecture/webhook-pipeline.md) |
| Understand the AI layer | [AI Routing](ai-system/ai-routing.md) |
| Understand what threats exist | [Threat Model](security/threat-model.md) |
| Install and use the bot | [User Setup Guide](guides/user-setup.md) |
| Deploy to production | [Render Deploy](deployment/render-deploy.md) |
| Write or run tests | [Testing Guide](testing/testing-guide.md) |
| Monitor in production | [Observability](observability/observability.md) |
| See all system diagrams | [Diagrams](diagrams/diagrams.md) |

---

## Documentation Structure

```
docs/
├── README.md                           ← you are here
│
├── architecture/
│   ├── system-architecture.md          ← MOST IMPORTANT — read first
│   └── webhook-pipeline.md             ← 7-stage security pipeline
│
├── ai-system/
│   ├── ai-routing.md                   ← provider selection, circuit breakers
│   └── autofix-engine.md               ← /autofix 5-stage pipeline
│
├── security/
│   └── threat-model.md                 ← 9 attack vectors, mitigations
│
├── deployment/
│   └── render-deploy.md                ← Render + GitHub App setup
│
├── testing/
│   └── testing-guide.md                ← 6 critical patterns, templates
│
├── observability/
│   └── observability.md                ← health, metrics, Redis reference
│
├── guides/
│   └── user-setup.md                   ← install, configure, use
│
└── diagrams/
    └── diagrams.md                     ← 7 ASCII + 3 Mermaid diagrams
```

---

## Document Summaries

### [System Architecture](architecture/system-architecture.md)
The foundational document. Covers: design goals (8 non-negotiable properties), component map with full ASCII diagram, all 4 phases of request lifecycle, data flow to Redis (every key, every TTL), reliability model, failure handling table, 4 architectural decisions with tradeoff tables (threading vs Celery, Redis SET NX, in-memory config cache, FakeRedis fallback), scalability strategy, current limitations, future architecture.

### [Webhook Pipeline](architecture/webhook-pipeline.md)
Deep dive into all 7 security stages. For each stage: what threat it prevents, exact implementation code, why this approach over alternatives, failure modes and recovery. Special attention to Stage 3 (HMAC — why `hmac.compare_digest` not `==`, the original fail-open bug and fix) and Stage 6 (replay protection — why `SET NX` not `EXISTS`+`SET`).

### [AI Routing](ai-system/ai-routing.md)
How the 4-provider LLM router works. Covers: single interface design (handlers never know which provider answered), task classification (TASK_MAP with all task strings and tiers), provider specifications table, selection algorithm with 80% usage threshold, circuit breaker state machine with code, prompt sanitization and injection defense (and its known limitations), `_extract_json` brace-depth scanner, hallucination confidence scoring with penalty table and action thresholds, cost tracking in Redis, full routing flow diagram, failure modes, and alternatives considered.

### [Autofix Engine](ai-system/autofix-engine.md)
The most complex feature. Covers: why automated code fixing is hard (3 fundamental problems), 5-stage pipeline diagram, file candidate selection algorithm, `_safe_excerpt` with truncation marker, fix plan generation (chain-of-thought reasoning design), the 70% safety guard (why 70%, false positive risk), the 3 Sprint-8 bugs with before/after code and commit messages, all failure scenarios with bot responses, current limitations, and 4-phase roadmap to sandboxed execution.

### [Threat Model](security/threat-model.md)
9 threat vectors with full analysis: forged webhooks, replay attacks, webhook flooding, privilege escalation (the original v4 bug documented), prompt injection (and bypass methods), secret leakage (35+ pattern categories), bot feedback loops, command spam, malicious PR config override. Each entry includes: attack description, impact without mitigation, mitigation code, residual risk. Security posture summary table and current unmitigated risks.

### [User Setup Guide](guides/user-setup.md)
Step-by-step: fork → create Render web service → create Redis → configure all environment variables (required + recommended + optional with sources) → create GitHub App (exact permissions and events) → install on repository → verify with `/health` → configure `.ai-repo-manager.yml` → use slash commands. Full troubleshooting section (7 common problems with solutions), updating, uninstalling.

### [Testing Guide](testing/testing-guide.md)
383 tests, 18 files, zero network calls, ~2s run time. 6 critical patterns every contributor must know: circuit breaker injection (why `patch()` doesn't work), router mock returns tuple (the `ValueError` trap), module import caching (why `patch.object` is required), falsy empty list in fixtures (the `x or [default]` bug found in Sprint 8), patch at source module (local imports), secret scanner safety (runtime assembly vs literals). Full handler test template, known gotchas, coverage targets, CI configuration.

### [Observability](observability/observability.md)
Production monitoring reference. Health endpoint anatomy (every field explained), metrics endpoint, 8 evaluation metrics with measurement methodology, structured logging (format, levels, log event catalog), complete Redis key reference with `redis-cli` commands for every key type, `/report` and `/budget` command output walkthroughs, alerting recommendations (Render health checks, UptimeRobot, Discord), performance baselines table (P50/P95 for every operation), step-by-step silent failure debugging checklist.

### [Diagrams](diagrams/diagrams.md)
7 ASCII diagrams and 3 Mermaid diagrams: full system architecture, security pipeline flowchart, AI routing decision tree, autofix engine flow, circuit breaker state machine, Redis data flow, version timeline v1→v4. Mermaid versions for GitHub's built-in renderer: system architecture graph, request lifecycle sequence diagram, circuit breaker stateDiagram.

---

## Key Design Decisions (Quick Reference)

| Decision | Chosen approach | Why | Tradeoff |
|----------|----------------|-----|----------|
| Task execution | `ThreadPoolExecutor` (6 workers) | Render free tier has no background worker | Queue lost on restart |
| Event deduplication | Redis `SET NX` (atomic) | No TOCTOU race condition | 1-hour TTL, not 72h |
| Config cache | In-memory with `RLock` | No serialization overhead | Per-process cache |
| Redis failure | `_FakeRedis` fallback | Dev convenience, production graceful | Per-process rate limits |
| LLM redundancy | 4-provider chain | No single point of failure | Complexity |
| Security default | Fail closed | Empty secret → reject, not bypass | Stricter ops setup |

---

## Evaluation Metrics

| Metric | Target | How to check |
|--------|--------|-------------|
| Webhook ACK latency | < 200ms | Render access logs |
| Handler latency | < 10s for /fix | `dispatch.start` to `dispatch.done` log delta |
| LLM latency | < 4s Groq 70B | `ai.router` log entries |
| Hallucination rate | < 5% | `hallucination.warning` / total LLM calls |
| Circuit trip rate | 0/day | `circuit.opened` log count |
| Command success rate | > 97% | `dispatch.done` / `dispatch.start` |

---

## Current Limitations (Honest Assessment)

1. **No execution sandbox** — autofix commits without running tests
2. **Single-file autofix** — multi-file bugs need manual intervention
3. **Blocklist injection defense** — bypassable via unicode or rephrasing
4. **5-minute permission cache** — revoked access persists briefly
5. **No audit log** — individual invocations not persisted
6. **Learning loop incomplete** — acceptance rate tracked but not used
7. **Embeddings partial** — vector context wired to PR review only

