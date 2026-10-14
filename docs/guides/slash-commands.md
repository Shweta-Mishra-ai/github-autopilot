# Slash Commands Reference

> Complete reference for all 27 slash commands (the registry in `app/core/commands.py`).
> For each command: syntax, permissions, what it uses, what it posts, example output, and common errors.

---

## Table of Contents

1. [How Commands Work](#1-how-commands-work)
2. [Permission Levels](#2-permission-levels)
3. [Rate Limits](#3-rate-limits)
4. [Code Quality Commands](#4-code-quality-commands)
5. [Understanding Commands](#5-understanding-commands)
6. [Documentation and Release Commands](#6-documentation-and-release-commands)
7. [Security and Health Commands](#7-security-and-health-commands)
8. [Operations Commands](#8-operations-commands)
9. [Enabling and Disabling Commands](#9-enabling-and-disabling-commands)
10. [Error Reference](#10-error-reference)

---

## 1. How Commands Work

Post any command as a comment on a GitHub issue or pull request. The bot reads the comment, identifies the command, checks permissions, and responds within 30 seconds.

**Triggering rules:**
- The command must start a line of the comment (optionally after an `@mention` of the bot). A command mentioned mid-sentence — "see /release for details" — is not run
- Commands inside quoted lines (`>`), inline code, fenced or indented (4-space/tab) code blocks and HTML comments (`<!-- -->`) are ignored, so quoting or pasting text that mentions a command never runs it
- A command's arguments are the rest of its own line (`/rollback 2 confirm`, `/apply fix/bot-issue-7-…`). `/ci` is the exception: everything after it, including a log pasted on the lines below, is its input
- Editing a comment runs a command only if the edit added it — fixing a typo elsewhere in an old `/release` comment does not release again
- Commands are case-insensitive: `/Fix` works the same as `/fix`
- Only the first recognised command in a comment is processed
- The bot processes `issue_comment` events — comments on issues AND pull requests both work

**Example:**
```
Hey bot, can you check this PR for issues?

/security

Thanks
```
The bot sees `/security` in the comment body and runs the security scan.

---

## 2. Permission Levels

Commands are divided into two groups:

### Available to everyone (read access or higher)
All users who can view and comment on the issue or PR can use these commands.

### Maintainer-only (write/maintain/admin access required)
These commands take destructive or production-affecting actions. GitHub collaborator permission API is called before execution. Denied with an explanation comment if the user lacks access.

**Always maintainer-only** (cannot be relaxed by configuration): `/merge`, `/rollback`, `/release`, `/autofix`, `/apply`, `/secfull`, `/ignore`, `/runtests`, `/notify`.

**More can be added in `.ai-repo-manager.yml`** — `maintainer_only` only ever adds to that list:
```yaml
commands:
  permissions:
    maintainer_only:
      - changelog   # e.g. also restrict /changelog
```

**Permission levels that qualify as "maintainer":**

| GitHub level | Qualifies? |
|-------------|-----------|
| `admin` | ✅ Yes |
| `maintain` | ✅ Yes |
| `write` | ✅ Yes |
| `read` | ❌ No |
| `none` (not a collaborator) | ❌ No |

---

## 3. Rate Limits

**Per-user limit:** 10 commands per user per hour per repository.

When the limit is hit, the bot posts:
```
## ⏳ Rate Limit Reached

@username — you've used the command limit (10/hour) for this repo.

Please try again in an hour.
```

The limit is counted before the enable/permission checks, so a command that is then refused still uses one of your ten.

The limit resets at the start of the next UTC hour bucket. It is independent per repository — hitting the limit on `repo-A` does not affect `repo-B`.

**Per-repo daily AI limit:** Configurable via `REPO_DAILY_AI_LIMIT` env var (default: 150 AI calls/day). It counts every AI call made for the repository — automatic PR, issue and push processing as well as commands — and resets at midnight UTC. Once it is spent, AI commands answer `## ⏳ AI Budget Reached`. If Redis is unavailable the limit cannot be counted and calls are allowed.

---

## 4. Code Quality Commands

### `/fix`

**Permission:** Everyone  
**Works on:** Issues and PRs  
**Input used:** Issue/PR title and body; on a PR, also the changed files and the start of the diff. Text written after the command in your comment is not used.

**What it does:** Analyses the issue to identify the root cause, suggests a production-ready fix, and generates a test to verify the fix. Does NOT make any code changes — output is a comment only.

**Output:**
```markdown
## 🔧 Fix

**Root cause:** The `process_payment()` function does not handle the case
where `stripe_token` is `None`, raising `AttributeError: 'NoneType' object
has no attribute 'id'` on line 47.

**Fix:**
```
def process_payment(user_id: int, stripe_token: str | None) -> bool:
    if stripe_token is None:
        log.warning(f"No Stripe token for user {user_id}")
        return False
    # ... existing code
```

**Why:** The frontend can submit the payment form before Stripe.js
has finished loading, resulting in a None token reaching the backend.

**Test:**
```
def test_process_payment_none_token():
    result = process_payment(user_id=1, stripe_token=None)
    assert result is False
```

> ⚠️ AI confidence: 82% · verify before applying
```

**Common errors:**
- "Fix didn't change anything" → LLM returned prose. Try rephrasing the issue title to be more specific, or wait a few minutes and retry.
- Response is very short → hallucination guard triggered. LLM had low confidence. The bot retried all providers — this is the best available response.

---

### `/autofix`

**Permission:** Write/maintain/admin (always)  
**Works on:** Issues and PRs  
**Input used:** Issue title and body; an optional file path argument

**What it does:** Plans a fix, reads the target file, asks for exact find/replace edits, applies them, and commits the result to a new branch `fix/bot-issue-<N>-<timestamp>`. It does **not** open a pull request: it posts the diff and you decide.

**Syntax:**
```
/autofix
/autofix app/handlers/comments.py    ← specify the file explicitly
```

**Output:**
```markdown
## 🤖 Autofix Ready — Review Required

**File:** `app/handlers/push.py` | **Branch:** `fix/bot-issue-42-1716883200` | **Confidence:** 80%

**Problem:** …
**Fix:** …

### Diff Preview
…

**To create a PR:** reply `/apply fix/bot-issue-42-1716883200`
**To discard:** delete the branch — nothing is merged unless you `/apply` it
```

**What autofix will edit:** `.py`, `.md`, `.txt`, `.json`, `.toml`, and a few named YAML files (`mkdocs.yml`, `.pre-commit-config.yaml`, `codecov.yml`, `readthedocs.yml`, `.markdownlint.yml`). Never: `.env*`, `.github/workflows/` and `.github/actions/`, `secrets/`, `certs/`, `keys/`, dependency and build files (`requirements*.txt`, `setup.py`, `setup.cfg`, `pyproject.toml`, `Dockerfile`, `docker-compose.yml`), and this bot's own security modules.

**Size limit:** the whole file must fit in the model's prompt — about 6,000 characters. Larger files are skipped with an explanation; use `/fix` for a suggestion instead.

**Errors** (each heading names the reason): `⚠️ Autofix Skipped`, `⚠️ Autofix Failed`, `⚠️ Autofix Blocked` (a path outside what it may edit), `⚠️ Branch Error`, `⚠️ Commit Failed`.

---

### `/apply`

**Permission:** Write/maintain/admin (always)  
**Works on:** Issues and PRs  
**Input used:** The branch name argument

**What it does:** Opens a pull request from an autofix branch into the default branch, with `Closes #N` for the issue it came from.

**Usage:**
```
/apply                               ← list the fix/bot-issue-* branches
/apply fix/bot-issue-42-1716883200   ← open a PR from that branch
```

If a PR from that branch is already open, it links to it instead of opening another.

---

### `/improve`

**Permission:** Everyone  
**Works on:** Issues and PRs  
**Input used:** Issue/PR title and body; on a PR, the changed files and the start of the diff

**What it does:** Analyses code for improvements across four dimensions with scored severity.

**Output:**
```markdown
## ✨ Improvements

**Overall:** Solid implementation with room for performance and readability improvement.

### 1. `PERFORMANCE` — Cache repeated database lookups
The `get_user()` call on line 23 executes on every request. With high traffic this
creates significant database load.
```
from functools import lru_cache

@lru_cache(maxsize=1000)
def get_user(user_id: int) -> User:
    return db.query(User).filter_by(id=user_id).first()
```

### 2. `READABILITY` — Extract magic numbers to named constants
```
MAX_RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2.0
```

### 3. `SECURITY` — Validate file extension before processing upload
```
ALLOWED_EXTENSIONS = {'.jpg', '.png', '.gif', '.pdf'}
if not any(filename.endswith(ext) for ext in ALLOWED_EXTENSIONS):
    raise ValueError(f"File type not allowed: {filename}")
```
```

---

### `/refactor`

**Permission:** Everyone  
**Works on:** Issues and PRs  
**Input used:** Issue/PR title and body; on a PR, the changed files and the start of the diff

**What it does:** Suggests structural refactors with before/after code. Focuses on reducing complexity, improving testability, and eliminating duplication.

**Output includes:** Refactor type, description, before/after code snippets, and the engineering benefit of each change.

---

### `/perf`

**Permission:** Everyone  
**Works on:** Issues and PRs  
**Input used:** Issue/PR title and body; on a PR, the changed files and the start of the diff

**What it does:** Analyses time and space complexity, detects N+1 query patterns, identifies blocking I/O in async contexts, and suggests caching opportunities.

**Output:**
```
## ⚡ Performance Analysis

Rating: 🟠 Slow

Summary: The main bottleneck is a nested loop creating O(n²) complexity
in the match_users() function. With 1,000 users this executes 1,000,000 iterations.

1. match_users() — O(n²) → O(n) with a dict lookup

Problem: Nested loop comparing every user pair.

Fix:
    # O(n) with set lookup
    user_emails = {u.email for u in users_b}
    matches = [u for u in users_a if u.email in user_emails]

Improvement: ~1000x faster at 1,000 users, scales linearly.

Quick Wins:
- Use set for membership checks instead of list
- Cache db.query(Config).first() — called 47 times per request
- Move load_translations() outside the request handler
```

---

## 5. Understanding Commands

### `/explain`

**Permission:** Everyone  
**Works on:** Issues and PRs  
**Input used:** Issue/PR title and body; on a PR, the changed files and the start of the diff

**What it does:** Explains code or a concept in plain English across five sections.

**Output structure:**
```markdown
## 💡 Explanation

**What it is:** A circuit breaker is a state machine that stops calling
a failing service temporarily to allow it to recover.

**How it works:** The breaker has three states: CLOSED (normal operation),
OPEN (all calls rejected), and HALF_OPEN (testing recovery). After 3 failures
the circuit opens. After 60 seconds it allows one test call. If that succeeds,
it closes again.

**Why it exists:** Without a circuit breaker, a slow or failing LLM provider
would cause every handler to wait 45 seconds (the timeout) before moving to
the next provider. With the breaker, failures are detected after 3 attempts
and subsequent calls fail instantly, allowing fast fallback.

**Example:**
```
breaker = CircuitBreaker("groq_70b", fail_threshold=3, recovery_timeout=60)
if breaker.is_available():
    response = call_groq()
    breaker.record_success()
```

**Pitfalls:** Setting `fail_threshold` too low (e.g., 1) causes the circuit to
open on any transient error, causing unnecessary fallbacks. Setting
`recovery_timeout` too short prevents the provider from actually recovering.
```

---

### `/summarize`

**Permission:** Everyone  
**Works on:** Issues and PRs  
**Input used:** All existing comments on the issue/PR (fetched from GitHub API)

**What it does:** Reads the comment thread (up to 50 comments, each given an equal share of the prompt so the end of the thread is included) and writes a 3–5 sentence summary: what the issue is about, key points raised, current status or resolution, and action items.

---

### `/arch`

**Permission:** Everyone  
**Works on:** PRs (uses file list) or Issues (uses title/body)  
**Input used:** Changed filenames on PR, issue body for issues

**What it does:** Reviews architectural quality — layer boundary violations, circular dependencies, god classes, tight coupling, and naming inconsistencies.

**Output:**
```markdown
## 🏗️ Architecture Review

**Health:** 🟠 Needs work
**Refactoring Priority:** 🟡 Planned

**Summary:** The PR introduces a dependency from `app/core/config.py` into
`app/handlers/pull_request.py` that violates the layer boundary. Core modules
should have no knowledge of handler implementations.

### Issues Found

- 🔴 **Layer Violation** — `app/core/config.py`
  `config.py` imports from `app/handlers/pull_request.py` to get `_blast_radius()`.
  This creates a circular dependency risk and violates the core → handler one-way dependency rule.
  → Move `_blast_radius()` to `app/core/blast_radius.py` and import from there.

- 🟡 **God Function** — `app/handlers/comments.py::handle()`
  `handle()` is 580 lines with 26 elif branches. Hard to test individual commands.
  → The slash command dispatcher pattern is intentional but could use a command registry.

### ✅ Good Patterns
- ✅ No handler module imports from another handler
- ✅ All GitHub API calls go through `app/github/client.py`
- ✅ Redis access isolated to `app/core/redis_client.py`
```

---

### `/impact`

**Permission:** Everyone  
**Works on:** PRs only  
**Input used:** Changed files list from GitHub API

**What it does:** Maps changed files to system layers (handlers, core, AI, security, tests, config/deploy) and analyses the blast radius and risk level of the PR.

**Output includes:** Layer map, breaking change risk (low/medium/high), migration requirement, review priority, and affected systems.

---

### `/gaps`

**Permission:** Everyone  
**Works on:** Issues and PRs  
**Input used:** Issue/PR title and body; on a PR, the changed files and the start of the diff

**What it does:** Identifies missing test coverage with risk-based priority. Shows which functions, branches, and edge cases are untested.

---

### `/ci`

**Permission:** Everyone  
**Works on:** Issues and PRs  
**Input used:** Everything after `/ci` in your comment — paste the log on the lines below it. With nothing pasted, the metadata of the repository's latest failed Actions run (workflow, branch, commit) — no log output, and the reply says so.

**What it does:** Identifies the root cause, fix steps, and prevention. A long log is cut to its last 3,000 characters, where the failure is.

**Usage:**
````
/ci
```
FAILED tests/test_auth.py::test_login - ImportError: cannot import name 'create_token' from 'app.auth'
```
````

---

## 6. Documentation and Release Commands

### `/docs`

**Permission:** Everyone  
**Works on:** Issues and PRs  
**Input used:** Issue/PR title and body; on a PR, the changed files and the start of the diff

**What it does:** Generates a complete docstring (Google style), usage example, and README section for the provided code.

---

### `/test`

**Permission:** Everyone  
**Works on:** Issues and PRs  
**Input used:** Issue/PR title and body; on a PR, the changed files and the start of the diff

**What it does:** Generates a complete pytest test file with unit tests, edge cases, and mocks for the provided code.

**Output includes:** Test class structure, happy path tests, edge cases, error handling tests, and mock setup.

---

### `/changelog`

**Permission:** Everyone  
**Works on:** Issues and PRs  
**Input used:** Commits on the default branch since the latest tag (up to 20 fetched, 15 used)

**What it does:** Generates a `CHANGELOG.md` entry in [Keep a Changelog](https://keepachangelog.com) format, headed `## [X.Y.Z] - YYYY-MM-DD` with today's date. With nothing since the tag it says so instead.

**Output:**
```
## 📋 CHANGELOG Entry

```markdown
## [1.4.0] - 2026-05-28

### Added
- `/runtests` command: trigger GitHub Actions workflow_dispatch
- Permission cache: 5-minute RLock cache for collaborator API results
- Bounded thread pool: `ThreadPoolExecutor` with configurable worker count

### Changed
- `enhanced_secrets.py` replaces `secrets.py` — 35+ patterns, entropy gating
- `_MAX_FILE_CHARS` raised from 4,000 to 16,000 in autofix engine

### Fixed
- HMAC signature verification now fails closed on empty `WEBHOOK_SECRET`
- Autofix: LLM JSON parse failures now logged instead of silently returning
- Branch creation: `KeyError` on unexpected GitHub ref structure now caught
```
```

---

### `/release`

**Permission:** Write/maintain/admin  
**Works on:** Issues and PRs  
**Input used:** Recent commit history and existing tags

**What it does:** Creates a GitHub **draft release** with AI-generated release notes. The release is draft — you must review and publish it manually.

**Output:**
```
## 🚀 Draft Release Created

**Version:** *(AI-generated based on commit history)*
**Status:** Draft (review before publishing)

### Highlights
- Full security hardening — webhook fail-closed, HMAC fix, auth enforcement
- 35+ secret patterns with entropy gating and false-positive suppression
- Bounded thread pool — 6 workers, 50-job queue cap

*(Link to the draft release will appear in the response)*

> ✏️ Review the release notes and adjust before publishing.
> AI-generated descriptions may need human refinement.
```

---

### `/version`

**Permission:** Everyone  
**Works on:** Issues and PRs

**What it does:** Shows the current tag history, latest release, and recent commits.

---

### `/runtests`

**Permission:** Write/maintain/admin (always)  
**Works on:** Issues and PRs

**What it does:** Dispatches the repository's test workflow via GitHub Actions `workflow_dispatch`. It picks the first workflow whose name or file name contains one of `test`, `tests`, `ci`, `pytest`, `check`, `checks`, `lint`, `unit`, and never one mentioning `release`, `deploy`, `publish`, `pages`, `prod` or `production`. On a pull request from the same repository it runs on the PR's head branch; otherwise on the default branch (a fork PR's branch cannot be dispatched, and the reply says so).

**Requirement:** The workflow must declare `on: workflow_dispatch`.

---

## 7. Security and Health Commands

### `/security`

**Permission:** Everyone  
**Works on:** PRs only (on an issue it replies that it works on pull requests)

**What it does:** Scans the PR's added lines for credential patterns (`enhanced_secrets.py`) and, when the PR changes the **root** `requirements.txt`, checks that file as of the PR head for CVE-linked versions. If the PR does not change it, or it cannot be read, the reply says dependencies were **not checked** rather than reporting them clean.

**Output:**
```
## 🔒 Security Scan Results

✅ **No secrets detected** in changed files.

### Dependency Vulnerabilities

| Package | Current | Safe version | Severity | CVE |
|---------|---------|-------------|----------|-----|
| `Pillow` | 9.0.0 | 9.3.0 | 🔴 HIGH | CVE-2022-45199 |
| `requests` | 2.20.0 | 2.28.1 | 🟡 MEDIUM | CVE-2022-42969 |

**Fix:**
```
pip install Pillow>=9.3.0 requests>=2.28.1
```
```

---

### `/secfull`

**Permission:** Write/maintain/admin  
**Works on:** Issues and PRs

**What it does:** Full security audit using GitHub's Security APIs — Dependabot alerts, CodeQL findings, and Secret Scanning alerts. Summarises all open security issues in one report.

**Licence compliance:** if the repository has a `requirements.txt`, the report also carries a licence section flagging copyleft and unidentified dependency licences, resolved against PyPI. It is bounded to 20 packages and a 20-second budget; a package the scan did not reach is **not reported as unknown**, and the section says how many of how many packages were checked, so a partial scan is never presented as "all permissive". Repositories without a `requirements.txt` get the security report with no licence section — not an error.

**Why maintainer-only:** Exposes all open security vulnerabilities in the repository. This is sensitive information that should not be broadcast in public issue comments.

---

### `/health`

**Permission:** Everyone  
**Works on:** Issues and PRs

**What it does:** Grades the repository A+ to F from four things it reads: the number of open issues, the number of open pull requests, whether it has a licence, and whether it has a description. It does not look at CI, security alerts or documentation — use `/secfull` for security.

**Output:**
```
## 🏥 Repo Health — `org/repo`

### Grade: **B** (75/100)
`███████░░░`

### Findings
- 🔴 25 open issues
- 🟡 7 open PRs
- ✅ License: MIT License
- 🟡 No description

### 💡 Recommendations
1. Triage and close old issues
```

Scoring: start at 100; more than 20 open issues −15 (more than 10: −7); more than 10 open PRs −10 (more than 5: −5); no licence −8; no description −5. A+ ≥ 90, A ≥ 80, B ≥ 70, C ≥ 60, D ≥ 50, else F.

---

## 8. Operations Commands

### `/merge`

**Permission:** Write/maintain/admin  
**Works on:** PRs only

**What it does:** Merges the PR after all guardrails pass. It refuses entirely unless `auto_merge.enabled: true` is set in `.ai-repo-manager.yml` (off by default). Guardrails checked:
- The PR is open, not already merged, and not a draft, with at least one commit
- GitHub has computed mergeability and there are no merge conflicts
- No reviewer's latest review is "changes requested" (every page of reviews is read; an approval is not required)
- No check run failed or is still running, and no commit status is failing or pending
- At least one check or status has reported — a commit with none is refused, since that is also how it looks before CI starts. Repositories with no CI set `auto_merge.require_passing_checks: false`
- The base branch is not `main`, `master`, `production` or `release`, unless `auto_merge.allow_protected_branches: true`
- The PR's recorded risk level is in `auto_merge.allowed_risk_levels` (default `[low]`); with no risk analysis on record it is refused

It merges exactly the commit that was checked. If any guardrail fails, the bot posts the reason and does NOT merge.

**Output on success:**
```
## ✅ Merged!

**`feat/user-auth`** → **`main`**
SHA: `a1b2c3d4`
```

**Output on guardrail failure:**
```
## 🚫 Cannot Merge

**Reason:** Checks still running: lint
```

---

### `/rollback`

**Permission:** Write/maintain/admin (always)  
**Works on:** Issues and PRs

**What it does:** Undoes the bot's own recorded actions. The bot records the issues it opens (secret, dependency and commit-lint alerts), the labels it adds during triage (not ones the author set), and the PR titles and descriptions it rewrites — grouped into one `bot_activity` snapshot per hour. Rolling back a snapshot closes those issues, removes those labels and restores those titles and descriptions, newest first. It does **not** revert code commits or delete comments.

**Usage:**
```
/rollback              ← list snapshots (#1 is the newest; up to 10, kept 7 days)
/rollback 2            ← preview what snapshot #2 would undo
/rollback 2 confirm    ← undo it
```

Before undoing anything it takes a `pre_rollback_by_<user>` snapshot; if that fails, nothing is undone.

**List output:**
```
## 📸 Repo Snapshots — org/repo

| # | Trigger | Taken At | State | Commit | Bot Actions |
|---|---------|----------|-------|--------|-------------|
| **#1** | `bot_activity` | 2026-05-27 14:00 UTC | — | `—` | 3 actions |
| **#2** | `pre_rollback_by_alice` | 2026-05-27 09:15 UTC | 4 issues, 2 PRs | `a1b2c3d` | 0 actions |
```

---

### `/report`

**Permission:** Everyone  
**Works on:** Issues and PRs

**What it does:** Posts this week's activity: PRs merged today and average time to merge, issues closed today and average time to close, how often each command was run, and total actions. With nothing recorded yet it says so; it gives no grade or score.

---

### `/budget`

**Permission:** Everyone  
**Works on:** Issues and PRs

**What it does:** Shows today's LLM API usage per provider with remaining capacity.

---

### `/notify`

**Permission:** Write/maintain/admin (always)  
**Works on:** Issues and PRs

**What it does:** Posts the current issue or PR to Discord and/or Slack, whichever webhooks are configured, colour-coded red for bug/security/critical labels, green for feature/enhancement, otherwise blurple. Text after `/notify` on the same line is added as a note. The reply lists which channels it was delivered to and which failed. A repository that sets `notifications.discord: false` or `notifications.slack: false` is not posted to that channel.

**Requirement:** `DISCORD_WEBHOOK_URL` or `SLACK_WEBHOOK_URL` must be set in the environment.

---

### `/ignore`

**Permission:** Write/maintain/admin (always)  
**Works on:** Issues and PRs

**What it does:** Saves a preference to the repository's memory, which later AI reviews and commands are given as context — for example `/ignore we use print() for CLI output on purpose`. With no text it shows usage. It replies whether the preference was stored, was already stored, or could not be saved.

---

## 9. Enabling and Disabling Commands

In `.ai-repo-manager.yml`:

```
commands:
  enabled:
    - fix           # ← only commands in this list are active
    - explain
    - health
    - security
    # /autofix, /merge, etc. are NOT listed → will show "Command Disabled"
```
```

If `commands.enabled` is not present in the config file, all 27 commands are active by default. `enabled: []` turns every command off, and `bot.enabled: false` turns the whole bot off, commands included.

To disable a single command without listing all others:

> **Note:** This is not supported. You must list all enabled commands explicitly.
> If you want all commands except `/merge`, list all commands except `/merge`.


To add a command back after it showed "Command Disabled":
1. Open `.ai-repo-manager.yml` in your default branch
2. Add the command name to the `commands.enabled` list
3. Commit to the default branch
4. The bot picks up the change within 5 minutes (config cache TTL)

---

## 10. Error Reference

| Heading the bot posts | Cause | Fix |
|----------------------|-------|-----|
| `🚫 Command Disabled` | Command not in `commands.enabled`, or the bot is disabled | Add it to the enabled list in `.ai-repo-manager.yml` |
| `🚫 /cmd Not Run` | You lack write/maintain/admin access, or the permission check itself failed (the text says which) | Use a maintainer account, or check the App's installation permissions |
| `⏳ Rate Limit Reached` | 10 commands from you on this repo in the current hour | Wait for the hour to reset |
| `⏳ AI Budget Reached` | The repository's daily AI limit is spent | Wait for midnight UTC, or raise `REPO_DAILY_AI_LIMIT` |
| `🚫 Cannot Merge` | A merge guardrail failed | The reason line names it |
| `⚠️ AI Temporarily Unavailable` | Every AI provider failed or is circuit-broken | Wait a few minutes; `/health` and `/budget` show provider state |
| `⚠️ /cmd Produced No Output` | The command ran but had nothing to post | Retry; report it if it repeats |
| `⚠️ <Command> failed` | The command raised an error; the message is shown | Check the server logs; retry |
| `ℹ️ /merge only works on Pull Requests` | Command used on an issue | Use on a PR instead |
| `ℹ️ /impact only works on Pull Requests` | Command used on an issue | Use on a PR instead |
| `⚠️ Autofix Skipped` / `Failed` / `Blocked` | Autofix could not find, read, edit or was not allowed to edit the file | The text names the reason; specify the file with `/autofix path/to/file.py` or use `/fix` |
