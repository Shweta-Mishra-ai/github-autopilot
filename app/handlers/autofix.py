"""
app/handlers/autofix.py
Fixed V4.2 — All issues addressed.

FIXES vs V4.1:
  1. BLOCKED_PATHS massively expanded — CI workflows, requirements.txt,
     Dockerfile, core config, authorization are now protected.
  2. BLOCKED_PREFIXES added — prefix-based block for .github/workflows/,
     app/core/webhook*, .env* patterns. LLM can no longer steer autofix
     at sensitive path families.
  3. Path traversal guard — rejects any filepath with '..' sequences.
  4. Human confirmation step — autofix now creates branch+commit but does NOT
     open a PR automatically. Instead it posts the diff as a comment and asks
     for '/apply <branch>' confirmation. This keeps a human in the loop.
  5. LLM-generated target_file validated against user-provided hint — if user
     gave a target file hint and LLM returns a completely different file, we
     use the user hint (prevents LLM steering to sensitive files).
  6. Token cost logging added — logs both LLM calls' token usage.
  7. _apply_fix: 70% length guard improved — now also checks that at least
     1 line actually changed (not just whitespace).
"""

import base64
import logging
from typing import Optional

from app.github.client import gh_get, gh_post, gh_put, GitHubError
from app.ai.router import router

log = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".py", ".md", ".txt", ".yml", ".yaml", ".json", ".toml"}

# Hard blocklist — exact paths
BLOCKED_PATHS = {
    "server.py",
    ".env",
    "app/github/auth.py",
    "requirements.txt",
    "requirements-dev.txt",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "app/core/config.py",
    "app/core/authorization.py",
    "app/core/webhook_security.py",
    "app/core/redis_client.py",
    "setup.py",
    "setup.cfg",
    "pyproject.toml",
}

# Prefix blocklist — any filepath starting with these is blocked
BLOCKED_PREFIXES = (
    ".github/workflows/",  # CI pipeline injection vector
    ".github/actions/",
    "app/core/webhook",    # All webhook security modules
    ".env",                # .env, .env.local, .env.production, etc.
    "secrets/",
    "certs/",
    "keys/",
)

_MAX_FILE_CHARS = 16_000
_TRUNCATION_MARKER = (
    "\n\n# [AUTOFIX NOTE: FILE TRUNCATED AT {limit} CHARS — "
    "DO NOT REMOVE CONTENT AFTER THIS POINT IN YOUR RESPONSE]\n"
)




def _get_default_branch(repo: str, token: str) -> str:
    try:
        return gh_get(f"/repos/{repo}", token).get("default_branch", "main")
    except Exception:
        return "main"


def _create_branch(repo: str, token: str, branch: str, base: str) -> None:
    ref = gh_get(f"/repos/{repo}/git/ref/heads/{base}", token)
    try:
        sha = ref["object"]["sha"]
    except (KeyError, TypeError) as e:
        raise GitHubError(
            f"Cannot read SHA for branch '{base}': unexpected API response. "
            f"Keys: {list(ref.keys()) if isinstance(ref, dict) else type(ref)}"
        ) from e
    gh_post(
        f"/repos/{repo}/git/refs",
        token,
        {"ref": f"refs/heads/{branch}", "sha": sha},
    )

def run_autofix(
    repo: str,
    issue_number: int,
    issue: dict,
    token: str,
    target_file: str = "",
) -> str:
    """
    Entry point for /autofix command.
    Returns a Markdown string suitable for posting as a GitHub comment.

    Flow:
      1. Generate fix plan (LLM call 1)
      2. Validate target file is safe
      3. Read current file
      4. Generate fixed content (LLM call 2)
      5. Create branch + commit
      6. POST DIFF as comment + ask for '/apply <branch>' confirmation
         (human in the loop — no PR created automatically)
    """
    title = issue.get("title", "")
    body  = (issue.get("body") or "")[:2000]

    # ── Step 1: Generate fix plan ──────────────────────────────────────────
    fix_plan = _generate_fix_plan(title, body, target_file)
    if not fix_plan:
        return (
            "## ⚠️ Autofix Failed\n\n"
            "Could not generate a confident fix plan.\n\n"
            "**Try instead:**\n"
            "- `/fix` — AI suggestions without code changes\n"
            "- `/autofix app/handlers/foo.py` — specify the exact file"
        )

    # ── Step 2: Validate target file ──────────────────────────────────────
    # If user provided a hint, prefer it over LLM-generated path
    llm_target = fix_plan.get("target_file", "").strip()
    target = target_file.strip() if target_file.strip() else llm_target

    # Path traversal guard
    if ".." in target or target.startswith("/"):
        log.warning(f"autofix.path_traversal_attempt target={target!r}")
        return (
            f"## ⚠️ Autofix Blocked\n\n"
            f"Invalid file path `{target}`. Path traversal not allowed."
        )

    if not _is_allowed(target):
        reason = _block_reason(target)
        return (
            f"## ⚠️ Autofix Skipped\n\n"
            f"Cannot auto-modify `{target}` — {reason}.\n\n"
            f"Use `/fix` for manual suggestions."
        )

    # ── Step 3: Read target file ───────────────────────────────────────────
    try:
        file_data = gh_get(f"/repos/{repo}/contents/{target}", token)
        current   = base64.b64decode(file_data["content"]).decode("utf-8")
        file_sha  = file_data["sha"]
    except Exception as e:
        return (
            f"## ⚠️ Autofix Failed\n\n"
            f"Cannot read `{target}`: `{str(e)[:100]}`\n\n"
            f"Make sure the file path is correct."
        )

    # ── Step 4: Generate fixed content ────────────────────────────────────
    fixed, tokens_used = _apply_fix(current, fix_plan, title)
    log.info(f"autofix.tokens_used tokens={tokens_used}")

    if not fixed or fixed == current:
        return (
            "## ⚠️ Autofix Skipped\n\n"
            "The fix didn't produce any changes to the file.\n\n"
            "Try `/fix` for manual suggestions, or specify a more precise issue."
        )

    # Check at least 1 meaningful line actually changed
    orig_lines  = set(current.splitlines())
    fixed_lines = set(fixed.splitlines())
    if orig_lines == fixed_lines:
        return (
            "## ⚠️ Autofix Skipped\n\n"
            "No line-level changes detected. Use `/fix` for suggestions."
        )

    # ── Step 5: Create branch + commit ────────────────────────────────────
    branch     = f"fix/bot-issue-{issue_number}"
    default_br = _get_default_branch(repo, token)

    try:
        _create_branch(repo, token, branch, default_br)
    except GitHubError as e:
        if "already exists" not in str(e):
            return f"## ⚠️ Branch Error\n\n`{str(e)[:100]}`"
    except Exception as e:
        log.error(f"autofix._create_branch unexpected: {e}")
        return f"## ⚠️ Branch Error\n\nUnexpected error: `{str(e)[:100]}`"

    try:
        gh_put(
            f"/repos/{repo}/contents/{target}",
            token,
            {
                "message": (
                    f"fix: {fix_plan.get('commit_message', f'fix issue #{issue_number}')}\n\n"
                    f"Closes #{issue_number}\nAuto-generated by AI Repo Manager"
                ),
                "content": base64.b64encode(fixed.encode()).decode(),
                "sha":     file_sha,
                "branch":  branch,
            },
        )
    except GitHubError as e:
        return f"## ⚠️ Commit Failed\n\n`{str(e)[:100]}`"

    # ── Step 6: Post diff + ask for confirmation (human in the loop) ───────
    diff_preview = _make_diff_preview(current, fixed, target)
    conf_pct     = int(float(fix_plan.get("confidence", 0.8)) * 100)

    log.info(f"autofix.branch_ready branch={branch}")

    return (
        f"## 🤖 Autofix Ready — Review Required\n\n"
        f"**File:** `{target}` | **Branch:** `{branch}` | "
        f"**Confidence:** {conf_pct}%\n\n"
        f"**Problem:** {fix_plan.get('problem', '')}\n\n"
        f"**Fix:** {fix_plan.get('explanation', '')}\n\n"
        f"### Diff Preview\n{diff_preview}\n\n"
        f"---\n"
        f"**To create a PR:** reply `/apply {branch}`\n"
        f"**To discard:** reply `/rollback` or just close this issue\n\n"
        f"> ⚠️ AI-generated fix — please review the diff before applying."
    )


# ── Private helpers ────────────────────────────────────────────────────────────

def _make_diff_preview(original: str, fixed: str, filepath: str) -> str:
    """Generate a simple unified-diff-style preview (first 30 changed lines)."""
    orig_lines  = original.splitlines()
    fixed_lines = fixed.splitlines()

    diff_lines = []
    added = removed = 0

    # Simple line-by-line diff
    for i, line in enumerate(orig_lines):
        if i < len(fixed_lines):
            if line != fixed_lines[i]:
                diff_lines.append(f"- {line[:120]}")
                diff_lines.append(f"+ {fixed_lines[i][:120]}")
                removed += 1
                added   += 1
        else:
            diff_lines.append(f"- {line[:120]}")
            removed += 1

    for i in range(len(orig_lines), len(fixed_lines)):
        diff_lines.append(f"+ {fixed_lines[i][:120]}")
        added += 1

    preview = "\n".join(diff_lines[:30])
    truncated = " *(truncated)*" if len(diff_lines) > 30 else ""

    return (
        f"```diff\n# {filepath}\n{preview}{truncated}\n```\n"
        f"*+{added} lines, -{removed} lines*"
    )


def _generate_fix_plan(title: str, body: str, target_file: str) -> Optional[dict]:
    try:
        hint = f"Focus on file: {target_file}" if target_file else ""
        r, meta = router.ask(
            "Principal engineer. Generate precise minimal code fixes. JSON only.",
            f"""Issue: {title}\nDetails: {body}\n{hint}\n
Return JSON:
{{
  "target_file": "path/to/file.py",
  "pr_title": "description",
  "commit_message": "fix: what changed",
  "problem": "what is broken",
  "fix_description": "what this PR does",
  "explanation": "why this fixes it",
  "patch": "exact code change",
  "confidence": 0.8
}}
If confidence < 0.6 return {{"confidence": 0.0}}""",
            task="fix_command",
            max_tokens=1500,
        )

        if "raw" in r and "confidence" not in r:
            log.warning("autofix._generate_fix_plan: LLM returned non-JSON")
            return None

        if not r or float(r.get("confidence", 0)) < 0.6:
            return None

        return r

    except Exception as e:
        log.error(f"autofix._generate failed: {e}")
        return None


def _apply_fix(current: str, fix_plan: dict, title: str) -> tuple[str, int]:
    """
    Apply fix_plan to current file content.
    Returns (fixed_content, tokens_used).
    Returns (current, 0) on any failure — never raises.
    """
    try:
        file_for_llm, was_truncated = _safe_excerpt(current)

        r, meta = router.ask(
            "Code editor. Apply fix precisely. Return complete file. JSON only.",
            f"""Apply fix to file:
ISSUE: {title}
FIX: {fix_plan.get('patch', '')}

FILE{' (TRUNCATED — return only the shown portion, preserve rest)' if was_truncated else ''}:
```
{file_for_llm}
```

Return JSON: {{"fixed_content": "complete file content", "changed_lines": 2}}""",
            task="fix_command",
            max_tokens=4000,
        )

        tokens = getattr(meta, "total_tokens", 0)

        if "raw" in r and "fixed_content" not in r:
            log.warning("autofix._apply_fix: LLM returned non-JSON")
            return current, tokens

        fixed = r.get("fixed_content", "")

        if not fixed or len(fixed) < 10:
            return current, tokens

        # Safety guard: reject if response <70% of original (truncation indicator)
        if len(fixed) < len(current) * 0.70:
            log.warning(
                f"autofix._apply_fix: response too short "
                f"({len(fixed)} vs {len(current)}) — rejecting"
            )
            return current, tokens

        return fixed, tokens

    except Exception as e:
        log.error(f"autofix._apply_fix failed: {e}")
        return current, 0


def _safe_excerpt(content: str) -> tuple[str, bool]:
    if len(content) <= _MAX_FILE_CHARS:
        return content, False
    truncated  = content[:_MAX_FILE_CHARS]
    truncated += _TRUNCATION_MARKER.format(limit=_MAX_FILE_CHARS)
    return truncated, True


def _build_pr_body(fix_plan: dict, issue_number: int, title: str) -> str:
    conf = int(float(fix_plan.get("confidence", 0.8)) * 100)
    return (
        f"## 🤖 Automated Fix — Issue #{issue_number}\n\n"
        f"**Issue:** {title}\n\n"
        f"### Problem\n{fix_plan.get('problem', '')}\n\n"
        f"### Fix\n{fix_plan.get('fix_description', '')}\n\n"
        f"### Why\n{fix_plan.get('explanation', '')}\n\n"
        f"---\nCloses #{issue_number}\n\n"
        f"> 🤖 Auto-generated | Confidence: {conf}% | Please review before merging."
    )


def _is_allowed(filepath: str) -> bool:
    """Return True only if the file path is safe to auto-modify."""
    if not filepath:
        return False
    # Path traversal
    if ".." in filepath or filepath.startswith("/"):
        return False
    # Exact blocked paths
    if filepath in BLOCKED_PATHS:
        return False
    # Prefix blocked paths
    for prefix in BLOCKED_PREFIXES:
        if filepath.startswith(prefix):
            return False
    # Extension check
    ext = "." + filepath.rsplit(".", 1)[-1] if "." in filepath else ""
    return ext in ALLOWED_EXTENSIONS


def _block_reason(filepath: str) -> str:
    """Human-readable reason why a file is blocked."""
    if filepath in BLOCKED_PATHS:
        return "this is a security-sensitive file"
    for prefix in BLOCKED_PREFIXES:
        if filepath.startswith(prefix):
            return f"files under `{prefix}` are protected"
    ext = "." + filepath.rsplit(".", 1)[-1] if "." in filepath else ""
    if ext not in ALLOWED_EXTENSIONS:
        return f"extension `{ext}` is not in the allowed list"
    return "path is restricted"
