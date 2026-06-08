# Autofix Engine

> The most complex feature in GitHub Autopilot.
> `/autofix` takes a GitHub issue as input and produces a working pull request as output — without human intervention in the fix generation step.
> This document explains every stage of the engine, every bug that was fixed, every failure mode, and why the system is designed this way.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Why Automated Code Fixing Is Hard](#2-why-automated-code-fixing-is-hard)
3. [The Five-Stage Pipeline](#3-the-five-stage-pipeline)
4. [Stage 1 — Issue Analysis and File Candidate Selection](#4-stage-1--issue-analysis-and-file-candidate-selection)
5. [Stage 2 — File Fetching and Safe Excerpt](#5-stage-2--file-fetching-and-safe-excerpt)
6. [Stage 3 — Fix Plan Generation](#6-stage-3--fix-plan-generation)
7. [Stage 4 — Fix Application and Safety Guards](#7-stage-4--fix-application-and-safety-guards)
8. [Stage 5 — Branch Creation, Commit, and Pull Request](#8-stage-5--branch-creation-commit-and-pull-request)
9. [Key Reliability Fixes](#9-key-reliability-fixes)
10. [Failure Scenarios and Responses](#10-failure-scenarios-and-responses)
11. [Current Limitations](#11-current-limitations)
12. [Roadmap to Sandboxed Execution](#12-roadmap-to-sandboxed-execution)

---

## 1. Overview

When a user comments `/autofix` on a GitHub issue, the engine:

1. Analyses the issue to identify which file is most likely buggy
2. Fetches the file content from GitHub
3. Asks an LLM to generate a fix plan (root cause + description of change)
4. Asks the LLM to apply the fix (return the complete corrected file)
5. Validates the response (not truncated, not prose, not empty)
6. Creates a new branch named `autopilot/fix/{issue_number}/{timestamp}`
7. Commits the fixed file to that branch
8. Opens a pull request that closes the original issue
9. Posts a summary comment on the issue

The result is a real, reviewable pull request that a human can inspect, test, and merge.

**What autofix does NOT do:**
- Merge without human review
- Fix bugs that require changes across multiple files
- Run tests to verify the fix
- Guarantee the fix is correct — it is AI-generated and requires review

---

## 2. Why Automated Code Fixing Is Hard

Three fundamental problems make this harder than it appears:

### Problem 1 — LLM Context Window Limits

LLMs have finite context. A fix requires the model to see the full file, understand the bug, and return the complete corrected version. If you send only the first 4,000 characters of a file (the original limit in this codebase), the model sees a partial file and returns a "fixed" version based on what it saw — which, when committed, silently overwrites everything after the 4,000-character mark with nothing.

This was a real bug: files over ~100 lines were being silently truncated on every `/autofix` call.

**Fix:** Limit raised to 16,000 characters with an explicit truncation marker. Safety guard added: if the response is less than 70% of the original file length, it is rejected.

### Problem 2 — JSON Reliability

The router always asks for JSON output. LLMs occasionally return prose instead. The original code called `r.get("fixed_content", "")` on the result — when the result was `{"raw": "Here's how I'd fix this..."}` (the prose fallback), `fixed_content` was absent, `""` was returned, and the function fell back to returning the original file unchanged, with no log message.

The user saw "fix didn't change the file" with no explanation. The error was completely invisible.

**Fix:** Detect the `{"raw": ...}` path explicitly and log a warning at `WARNING` level so it appears in Render logs.

### Problem 3 — GitHub API Fragility

Branch creation reads the current HEAD SHA: `ref["object"]["sha"]`. If GitHub's API returns an unexpected response structure for any reason (network partial response, API version change, repository in an edge state), this line raises `KeyError`. The original `run_autofix` only caught `GitHubError` — an unhandled `KeyError` would propagate as an uncaught exception, the user would see no response, and the Render logs would show a stack trace.

**Fix:** Wrap the SHA lookup in `except (KeyError, TypeError)` and raise a descriptive `GitHubError` that is caught cleanly by the outer handler.

---

## 3. The Five-Stage Pipeline

```
/autofix comment received
         │
         ▼
┌─────────────────────────┐
│  Stage 1                │
│  Issue Analysis         │
│  File Candidate         │
│  Selection              │──── no candidates? ──► "No files to fix"
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│  Stage 2                │
│  File Fetching          │
│  Safe Excerpt           │──── 404? ──────────► "File not found"
│  (_MAX_FILE_CHARS=16k)  │──── too large? ─────► "File too large"
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│  Stage 3                │
│  Fix Plan Generation    │──── {"raw":...}? ──► Log warning, abort
│  (root_cause + desc)    │──── low confidence?► Retry next provider
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│  Stage 4                │
│  Fix Application        │──── {"raw":...}? ──► Log warning, return original
│  Full file returned     │──── len < 70%? ────► Reject, return original
│  Safety guard 70%       │──── empty? ────────► Return original
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│  Stage 5                │
│  Branch Creation        │──── KeyError? ─────► GitHubError (caught)
│  File Commit            │──── 409 conflict? ─► "File changed, retry"
│  PR Open                │──── 422 exists? ───► "Branch already exists"
│  Summary Comment        │
└───────────┬─────────────┘
            │
            ▼
    ✅ PR opened, comment posted
```

---

## 4. Stage 1 — Issue Analysis and File Candidate Selection

```python
def run_autofix(repo: str, issue_number: int, issue: dict,
                token: str, target_file: str = "") -> str:

    title = issue.get("title", "")
    body  = (issue.get("body") or "")[:1000]

    # If user specified a file in the command args, use it directly
    if target_file and _is_allowed(target_file):
        candidates = [target_file]
    else:
        candidates = _get_file_candidates(repo, token, title, body)

    if not candidates:
        return (
            "## ℹ️ No Files to Fix\n\n"
            "Could not identify a file to fix from the issue description.\n\n"
            "**Tip:** Use `/autofix path/to/file.py` to specify the file directly."
        )
```

**File candidate ranking:**
```python
ALLOWED_EXTENSIONS = {".py", ".js", ".ts", ".go", ".rs", ".java",
                      ".rb", ".md", ".yml", ".yaml", ".json", ".toml", ".txt"}

BLOCKED_PATHS = {
    "server.py",            # Core app entry point
    "app/github/auth.py",   # Authentication — never auto-modify
    ".env",                 # Environment file
    "requirements.txt",     # Dependencies need human review
}

def _get_file_candidates(repo: str, token: str,
                          title: str, body: str) -> list[str]:
    candidates = []

    # 1. Files mentioned by path in issue body
    path_mentions = re.findall(r'`([^`]+\.[a-z]+)`', body + " " + title)
    for path in path_mentions:
        if _is_allowed(path):
            candidates.append(path)

    if candidates:
        return candidates[:3]   # stop here if explicit mentions found

    # 2. Recently modified files from commit history
    try:
        commits = gh_get(f"/repos/{repo}/commits?per_page=5", token)
        for commit in commits:
            detail = gh_get(f"/repos/{repo}/commits/{commit['sha']}", token)
            for f in detail.get("files", []):
                path = f.get("filename", "")
                if _is_allowed(path) and path not in candidates:
                    candidates.append(path)
                if len(candidates) >= 5:
                    break
    except Exception:
        pass

    return candidates[:3]

def _is_allowed(path: str) -> bool:
    if path in BLOCKED_PATHS:
        return False
    ext = os.path.splitext(path)[1].lower()
    return ext in ALLOWED_EXTENSIONS
```

**Why `BLOCKED_PATHS`?** Certain files should never be auto-modified regardless of what the issue says. `server.py` is the Flask entry point — a broken autofix here takes the entire bot down. `app/github/auth.py` handles GitHub JWT signing — a bug here would silently invalidate all API calls. The auth file is also the most security-sensitive file in the codebase.

**Why at most 3 candidates?** The engine tries each candidate in order until one succeeds. With more than 3 candidates, a low-confidence issue description could cause the bot to attempt fixes on several unrelated files. Three is the right balance between coverage and noise.

---

## 5. Stage 2 — File Fetching and Safe Excerpt

```python
_MAX_FILE_CHARS = 16_000   # Raised from 4,000 in v4 — see §9

_TRUNCATION_MARKER = """

# ═══════════════════════════════════════════════════════════
# AUTOFIX TRUNCATION NOTICE
# This file exceeds {limit} characters. Only the first {limit}
# characters are shown above. Generate a fix for the shown
# portion ONLY. Do NOT generate content beyond this point.
# ═══════════════════════════════════════════════════════════
"""

def _safe_excerpt(content: str) -> tuple[str, bool]:
    if len(content) <= _MAX_FILE_CHARS:
        return content, False   # (full content, was_truncated=False)
    truncated = content[:_MAX_FILE_CHARS]
    marker    = _TRUNCATION_MARKER.format(limit=_MAX_FILE_CHARS)
    return truncated + marker, True

def _fetch_file(repo: str, path: str, token: str) -> tuple[str, str]:
    data    = gh_get(f"/repos/{repo}/contents/{path}", token)
    content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    sha     = data["sha"]   # required for the commit update
    return content, sha
```

**Why base64 decode?** GitHub's Contents API returns file contents as base64. This is true even for plain-text Python files. The `errors="replace"` flag handles any non-UTF-8 bytes (binary files accidentally matched by extension) without crashing.

**Why `_safe_excerpt` returns a `was_truncated` bool?** The fix application prompt in Stage 4 changes its instruction when the file is truncated — telling the LLM explicitly "return ONLY the shown portion, not the complete file." This prevents the LLM from trying to generate the missing tail of the file from memory, which would be hallucinated content.

---

## 6. Stage 3 — Fix Plan Generation

```python
def _generate_fix_plan(title: str, body: str,
                        file_path: str, content: str) -> dict:
    excerpt, was_truncated = _safe_excerpt(content)

    r, _meta = router.ask(
        system="Senior software engineer. Analyse this bug report and file. JSON only.",
        user=f"""Issue title: {title}
Issue description: {body[:500]}
File: {file_path}
{'(FILE IS TRUNCATED — only first portion shown)' if was_truncated else ''}

File contents:
{excerpt}

Return JSON:
{{
  "root_cause":       "the precise technical cause of the bug",
  "fix_description":  "what change is needed and why",
  "files": [
    {{
      "path":   "{file_path}",
      "change": "description of the change to this file"
    }}
  ],
  "confidence": 0.85
}}""",
        task="fix_command",
    )

    # Detect JSON parse failure — {"raw": text} returned when LLM gave prose
    if "raw" in r and "root_cause" not in r:
        log.warning(
            f"autofix._generate_fix_plan: LLM returned prose (not JSON) "
            f"for file={file_path}. Check router logs for raw response."
        )
        return {}

    return r
```

**Why a separate fix plan step before applying the fix?** Two reasons:

1. **Better quality.** Asking the LLM to first articulate the root cause and then apply the fix (chain-of-thought reasoning) produces better fixes than asking it to fix the code directly. The plan step forces the model to reason before acting.

2. **User-visible output.** The fix plan (root cause, fix description) is shown in the PR body and the issue comment. Users can understand what the bot did and why — not just a diff.

---

## 7. Stage 4 — Fix Application and Safety Guards

This is the most critical stage. The LLM receives the file and must return the corrected complete file.

```python
def _apply_fix(current: str, fix_plan: dict, title: str) -> str:
    excerpt, was_truncated = _safe_excerpt(current)

    truncation_note = (
        "\n\nIMPORTANT: This file was truncated. "
        "Return ONLY the shown portion with your fix applied. "
        "Do NOT generate content beyond what is shown."
        if was_truncated else ""
    )

    r, _meta = router.ask(
        system=(
            "Senior software engineer. Apply the fix to this file. "
            "Return the COMPLETE corrected file exactly. "
            "Do not summarise, do not explain, do not add comments. "
            "JSON only."
        ),
        user=f"""Fix to apply: {fix_plan.get("fix_description", title)}
{truncation_note}

FILE CONTENTS:
{excerpt}

Return JSON:
{{"fixed_content": "complete corrected file here"}}""",
        task="fix_command",
        max_tokens=4000,
    )

    # Guard 1 — JSON parse failure
    if "raw" in r and "fixed_content" not in r:
        log.warning(
            f"autofix._apply_fix: LLM returned prose (not JSON). "
            f"Returning original file unchanged."
        )
        return current

    fixed = r.get("fixed_content", "").strip()

    # Guard 2 — Empty response
    if not fixed:
        log.warning("autofix._apply_fix: fixed_content is empty. Returning original.")
        return current

    # Guard 3 — 70% length safety check
    if len(fixed) < len(current) * 0.70:
        log.warning(
            f"autofix._apply_fix: response too short "
            f"(fixed={len(fixed)} chars vs original={len(current)} chars, "
            f"ratio={len(fixed)/len(current):.0%}). "
            f"LLM likely truncated. Rejecting, returning original."
        )
        return current

    return fixed
```

### The 70% Safety Guard — Detailed Explanation

**Why 70%?**

A legitimate bug fix typically changes, adds, or removes a small portion of a file. A complete rewrite of a 500-line file into 10 lines is almost certainly not a fix — it is a truncated LLM response.

The 70% threshold was chosen by empirical reasoning:
- Most fixes change < 30% of file content by volume
- Genuine refactors that reduce file size significantly (e.g., removing dead code) might legitimately reduce a file by 40–50%
- A truncated LLM response typically produces 10–40% of the original file size

70% is conservative enough to catch truncations while permitting aggressive-but-legitimate refactors.

**False positive scenario:** A fix that removes large amounts of genuinely dead code could reduce a file by more than 30%, triggering this guard. In this case, the fix is rejected and the user sees a log warning. The user must apply the fix manually. This is the correct trade-off — data safety (not silently truncating production code) takes priority over automation convenience.

**Why not use token count instead of char count?** Token counting requires a tokeniser library tied to the specific model, adding ~50MB of memory. Character count is a reasonable proxy that requires no dependencies.

---

## 8. Stage 5 — Branch Creation, Commit, and Pull Request

```python
def _create_branch(repo: str, token: str,
                   branch: str, base_branch: str) -> None:
    try:
        ref_data = gh_get(
            f"/repos/{repo}/git/ref/heads/{base_branch}", token
        )
        base_sha = ref_data["object"]["sha"]   # KeyError possible here
    except (KeyError, TypeError) as e:
        raise GitHubError(
            f"Unexpected GitHub ref structure for branch '{base_branch}': {e}. "
            f"Expected ref_data['object']['sha'] to be a string."
        ) from e

    gh_post(f"/repos/{repo}/git/refs", token, {
        "ref": f"refs/heads/{branch}",
        "sha": base_sha,
    })

def _commit_file(repo: str, token: str, branch: str,
                  path: str, content: str, message: str,
                  current_sha: str) -> None:
    gh_put(f"/repos/{repo}/contents/{path}", token, {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "sha":     current_sha,   # required by GitHub API for updates
        "branch":  branch,
    })

def _open_pr(repo: str, token: str, branch: str,
              base_branch: str, title: str,
              body: str) -> dict:
    return gh_post(f"/repos/{repo}/pulls", token, {
        "title": title[:72],
        "head":  branch,
        "base":  base_branch,
        "body":  body,
        "draft": False,
    })
```

**Branch naming convention:** `autopilot/fix/{issue_number}/{unix_timestamp_seconds}`

The timestamp suffix ensures uniqueness if `/autofix` is run multiple times on the same issue. The `autopilot/fix/` prefix groups all bot-created branches together in the branch list, making them easy to identify and clean up.

**Why `sha` is required in the commit:** GitHub's Contents API requires the current blob SHA when updating an existing file. This prevents write conflicts — if someone else modified the file between the fetch and the commit, GitHub returns HTTP 409 (Conflict) rather than silently overwriting their changes.

**PR body template:**
```
## 🤖 Autofix — Issue #{issue_number}

**Root cause:** {root_cause}

**Fix:** {fix_description}

---
Closes #{issue_number}

> ⚠️ This fix was generated by AI. Please review carefully before merging.
> Run your test suite on this branch to verify correctness.
```

---

## 9. Key Reliability Fixes

### Bug 1 — Silent File Truncation (Critical)

**Original code:**
```python
excerpt = current[:4000]   # hard truncation, no marker, no warning
r = router.ask(..., user=f"Fix this file:\n{excerpt}\n\nReturn full corrected file")
```

**What happened:** For any file > ~100 lines, the LLM received a partial file. It returned a "complete" corrected version of what it saw — 4,000 characters of content. This was committed, truncating everything after the 4,000-character mark in the original file. Production code was silently deleted.

**Fix:**
- `_MAX_FILE_CHARS` raised from 4,000 to 16,000
- `_safe_excerpt()` adds an explicit `_TRUNCATION_MARKER` when truncating
- The prompt changes when truncated: "return ONLY the shown portion"
- 70% safety guard rejects responses shorter than 70% of original

**Commit:** `fix(autofix): raise file-size limit 4k→16k, add truncation marker and 70% safety guard`

---

### Bug 2 — Silent JSON Parse Failure (High)

**Original code:**
```python
r = router.ask(...)   # may return {"raw": "Here's how I'd fix this..."}
fixed = r.get("fixed_content", "")   # "" when parse failed
if not fixed:
    return current   # silently returned original, no log
```

**What happened:** When the LLM returned prose instead of JSON (common under load or with ambiguous prompts), `_extract_json` returned `{"raw": text}`. The `.get("fixed_content", "")` returned `""`. The function returned `current` (original file) unchanged. The user saw "the fix didn't change the file" with no explanation.

**Fix:**
```python
if "raw" in r and "fixed_content" not in r:
    log.warning(
        f"autofix._apply_fix: LLM returned prose, not JSON. "
        f"Returning original file unchanged."
    )
    return current
```

**Commit:** `fix(autofix): log LLM JSON parse failures instead of silently returning`

---

### Bug 3 — Unhandled KeyError in Branch Creation (High)

**Original code:**
```python
ref_data = gh_get(f"/repos/{repo}/git/ref/heads/{base}", token)
base_sha = ref_data["object"]["sha"]   # KeyError if structure unexpected
```

**What happened:** `run_autofix` only had `except GitHubError` in its outer handler. A `KeyError` from `ref_data["object"]["sha"]` propagated as an unhandled exception. The thread crashed, the user got no response, and Render logs showed a stack trace with no helpful context.

**Fix:**
```python
try:
    base_sha = ref_data["object"]["sha"]
except (KeyError, TypeError) as e:
    raise GitHubError(
        f"Unexpected GitHub ref structure for branch '{base}': {e}. "
        f"Expected ref_data['object']['sha']."
    ) from e
```

Now caught cleanly by the outer `except GitHubError` handler, which posts an informative error comment to the issue.

**Commit:** `fix(autofix): wrap _create_branch SHA lookup in KeyError guard`

---

## 10. Failure Scenarios and Responses

| Scenario | Detection | Bot response to user |
|----------|-----------|---------------------|
| No file identified | Empty `candidates` list | "Could not identify a file — use `/autofix path/to/file.py`" |
| File in `BLOCKED_PATHS` | `_is_allowed()` returns False | "Cannot autofix this file — it is protected" |
| File not found (404) | `gh_get` raises `GitHubError` 404 | "File `{path}` not found in repository" |
| File too large | `len(content) > _MAX_FILE_CHARS * 2` | "File is too large for autofix (> 32,000 chars)" |
| Fix plan returns prose | `"raw" in r, "root_cause" not in r` | No response — bot retries with different file or aborts silently |
| Fix returns prose | `"raw" in r, "fixed_content" not in r` | Log warning, return original file — posts "no changes made" |
| Fix too short (< 70%) | Length guard fails | Log warning, return original — posts "fix rejected, LLM truncated" |
| Branch already exists | GitHub 422 Unprocessable | "A fix branch for this issue already exists" |
| File changed since fetch | GitHub 409 Conflict | "File was modified since fetch — please retry `/autofix`" |
| All LLMs down | `AllProvidersDown` raised | "AI is temporarily unavailable — try again in a few minutes" |
| Branch creation fails | `GitHubError` (now caught) | "Could not create fix branch: {descriptive message}" |

---

## 11. Current Limitations

**No execution sandbox.** Fixes are committed directly without running tests. A syntax error in generated code is committed. Every autofix PR requires human review and test execution before merging. This is by design — the bot is a productivity tool, not a fully autonomous agent.

**Single-file only.** Bugs requiring changes across multiple files (e.g., a bug in `handlers/comments.py` caused by a missing method in `core/config.py`) cannot be fixed by autofix. The user must apply the second change manually.

**No semantic understanding of cross-file dependencies.** The LLM sees only the target file. It cannot understand that a function signature change in `file A` requires a corresponding update in `file B`.

**No diff — full file replacement.** The bot commits the entire corrected file. For large files with small changes, this produces a large diff that is harder to review. A diff-based approach (generate and apply a unified diff) would produce smaller, more readable PRs.

**LLM fixes optimise for syntactic correctness, not logic correctness.** The model is good at fixing syntax errors, obvious logical mistakes, and common patterns. It struggles with complex algorithmic bugs, race conditions, and issues requiring deep domain knowledge.

---

## 12. Roadmap to Sandboxed Execution

Current flow:
```
Issue → AI fix plan → AI apply fix → commit → PR (human review required)
```

Phase 2 (syntax validation):
```
Issue → AI fix → syntax check (ast.parse) → commit if valid → PR
```

Phase 3 (test execution):
```
Issue → AI fix → syntax check → run pytest → commit if passing → PR with test results
```

Phase 4 (full sandbox):
```
Issue → AI fix → isolated container
               → install dependencies
               → run test suite
               → static analysis
               → commit if all pass
               → PR with full CI report
```

Phase 4 requires Docker/Firecracker/WebAssembly sandbox infrastructure — incompatible with Render free tier. The path requires moving to a paid compute environment or a self-hosted runner. The code architecture is ready for this extension: `run_autofix()` returns a string (the PR URL or error message) and all the sandboxing logic would sit between Stage 4 and Stage 5.
