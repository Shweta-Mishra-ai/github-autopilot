# Slash Commands Reference

> Complete reference for all 26 slash commands.
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
- The command must appear anywhere in the comment body (beginning, middle, or end)
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

**Default maintainer-only commands:** `/merge`, `/rollback`, `/release`

**Configurable in `.ai-repo-manager.yml`:**
```yaml
commands:
  permissions:
    maintainer_only:
      - merge
      - rollback
      - release
      - autofix     # add to restrict /autofix to maintainers too
      - secfull
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
## ⏱️ Rate Limit

@username you've used **10 commands** in the last hour on this repo.
Please wait before trying again.

*Limit resets hourly to prevent API abuse.*
```

The limit resets at the start of the next UTC hour bucket. It is independent per repository — hitting the limit on `repo-A` does not affect `repo-B`.

**Per-repo daily AI limit:** Configurable via `REPO_DAILY_AI_LIMIT` env var (default: 150 AI calls/day). Shared across all commands on the repo.

---

## 4. Code Quality Commands

### `/fix`

**Permission:** Everyone  
**Works on:** Issues and PRs  
**Input used:** Issue/PR title, body, and any code blocks in the comment

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

**Permission:** Write/maintain/admin (configurable)  
**Works on:** Issues and PRs  
**Input used:** Issue title, body, recent commits for file identification

**What it does:** The full automated fix pipeline — identifies the buggy file, generates a fix plan, applies the fix, creates a branch, commits the corrected file, and opens a pull request. The PR closes the original issue automatically.

**Syntax:**
```
/autofix
/autofix app/handlers/comments.py    ← specify file explicitly
/autofix src/utils/validator.ts      ← works for any allowed extension
```

**Output:**
```markdown
## 🤖 Autofix Complete

**File fixed:** `app/handlers/push.py`
**Branch:** `autopilot/fix/42/1716883200`
**Root cause:** Missing deduplication check in `_scan_secrets()`
**Fix applied:** Added `_already_reported()` check before issue creation

[View Pull Request #89](https://github.com/org/repo/pull/89)

> ⚠️ AI-generated fix. Review the diff carefully before merging.
> Run your test suite on this branch to verify correctness.
```

**What autofix will NOT touch:**
- `server.py` — Flask entry point
- `app/github/auth.py` — authentication code
- `.env` — environment files
- Files without recognised extensions (`.py`, `.js`, `.ts`, `.go`, etc.)

**Common errors:**
- "Could not identify a file" → no file mentioned in issue, no recent commits. Use `/autofix path/to/file.py`
- "File too large" → file exceeds 32,000 chars (16,000 × 2 safety margin). Apply manually.
- "LLM truncated" → 70% safety guard triggered. LLM returned less than 70% of original file length. Applied fix would have deleted code. Use `/fix` to see the suggested change and apply manually.

---

### `/apply`

**Permission:** Everyone  
**Works on:** Issues and PRs  
**Input used:** Recent commit history on the branch

**What it does:** Analyses recent commits and rewrites any that do not follow Conventional Commits format. Creates a new branch with the rewritten commits and opens a PR.

**Conventional Commits format:**
```
feat: add user authentication
fix(auth): correct token expiry handling
docs: update API reference
refactor: extract payment processor
test: add unit tests for validator
chore: bump dependencies
perf: cache database queries
ci: add lint step to workflow
```

**Output:**
```markdown
## 🔧 Auto-Apply Results

### ✅ Fixed (3 commits)

✅ `a1b2c3d` → `feat: add user authentication`
   *(was: `added login stuff`)*

✅ `b2c3d4e` → `fix(api): correct 404 handling for missing users`
   *(was: `fix bug`)*

✅ `c3d4e5f` → `chore: bump requests from 2.27 to 2.28`
   *(was: `update deps`)*

✨ Fix branch `autopilot/fix-commits-1716883200` created — PR opened for review!
```

---

### `/improve`

**Permission:** Everyone  
**Works on:** Issues and PRs  
**Input used:** Code in the issue/PR body or PR file changes

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
**Input used:** Code in the issue/PR body or comment

**What it does:** Suggests structural refactors with before/after code. Focuses on reducing complexity, improving testability, and eliminating duplication.

**Output includes:** Refactor type, description, before/after code snippets, and the engineering benefit of each change.

---

### `/perf`

**Permission:** Everyone  
**Works on:** Issues and PRs  
**Input used:** Code in the issue/PR body or PR file changes

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
**Input used:** Issue/PR body, code blocks in comment

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

**What it does:** Reads the entire comment thread (up to 50 comments) and produces a concise summary of the discussion — key decisions made, open questions, and current status.

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
**Input used:** Code in issue/PR body or PR file changes

**What it does:** Identifies missing test coverage with risk-based priority. Shows which functions, branches, and edge cases are untested.

---

### `/ci`

**Permission:** Everyone  
**Works on:** Issues and PRs  
**Input used:** CI failure text in comment body (paste the error output)

**What it does:** Analyses CI failure logs and identifies root cause, fix steps, and prevention.

**Best usage:** Paste the relevant CI output into the comment alongside `/ci`:
```
The CI is failing with this error, can you help?

/ci

```
Error:
```
FAILED tests/test_auth.py::test_login - ImportError: cannot import name 'create_token' from 'app.auth'
```
```

---

## 6. Documentation and Release Commands

### `/docs`

**Permission:** Everyone  
**Works on:** Issues and PRs  
**Input used:** Code in comment body

**What it does:** Generates a complete docstring (Google style), usage example, and README section for the provided code.

---

### `/test`

**Permission:** Everyone  
**Works on:** Issues and PRs  
**Input used:** Code in comment body or PR changes

**What it does:** Generates a complete pytest test file with unit tests, edge cases, and mocks for the provided code.

**Output includes:** Test class structure, happy path tests, edge cases, error handling tests, and mock setup.

---

### `/changelog`

**Permission:** Everyone  
**Works on:** Issues and PRs  
**Input used:** Recent commit history (fetched from GitHub API)

**What it does:** Generates a `CHANGELOG.md` entry in [Keep a Changelog](https://keepachangelog.com) format from the last 20 commits.

**Output:**
```
## 📋 CHANGELOG Entry

```markdown
## [Unreleased]

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

**Permission:** Everyone (configurable)  
**Works on:** Issues and PRs

**What it does:** Triggers the repository's test workflow via GitHub Actions `workflow_dispatch`. Finds the first workflow named `test.yml`, `ci.yml`, `pytest.yml`, or similar.

**Requirement:** The repository must have a GitHub Actions workflow file that supports `workflow_dispatch`.

---

## 7. Security and Health Commands

### `/security`

**Permission:** Everyone  
**Works on:** PRs (uses diff); Issues (uses body)

**What it does:** Scans the PR diff for credential patterns using `enhanced_secrets.py` (35+ patterns) and scans `requirements.txt` changes for CVE-linked vulnerable dependencies.

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

**Why maintainer-only:** Exposes all open security vulnerabilities in the repository. This is sensitive information that should not be broadcast in public issue comments.

---

### `/health`

**Permission:** Everyone  
**Works on:** Issues and PRs

**What it does:** Grades the repository A–F based on: open issue count, open PR count, presence of license and description, CI configuration, security alerts, and documentation completeness.

**Output:**
```
## 🏥 Repo Health — org/repo

### Grade: **B** (78/100)
`████████░░`

### Findings
- ✅ 4 open issues (healthy)
- ✅ 2 open PRs (healthy)
- ✅ License: MIT
- 🟡 8 Dependabot alerts open
- 🔴 No CONTRIBUTING.md found
- ✅ CI workflow configured

### 💡 Recommendations
1. Review and close Dependabot alerts (`/secfull` for details)
2. Add CONTRIBUTING.md to guide new contributors
3. Consider adding issue templates for bug reports and features
```

---

## 8. Operations Commands

### `/merge`

**Permission:** Write/maintain/admin  
**Works on:** PRs only

**What it does:** Merges the PR after all guardrails pass. Guardrails checked:
- CI checks all passing (green)
- No blocking reviews (approved or no reviews required)
- PR not in draft state
- No merge conflicts

If any guardrail fails, the bot posts an explanation and does NOT merge.

**Output on success:**
```
## ✅ Merged!

**`feat/user-auth`** → **`main`**
SHA: `a1b2c3d4`
```

**Output on guardrail failure:**
```
## 🚫 Cannot Merge

**Reason:** 2 CI checks are still running or failed.
- ❌ pytest (failed)
- ⏳ lint (in progress)

Fix the failing checks and retry `/merge`.
```

---

### `/rollback`

**Permission:** Write/maintain/admin  
**Works on:** Issues and PRs

**What it does:** Lists available snapshots (no argument) or restores repository state to a snapshot (with snapshot number).

**Usage:**
```
/rollback              ← list available snapshots
/rollback 2            ← restore snapshot #2
```

**List output:**
```
## 📸 Available Snapshots — org/repo

| # | Created | Trigger | Actions recorded |
|---|---------|---------|-----------------|
| 1 | 2026-05-27 14:32 | pr_analysis | 3 label additions, 1 title change |
| 2 | 2026-05-27 09:15 | issue_triage | 12 label additions, 5 comments |
| 3 | 2026-05-26 22:08 | pre_rollback | 0 actions |

Use `/rollback N` to restore to snapshot #N.

> ⚠️ Rollback undoes bot actions (labels, title changes) only.
> It does NOT revert code commits.
```

---

### `/report`

**Permission:** Everyone  
**Works on:** Issues and PRs

**What it does:** Posts a weekly analytics summary — PR velocity, issue resolution time, command usage, code review quality scores, and LLM budget usage.

---

### `/budget`

**Permission:** Everyone  
**Works on:** Issues and PRs

**What it does:** Shows today's LLM API usage per provider with remaining capacity.

---

### `/notify`

**Permission:** Everyone  
**Works on:** Issues and PRs

**What it does:** Sends a Discord embed for the current issue or PR, colour-coded by severity (red for bugs/security, green for features, blue for general).

**Requirement:** `DISCORD_WEBHOOK_URL` must be set in the environment.

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

If `commands.enabled` is not present in the config file, all 26 commands are active by default.

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

| Error message | Cause | Fix |
|--------------|-------|-----|
| `ℹ️ Command Disabled` | Command not in `commands.enabled` | Add to enabled list in `.ai-repo-manager.yml` |
| `⛔ Permission Denied` | User lacks write/maintain/admin access | Use a maintainer account, or add user as collaborator |
| `⏱️ Rate Limit` | 10 commands used in current hour | Wait for the hour to reset |
| `🚫 Cannot Merge` | PR guardrail failed | Fix failing CI, resolve conflicts, get approval |
| `⚠️ Command Error` | Handler threw an unexpected exception | Check Render logs; retry the command |
| `🤖 AI temporarily unavailable` | All LLM providers circuit-broken | Wait 5–10 minutes; check `/health` for provider states |
| `ℹ️ /merge only works on Pull Requests` | Command used on an issue | Use on a PR instead |
| `ℹ️ /impact only works on Pull Requests` | Command used on an issue | Use on a PR instead |
| `⚠️ No files to fix` | Autofix couldn't identify target file | Specify file: `/autofix path/to/file.py` |
| `⚠️ File not found` | Specified file doesn't exist in repo | Check the file path spelling |
| `⚠️ Cannot autofix this file` | File in `BLOCKED_PATHS` | Apply fix manually |

