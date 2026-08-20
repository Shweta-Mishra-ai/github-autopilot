<div align="center">

<img src="assets/logo.svg" alt="GitHub Autopilot logo" width="120"/>

# GitHub Autopilot

**Your repository's AI co-pilot. Fix bugs, review PRs, scan secrets — from a single comment.**

**The self-hosted one**: runs on your own free-tier infra, and in
[local-LLM mode](#private-mode--keep-code-on-your-own-hardware) your code
**never leaves your hardware** — the private-repo alternative to SaaS review bots.

[![CI](https://github.com/Shweta-Mishra-ai/github-autopilot/actions/workflows/ci.yml/badge.svg)](https://github.com/Shweta-Mishra-ai/github-autopilot/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![Tests](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FShweta-Mishra-ai%2Fgithub-autopilot%2Fbadges%2Ftests.json)](https://github.com/Shweta-Mishra-ai/github-autopilot/actions/workflows/ci.yml)
[![Server Health](https://github.com/Shweta-Mishra-ai/github-autopilot/actions/workflows/keepalive.yml/badge.svg)](https://github.com/Shweta-Mishra-ai/github-autopilot/actions/workflows/keepalive.yml)
[![MCP](https://img.shields.io/badge/MCP-server-a371f7?logo=anthropic&logoColor=white)](docs/mcp-setup.md)
[![License: MIT OR Apache-2.0](https://img.shields.io/badge/License-MIT%20OR%20Apache--2.0-22c55e.svg)](LICENSE)
[![Deploy to Render](https://img.shields.io/badge/deploy-Render-46E3B7?logo=render&logoColor=white)](https://render.com/deploy)
[![Sponsor](https://img.shields.io/badge/Sponsor-%E2%9D%A4-db61a2?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/Shweta-Mishra-ai)

<img src="assets/demo.svg" alt="Illustration: /fix command in a GitHub issue, bot replies with root cause, fix and test" width="720"/>

<sub>*Simulated output for illustration — see the [eval suite](evals/) for measured behaviour.*</sub>

</div>

---

## ⚠️ V7 behaviour changes

If you ran V6, three things now behave differently. All three exist to make the
bot quieter and more honest.

| Before | Now |
|--------|-----|
| A PR open posted **4 comments**, every push posted 2 more, none ever edited | **One sticky comment per PR**, edited in place. Collapsible sections. |
| Every secret finding opened an issue | Only **critical/high** severity does. Medium/low are logged. |
| A push with nothing to report still commented | The bot **stays silent** when it has nothing to say. |

Two more, less visible:

- **Repo memory is on by default.** It was opt-in, which meant it never worked in
  cloud deployments. Content is now redacted before storage — code bodies stripped,
  secret-shaped strings replaced. Set `MEMORY_ALLOW_CLOUD=0` for the old behaviour.
  See [docs/ai-system/memory.md](docs/ai-system/memory.md).
- **Unparseable model output no longer renders.** Previously a non-JSON response
  fell through to defaults and published "Score: 7/10 — no issues found" for a
  review that never ran. The bot now says it could not analyse the change.

---

## Why Autopilot?

| | |
|---|---|
| ⚡ **27 slash commands** | `/fix` `/security` `/merge` `/autofix` `/rollback` … right in issue/PR comments |
| 🛡️ **Safety-first automation** | Confidence gates, guardrails, human-in-the-loop `/apply`, maintainer-only permissions |
| 🔁 **Durable event queue** | Webhooks parked in Redis — survive restarts, deploys and crashes; if Redis itself dies, degrades to best-effort in-process dispatch (and says so in the logs) |
| 🧠 **5-provider AI failover** | Groq 70B → Groq 8B → Gemini → OpenRouter, with per-provider circuit breakers |
| 🔒 **Local-LLM privacy mode** | Run on your own Ollama — set `LLM_LOCAL_ONLY=1` and code **never** leaves your infra |
| 🧩 **Private repo memory** | Learns your repo's fixes & decisions; sensitive context stays local, [encrypted backup](docs/ai-system/memory.md) for durability |
| 🔐 **Security scanning** | Secret detection on **every push to every branch**, dependency CVE checks |
| 📍 **Inline PR reviews** | Findings land as line-anchored review comments with committable suggestions — not a wall-of-text comment |
| 📏 **Honest AI output** | Every comment discloses which model wrote it; optional [quality floor](#changelog) refuses to degrade reviews to a small model; [measured by evals](evals/), not vibes |
| 🔌 **MCP server built in** | Call Autopilot tools from Claude Code, Cursor, or Codex — [setup guide](docs/mcp-setup.md) |
| 📊 **Live ops dashboard** | `/dashboard` — queue depth, event throughput, provider circuit-breakers, thread pool. Zero build, no CDN |
| 💸 **Runs on free tier** | Render free web service + free Redis. $0/month |

---

## Quickstart — deploy in 10 minutes

### 1. Create a GitHub App

1. **github.com/settings/apps** → New GitHub App
2. Webhook URL: `https://github-autopilot-1.onrender.com/webhook`
3. Webhook secret: `python3 -c "import secrets; print(secrets.token_hex(32))"`
4. Permissions: Issues ✏️ · Pull requests ✏️ · Contents ✏️ · Actions ✏️
5. Subscribe to: Push · Pull request · Issue comment · Issues
6. Download the private key (`.pem`)

### 2. Deploy

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy)

Or manually: fork this repo → Render → **New Blueprint** → connect fork ([render.yaml](render.yaml) does the rest).

### 3. Environment variables

| Variable | Where to get it | Required |
|----------|----------------|----------|
| `GITHUB_APP_ID` | App settings page (numeric ID) | ✅ |
| `GITHUB_PRIVATE_KEY` | Contents of the `.pem` file | ✅ |
| `GITHUB_WEBHOOK_SECRET` | The secret from step 1 | ✅ |
| `GROQ_API_KEY` | [console.groq.com](https://console.groq.com) — free | ✅ |
| `REDIS_URL` | Auto-wired by render.yaml | ✅ |
| `MCP_API_KEY` | `python3 -c "import secrets; print(secrets.token_hex(32))"` | for MCP |
| `METRICS_AUTH_TOKEN` | Any strong random string | recommended |
| `GEMINI_API_KEY` / `OPENROUTER_API_KEY` | Optional extra AI fallbacks | optional |

### 4. Install & verify

Install the GitHub App on your repos, then:

```bash
curl https://github-autopilot-1.onrender.com/ping
# → {"status": "ok", "version": "7.1.1"}
```

> **Cold starts** — the demo instance runs on Render's free tier. A scheduled
> [keep-alive workflow](.github/workflows/keepalive.yml) pings it every 10 minutes
> to keep it warm (the badge above goes red if production is actually down), but if
> a ping window is missed the first request can take **~50 s** while the instance
> wakes. If a request stalls, retry once.

Comment `/health` on any issue. The bot replies with a repo health grade. Done. ✈️

---

## At a glance

<!-- autopilot:stats:start -->
| | |
|---|---|
| Modules | 84 |
| Lines of code | 16,595 |
| Slash commands | 27 |
| MCP tools | 9 |
| Internal imports | 244 |
<!-- autopilot:stats:end -->

<sub>Regenerated from the code by CI — see [managed README sections](#managed-readme-sections).</sub>

---

## Commands

Type any of these in a GitHub issue or PR comment:

| Command | Description | Who |
|---------|-------------|-----|
| `/fix` | AI bug fix with root cause + test | Anyone |
| `/explain` | Plain-English explanation | Anyone |
| `/improve` | Concrete improvement suggestions | Anyone |
| `/test` | Generate pytest test cases | Anyone |
| `/docs` | Generate docstrings + README section | Anyone |
| `/refactor` | Refactoring with before/after | Anyone |
| `/perf` | Performance analysis (O(n²), N+1, …) | Anyone |
| `/gaps` | Test coverage gap analysis | Anyone |
| `/arch` | Architecture review | Anyone |
| `/ci` | Analyze CI failure | Anyone |
| `/security` | Secret + dependency scan on PR | Anyone |
| `/secfull` | Full repo security scan | Maintainers |
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
| `/ignore <rule>` | Teach the bot to stop flagging a pattern in this repo | Maintainers |
| `/autofix` | Auto-apply code improvements (human-confirmed via `/apply`) | Maintainers |

---

## Architecture

```mermaid
flowchart TB
    GH[GitHub webhook] --> SEC["webhook_security<br/>HMAC-SHA256 · replay · IP rate limit"]
    SEC --> IDEM["idempotency<br/>24h Redis dedup"]
    IDEM --> Q["event_queue (Redis)<br/>durable · bounded · at-least-once"]
    Q --> C["consumer group<br/>(in-process, 2 threads)"]
    IDEM -. "Redis down → fallback" .-> TP["thread_pool<br/>bounded, backpressure"]
    TP --> H
    C --> H["handlers<br/>push · pull_request · issues · comments"]
    H --> R["ai/router<br/>Groq 70B → 8B → Gemini → OpenRouter"]
    R --> CB["circuit breakers<br/>per provider"]
    H --> GHA["GitHub API client<br/>retry · rate-limit aware"]
    IDE["Claude Code / Cursor / Codex"] -->|"MCP · Bearer auth"| MCP["/mcp endpoint<br/>fail-closed"]
    MCP --> H
```

<details>
<summary><b>Module dependency graph</b> — generated from the AST, never hand-drawn</summary>

<br/>

The diagram above is the request flow, written by hand. The one below is
derived from the import graph on every CI run, so it cannot drift from the
code. Explore it interactively at [`/graph`](#codebase-map), or regenerate with
`python -m app.intelligence.codegraph app server.py worker.py`.

<!-- autopilot:architecture:start -->
```mermaid
graph LR
    ai["ai<br/>14 modules"]
    core["core<br/>20 modules"]
    github["github<br/>8 modules"]
    handlers["handlers<br/>21 modules"]
    intelligence["intelligence<br/>6 modules"]
    mcp["mcp<br/>4 modules"]
    other["other<br/>5 modules"]
    security["security<br/>6 modules"]
    ai --> core
    ai --> github
    core --> ai
    core --> github
    core --> intelligence
    core --> security
    github --> ai
    github --> core
    github --> other
    handlers --> ai
    handlers --> core
    handlers --> github
    handlers --> intelligence
    handlers --> mcp
    handlers --> security
    intelligence --> ai
    intelligence --> core
    mcp --> ai
    mcp --> core
    mcp --> github
    mcp --> handlers
    mcp --> intelligence
    mcp --> other
    mcp --> security
    other --> ai
    other --> core
    other --> github
    other --> handlers
    other --> mcp
    security --> core
    security --> github
```
<!-- autopilot:architecture:end -->

</details>

**The queue is the backbone.** Every webhook is parked in Redis *before* the
`202` ACK, then consumed by an in-process worker group:

- **Durable** — deploys/restarts/crashes don't lose events; stranded work is requeued at boot, poison events dead-letter after 2 attempts
- **Bounded** — queue capped at 200 events, envelopes at 512KB, dead-letter at 50: nothing grows unbounded on a 512MB / 25MB-Redis free tier
- **Backpressured** — queue full → `503` → GitHub redelivers automatically
- **Degradable** — Redis down → automatic fallback to the bounded thread pool (reduced durability, still working)
- **Scale-ready** — need more throughput later? Run [`worker.py`](worker.py) as a Render worker service and set `EVENT_QUEUE_CONSUMERS=0` on web. Zero code changes.

**Other key decisions:**

- Idempotency keys live 24h — matches GitHub's webhook retry window
- Redis runs `noeviction` — dedup/queue keys are never silently evicted
- MCP + `/metrics` auth fail **closed** with constant-time compares
- Secret scanning runs on all branches, not just main
- Confidence gates: every automated action needs a per-action threshold (e.g. auto-merge ≥ 0.95)

---

## Use it from your IDE (MCP)

Autopilot ships an MCP server — analyze PRs, scan secrets, and generate tests
from Claude Code, Cursor, or Codex without leaving your editor:

```bash
claude mcp add --transport http github-autopilot \
  https://github-autopilot-1.onrender.com/mcp \
  --header "Authorization: Bearer YOUR_MCP_API_KEY"
```

Full client configs, tool reference, and troubleshooting: **[docs/mcp-setup.md](docs/mcp-setup.md)**

---

## Use it from your IDE (Claude Code plugin)

Install the commands + MCP server in one step:

```
/plugin marketplace add Shweta-Mishra-ai/github-autopilot
/plugin install github-autopilot
```

Point it at your deployed instance:

```bash
export GITHUB_AUTOPILOT_URL="https://github-autopilot-1.onrender.com/mcp"
export MCP_API_KEY="<your server's MCP_API_KEY>"
```

Then, from Claude Code: `/github-autopilot:review owner/repo 42` ·
`/github-autopilot:fix owner/repo 17` · `/github-autopilot:security file.py` ·
`/github-autopilot:health owner/repo`. Full details in [`plugin/README.md`](plugin/README.md).

---

## Private mode — keep code on your own hardware

By default the bot sends code to Groq/Gemini/OpenRouter. For private or
regulated repos, point it at a local [Ollama](https://ollama.com) instead —
source code never leaves your infrastructure:

```bash
ollama pull llama3.1:8b
```

```bash
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llama3.1:8b
LLM_LOCAL_ONLY=1     # Ollama or nothing — no cloud provider is ever contacted
# LLM_PREFER_LOCAL=1 # softer: try local first, fall back to cloud on failure
```

In `LLM_LOCAL_ONLY` mode the router **fails closed** — if Ollama is down, calls
error out rather than silently leaking to a cloud API. `cost_usd` is always `0`.

---

## Configuration

Drop `.ai-repo-manager.yml` in your repo root (the filename predates the
GitHub Autopilot rename and is kept so existing installs don't break):

```yaml
push:
  scan_secrets: true          # always on for all branches
  scan_dependencies: true

confidence:
  thresholds:
    auto_merge: 0.95
    fix_command: 0.75

commands:
  permissions:
    maintainer_only: [merge, rollback, release]

bot:
  enabled: true               # master kill switch — false stops everything
  footer: "*Powered by GitHub Autopilot*"

commands:
  enabled: [fix, explain, health]   # optional allow-list; omit to keep all commands
```

All keys are validated on load — bad values log a warning and fall back to safe defaults.

**Config is read from your default branch, never from a pull request.** This is
deliberate: config decides who may merge, whether auto-merge runs, and whether
secrets are scanned, so honouring it from a PR head would let any contributor
grant themselves those rights by editing the file inside their own PR. Config
changes take effect once merged — the same trust boundary GitHub Actions applies
to workflow permissions.

Two behaviours worth knowing:

- Omitting `commands.enabled` means *no restriction* — every command stays
  available. It is an allow-list, not a registry, so you never have to keep it in
  sync with new releases. An explicit `enabled: []` disables everything.
- `bot.enabled: false` stops all handlers: PRs, issues, pushes, CI and commands.

---

## Codebase map

An interactive, force-directed view of every module and what imports what,
served at `/graph`:

- **Click a node** to see exactly what imports it and what it imports
- **Import cycles** are detected and flagged — they are what makes a module
  impossible to test on its own
- **Unreferenced modules** are listed: nothing in `app/`, `server.py` or
  `worker.py` imports them, which usually means dead code
- **Hotspots** rank modules by size × how many things depend on them — the
  files that are expensive to change

The data comes from `python -m app.intelligence.codegraph`, which reads the AST
and **never imports the code it analyses**, so it is safe to point at any
repository. CI regenerates it and fails a PR whose committed copy is stale.

```bash
python -m app.intelligence.codegraph app server.py worker.py \
  --out docs/diagrams/codegraph.json
```

`/graph.json` is auth-gated with `METRICS_AUTH_TOKEN`, the same as `/health` —
a dependency graph is a map of the whole system. The same data is available to
your IDE through the `codebase_map` MCP tool.

---

## Managed README sections

Some facts in this README restate what the code already knows: module counts,
the command registry, the dependency graph. Those rot silently — this file
claimed the MCP endpoint had "8 tools" for exactly as long as it took someone
to add a ninth.

Blocks between `autopilot` markers are regenerated from the code. Paste an
empty pair where you want the content — writing `NAME` as one of the region
names below:

```markdown
<!-- autopilot:NAME:start -->
<!-- autopilot:NAME:end -->
```

Available regions: `stats`, `architecture`, `commands`. Everything outside a
marker pair is hand-written and never touched by the bot, and a repository with
no markers gets no edits at all — you opt in one region at a time by pasting a
marker pair where you want the content.

Refreshes arrive as a pull request, never as a direct commit to the default
branch. Set `README_SELF_UPDATE_REPO=owner/repo` to enable it for the
deployment's own repository.

---

## Local development

```bash
git clone https://github.com/Shweta-Mishra-ai/github-autopilot.git
cd github-autopilot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # fill in your credentials
python server.py
```

```bash
pytest tests/ -v              # 1054 tests, 80% coverage — the CI badge is the live number
ruff check app/               # lint
```

---

## Security model

- **Fail closed everywhere it matters**: unset webhook secret → boot refuses; unset `MCP_API_KEY` → MCP returns 503; token compares are constant-time
- HMAC-SHA256 signature verification on every webhook, replay + IP rate limiting (spoof-resistant)
- Autofix cannot touch CI workflows, Dockerfiles, env files, or security modules (path allowlist + prefix blocklist + traversal guard); changes require human `/apply`
- Optional `MCP_ALLOWED_INSTALLATIONS` allowlist for tenant isolation
- Bot-loop prevention on all event handlers
- Prompt-injection mitigation: input sanitization + delimiter-wrapped user content
- **No code-execution path**: the bot never runs untrusted repo code (no `eval`/`exec`/`subprocess`/`pickle`) — a malicious repo cannot execute code on the host

Full analysis: [reliability & isolation audit](docs/architecture/reliability-audit.md) · where we're headed: [roadmap](docs/architecture/roadmap.md).

Found a vulnerability? Please email rather than opening a public issue.

---

## Changelog

### V7.1.1 — 2026-08-03

- Removed `notifications.on_health_degraded` and the `notify_health_degraded` / `notify_ci_failure` / `notify_stale_closed` functions. Nothing could trigger any of them — the periodic health monitor was deleted in v6.1.0 and there is no stale-issue sweep — so these were alerts the product advertised and could never send. The v7.1.0 "every config key is read" check passed them because the toggle was wired even though the feature was unreachable; the check is now stricter.
- `notify_all_providers_down` is wired rather than removed, at most once per 15-minute window. A total outage affects every command at once, so an un-deduplicated alert would page the operator dozens of times for a single incident.
- `check_archived_repo()` had zero callers, so the bot commented and reviewed on archived repositories, which are read-only by intent. Now checked in the PR and issue handlers.

### V7.1.0 — 2026-08-03

Pre-launch audit. The theme is configuration the product documented and then ignored.

- **Thirteen dead config keys wired or removed.** `bot.enabled` — the documented master kill switch — had zero callers, so setting it to `false` left the bot fully active. `commands.enabled` was never enforced. `auto_merge.allowed_risk_levels` was never consulted, so a user restricting auto-merge to low-risk PRs still had high-risk ones merged. Every `notifications.on_*` toggle was ignored. `ai.primary_model` and friends sat in repo config where nothing could read them — model choice is a deployment concern (the router is a process-wide singleton), so they are now `LLM_PRIMARY_MODEL` / `LLM_FALLBACK_MODEL` env vars.
- **`/ignore` is now maintainer-only.** It writes to persistent repo memory, which V7 injects into every later prompt, but it was ungated: any commenter on a public repo could poison the context all subsequent commands saw — stored prompt injection that outlives the comment.
- **Per-repo AI budget is enforced.** `check_repo_rate_limit()` and `increment_repo_usage()` existed with zero callers, so `REPO_DAILY_AI_LIMIT` did nothing and one busy repository could drain the whole free-tier quota.
- **Review targets code, not licence files.** The review budget is spent by file kind first, then change size. Previously files were taken in GitHub's alphabetical order, so a PR touching `LICENSE`/`CONTRIBUTING`/`MANIFEST` exhausted the budget before reaching a single source file — and then reported a coverage score for code it had never read.
- **The command registry is no longer duplicated.** It lived in four places and had already drifted; `ALL_COMMANDS` is now the only source, and an absent `commands.enabled` means "no restriction" rather than "everything off".
- Config is documented as read from the default branch, never a PR head — a trust boundary, since config decides who may merge. Pinned by a test so it is not "fixed" into a privilege-escalation hole.
- New `tests/test_prelaunch_audit.py` checks these as classes rather than cases: every config key must be read, every `Config` helper must have a caller, any command reaching `remember()` must be gated, every command must be documented, and versions must agree across all manifests.

### V7.0.0 — 2026-07-27

**Correctness — the bot no longer fabricates output**
- Unparseable model responses (`{"raw": ...}`) fail closed instead of falling through to validator defaults. A non-JSON response used to render as "Score: 7/10 — ✅ No issues found" for a review that never happened.
- `validate_code_review` returned the assessment as `verdict` while the renderer read `summary` — **every** code review shipped with a blank summary. Second occurrence of this bug class after `improved_title`/`suggested_title`.
- `critical` was missing from `VALID_PRIORITIES`, so every critical issue was silently relabelled `medium` (this repo's own security issue #76 carries `priority: medium`). Same for type `refactor` and complexity `epic`. `time_estimate` was requested and discarded, so the Est. Effort row could never render.
- Hallucination detection guarded `/fix` and nothing else — 29 of ~30 output paths were unchecked. All commands now route through `app/ai/guarded.py`, with a structural test so a new command cannot skip it.

**Noise — comment volume cut hard**
- One sticky comment per PR, edited in place, replacing four on open plus two per push.
- Secret scanning switched to `enhanced_secrets` (the "drop-in replacement with false-positive reduction" that `push.py` never actually used) with a critical/high severity floor.
- Dedup now **fails closed**. `_already_reported` returned `False` on Redis errors — meaning "file it" — and the key hashed the *set of pattern names*, so different finding mixes bypassed each other. Evidence: issues #47/#50/#52/#54/#55/#59/#60 opened inside 73 seconds.
- CI had no dedup at all: a 5-job matrix failure produced 5 AI analyses and 5 comments. Now one per commit SHA.
- Code review batched into one LLM call instead of one per file (~7 calls per PR open → ~3).

**Intelligence — the subsystems are actually connected**
- Repo memory had **no write path**: nothing in the application called `remember()`. Added at `/merge`, `/apply` and triage.
- Recall was opt-in and therefore inert in every cloud deployment. Now on by default with write-time redaction; `MEMORY_ALLOW_CLOUD=0` opts out.
- `ConfidenceGate` compared every threshold against the model's *self-reported* confidence — a number it invents. Replaced with computed signals (field completeness, hallucination check, diff-anchor rate), with the model's claim at the lowest weight. `_review_code` was also passed the gate and never called it.

**Security (#76)**
- Zero-width stripping, whitespace collapse, and fail-closed rejection for critical-severity patterns.
- `wrap_user_content` had **zero production callers** — every handler interpolated raw user text into prompts. Now wired into every prompt site. See [docs/security/prompt-injection.md](docs/security/prompt-injection.md).

**Tests:** 908 → 1017. New tests assert on *rendered output* rather than validator return values — the gap that let all four correctness bugs survive the previous suite.

### V6.3.0 — 2026-07-16
- **CI security gate actually gates**: `pip-audit` had a trailing `|| true`, so the "Security" job could never fail even though `release` depends on it. 17 real CVEs across `flask`, `requests`, `PyJWT`, and `cryptography` (used for JWT signing and the encrypted memory backup) had gone silently unpatched as a result — all bumped, `pip-audit` now clean and blocking.
- **Gemini token-tracking bug fixed**: `_track()` used `incr()` (+1 per call) instead of `incrby(tokens)` — the identical V4 bug already fixed in `groq.py` but missed in `gemini.py`. `/budget` data for Gemini has been meaningless since it shipped. Caught by new tests (`gemini.py` coverage 23% → 90%).
- **Silent-failure audit**: all 26 bare `except Exception: pass` blocks in `app/` now log at debug/warning, so Redis and GitHub API degradation is observable instead of invisible.
- **Dead code removed**: `app/ai/prompt_builder.py` (297 lines, zero callers, zero tests) — a duplicate of prompt construction handlers already do inline. `learning.py` itself is confirmed wired (`record_fix_accepted`, `record_autofix_merged`).
- Local dev checkout re-synced (was 3+ weeks behind `main`) and MCP registration re-verified live against the deployed server.

### V6.2.0 — 2026-07-11
- **Inline PR reviews**: findings now post as a real GitHub Review with line-anchored comments, snapped onto actual diff lines, with committable ```suggestion blocks for safe single-line fixes. Automatic fallback to the classic issue comment if the Reviews API rejects a payload — a mapping bug can never lose a review.
- **AI evals** ([evals/](evals/)): golden issues + PR diffs with planted bugs (SQL injection, hardcoded secret, N+1, path traversal, plus a clean-diff over-flagging check), pushed through the *real* production code paths and scored deterministically. Manual `Evals` workflow in Actions.
- **Model disclosure**: every bot comment states which model produced it. **Quality floor** (`LLM_QUALITY_FLOOR=high`): reviews/fixes refuse to run on a basic-tier model instead of silently degrading to 8B.
- **Learning loop finally wired** (shipped unit-tested-but-unused in V6.0): `/apply` and merging a bot autofix branch now record acceptance; future `/fix` prompts inject the learned repo conventions.
- **Command rate limit enforced during Redis outages** (was fail-open) via a bounded in-memory window. **MCP named API keys** (`MCP_API_KEYS=laptop:tok1,ci:tok2`) with per-client revocation and an attributable audit log. **Redis memory watermark** on `/health` (the 25MB free tier fails writes when full — now visible before it bites).
- Honesty pass: durability claim corrected (Redis-down fallback is best-effort and now says so), demo labeled as simulated, `/` endpoint no longer reports the pre-rename app name.

### V6.1.1 — 2026-07-10
- **Honest badges**: the "tests: N passing" badge is now generated by CI itself — a `badges` job counts the passes from a real run on `main` and publishes the number; it can no longer drift from reality. New **Server Health** badge backed by a scheduled production ping.
- **No more cold-start surprises**: keep-alive workflow pings production every 10 minutes (Render free tier sleeps at 15 min idle) and turns red + emails the owner if the server is actually down. README now states the ~50 s cold-start worst case explicitly.
- **Event-queue fixes** (PR #69): eliminated constant "Timeout reading from socket" log spam, fixed a `TypeError` crash in confidence-gated `pull_request` handling, and a deadlock in `get_redis_blocking()`.
- Docker cleanup: removed a stale ChromaDB/SQLite `mkdir` from the Dockerfile and unused `SCHEDULED_*` env vars from docker-compose (that cron handler was deleted in V6.1.0).

### V6.1.0 — 2026-07-05
- **Live-validated, not just mock-tested**: booted the real app and drove it — real HMAC-signed webhooks through the full dispatch pipeline, `LLM_LOCAL_ONLY` refusing a genuinely unreachable network target, a full memory → encrypted-backup → restore round trip with an explicit no-plaintext-in-ciphertext assertion. Two real bugs found and fixed during this process: a duplicate/ungated release workflow, and the secret scanner flagging its own test fixtures.
- **+84 tests** (732 → 816): full integration coverage for the webhook pipeline, the local-LLM privacy guarantee, the comment-dispatch entry point (all 25 commands' routing verified), the GitHub Security API reader, and Slack/Discord notifications. Coverage 65% → 75%.
- **Two dead files removed** (verified via grep, not assumed): the pre-router V4 LLM client and an unwired V3 cron handler.
- Documentation corrected to match reality: the testing guide referenced a test file that no longer existed and a CI config that didn't match `.github/workflows/ci.yml`; both rewritten from verified values.

### V6.0.0 — 2026-07-04
- **Durable Redis event queue** — webhooks survive restarts; bounded, at-least-once, dead-letter, thread-pool fallback
- **Fail-closed MCP auth** + constant-time token compares + installation allowlist
- **Local-LLM privacy mode** (Ollama) — code never leaves your infra
- **Private repo memory** — explainable ("knows why") + encrypted backup
- **Live ops dashboard** (`/dashboard`) and **Claude Code plugin + marketplace**
- **Observability** — boot warnings for missing auth tokens; silent optional-path failures now instrumented
- **Maintainability** — `mcp_server.py` split into `tools.py` / `handlers.py` / dispatch
- Version single source of truth; config cross-tenant leak fixed; dead code purged
- Pro README, logo, animated demo, MCP setup guide, [reliability audit](docs/architecture/reliability-audit.md) + [roadmap](docs/architecture/roadmap.md)

### V5.0.0
- `comments.py` → `comments/` package (5 focused modules)
- Redis connection pooling, secret scanning on all branches
- LLM circuit breakers with automatic failover
- MCP server for IDE integrations · per-repo YAML config

---

## Contributing

Pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the
development setup, test commands, and coding conventions.

Before opening a PR: `python -m pytest -q` and `ruff check app/` must pass.
CI runs Python 3.10, 3.11 and 3.12.

---

## License

Dual-licensed under **either** of:

- **MIT** — [LICENSE-MIT](LICENSE-MIT)
- **Apache-2.0** — [LICENSE-APACHE](LICENSE-APACHE)

at your option. SPDX: `MIT OR Apache-2.0`

You only need to satisfy one of them, whichever your organisation prefers. MIT is
short and widely pre-approved; Apache-2.0 adds an explicit patent grant that some
corporate legal teams require before approving a dependency. Offering both means
neither requirement blocks adoption.

Contributions are accepted under the same dual licence — see [LICENSE](LICENSE).

---

## Support

Free and open source. If you'd like to support development, sponsorship is
available via [GitHub Sponsors](https://github.com/sponsors/Shweta-Mishra-ai) —
entirely optional.

---

<div align="center">

Built by [Shweta Mishra](https://github.com/Shweta-Mishra-ai) · Licensed under MIT OR Apache-2.0

⭐ Star this repo if Autopilot saved you time!

</div>
