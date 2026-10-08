"""
app/handlers/comments/dispatcher.py
Command extraction, rate limiting, and provider-down handling.
"""

from __future__ import annotations

import re
import threading
import time
import logging

from ._client import gh_get
from .constants import ALL_COMMANDS, PR_DIFF_CONTEXT_CHARS, USER_CMD_LIMIT, USER_CMD_WINDOW

log = logging.getLogger(__name__)

# In-memory fallback for the per-user command rate limit when Redis is down.
# Bounded: entries are pruned on every check, and the dict is hard-capped so a
# spray of unique (repo, author) pairs cannot grow it without limit.
_local_cmd_counts: dict[str, list] = {}
_local_cmd_lock = threading.Lock()
_LOCAL_CMD_MAX_KEYS = 5000


# Text a command in which is not an instruction to the bot: a fenced block, an
# inline code span, or a quoted line. The bot's own PR report says "Use
# `/gaps` ... or `/test`", so quote-replying it ran a command nobody typed, and
# pasting a log or snippet that mentions a command did the same.
_FENCED_RE = re.compile(r"```.*?(?:```|\Z)|~~~.*?(?:~~~|\Z)", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_QUOTE_LINE_RE = re.compile(r"(?m)^[ \t]*>.*$")
# Also not instructions: an HTML comment (invisible on GitHub — `<!-- /merge -->`
# ran /merge) and an indented code block (four spaces or a tab, rendered as
# code — "Steps I ran:\n\n    /release" cut a release).
_HTML_COMMENT_RE = re.compile(r"<!--.*?(?:-->|\Z)", re.S)
_INDENTED_RE = re.compile(r"(?m)^(?: {4,}|\t).*$")


def _blank_keeping_lines(m: re.Match) -> str:
    return "\n" * m.group(0).count("\n")


def _strip_non_instructions(body: str) -> str:
    """
    The comment with everything that is not an instruction blanked out, LINE
    FOR LINE, so a line found here is the same line in the original comment.
    """
    body = _FENCED_RE.sub(_blank_keeping_lines, body)
    body = _HTML_COMMENT_RE.sub(_blank_keeping_lines, body)
    body = _QUOTE_LINE_RE.sub("", body)
    body = _INDENTED_RE.sub("", body)
    return _INLINE_CODE_RE.sub(" ", body)


# A command must open a line, optionally after an @mention of the bot. It
# used to match anywhere, so prose ran commands: "See /release for details"
# cut a release, "I ran /test locally" generated tests.
_LINE_START = r"(?m)^[ \t]*(?:@[\w-]+(?:\[bot\])?[ \t,:]+)?"
_COMMAND_AT_LINE_START = re.compile(r"^[ \t]*(?:@[\w-]+(?:\[bot\])?[ \t,:]+)?(/[A-Za-z]+)(?!\w)")


def parse_command(body: str) -> tuple[str | None, str]:
    """
    (command, arguments) for the FIRST command a comment issues, or (None, "").

    A command counts only at the start of a line (after an optional
    @mention), and never inside a quote, code or an HTML comment. Its
    arguments are the rest of THAT line, in their original case.

    Two defects this replaces. The command picked was the LONGEST one found
    anywhere, so "/fix" then "/autofix" ran /autofix, while the documentation
    says the first command is the one processed. And the arguments were
    sliced from the first raw occurrence of the command text in the whole
    body — which could be inside a quote, a code block, prose or a longer
    word: "Running /rollback 2 confirm is scary…\n/rollback" gave /rollback
    the arguments "2 confirm is scary…", and "Our /circleci job fails\n/ci"
    gave /ci "rcleci job fails".
    """
    original = (body or "").split("\n")
    for i, line in enumerate(_strip_non_instructions(body or "").split("\n")):
        m = _COMMAND_AT_LINE_START.match(line)
        if not m or m.group(1).lower() not in ALL_COMMANDS:
            continue
        source = original[i] if i < len(original) else line
        om = _COMMAND_AT_LINE_START.match(source)
        args = source[om.end() :] if om else line[m.end() :]
        return m.group(1).lower(), args.strip()
    return None, ""


def extract_command(body: str) -> str | None:
    """The command a comment issues, or None. See parse_command."""
    return parse_command(body)[0]


def command_repeated_by_edit(payload: dict, cmd: str) -> bool:
    """
    True when an `edited` comment already held `cmd` before the edit.

    An edit runs a command only if the edit ADDED it. Every edit used to re-run
    whatever command the comment held — fixing a typo in an old `/release`
    comment cut another release. Correcting `/fxi` to `/fix` still runs,
    because the old body held no command.
    """
    if payload.get("action") != "edited":
        return False
    previous = ((payload.get("changes") or {}).get("body") or {}).get("from") or ""
    return extract_command(previous) == cmd


def check_user_rate_limit(repo: str, author: str) -> bool:
    """
    Returns True if user is within limit (USER_CMD_LIMIT / USER_CMD_WINDOW).
    Redis is the source of truth; when it is unavailable the limit is still
    enforced by a bounded in-memory sliding window (single-process deploys —
    gunicorn runs --workers 1 — so local counts are authoritative enough).
    """
    try:
        from app.core.redis_client import get_redis

        r = get_redis()
        key = f"cmd_rl:{repo}:{author}:{int(time.time() // USER_CMD_WINDOW)}"
        cnt = r.incr(key)
        r.expire(key, USER_CMD_WINDOW)
        return int(cnt) <= USER_CMD_LIMIT
    except Exception as e:
        from app.core.metrics import metrics

        metrics.increment("ratelimit.redis_fallback")
        log.warning(f"ratelimit.redis_unavailable local_fallback repo={repo} author={author}: {e}")
        return _check_local_rate_limit(f"{repo}:{author}")


def _check_local_rate_limit(key: str) -> bool:
    """Bounded in-memory sliding window — same semantics as the Redis path."""
    now = time.time()
    with _local_cmd_lock:
        window = [t for t in _local_cmd_counts.get(key, []) if now - t < USER_CMD_WINDOW]
        window.append(now)
        if len(_local_cmd_counts) >= _LOCAL_CMD_MAX_KEYS and key not in _local_cmd_counts:
            # Cap reached: prune every expired window before admitting a new key.
            for k in [
                k
                for k, w in _local_cmd_counts.items()
                if all(now - t >= USER_CMD_WINDOW for t in w)
            ]:
                _local_cmd_counts.pop(k, None)
            if len(_local_cmd_counts) >= _LOCAL_CMD_MAX_KEYS:
                # Still full of live windows → deny rather than grow unbounded.
                log.warning(f"ratelimit.local_capacity_deny key={key}")
                return False
        _local_cmd_counts[key] = window
        return len(window) <= USER_CMD_LIMIT


def augment_with_memory(context: str, repo: str, query: str) -> str:
    """
    Append recalled repository memory to the prompt context.

    Privacy guard lives in memory.recall_context(): it returns "" unless a local
    model is active (or MEMORY_ALLOW_CLOUD=1), so sensitive learned context never
    leaks to a cloud LLM in the default configuration. Never raises — memory is
    an enhancement, not a hard dependency.
    """
    try:
        from app.intelligence.memory import recall_context

        mem_ctx = recall_context(repo, query)
        if mem_ctx:
            log.info(f"memory.injected repo={repo}")
            return f"{context}\n\n{mem_ctx}"
    except Exception as exc:
        log.debug(f"memory.augment_skipped repo={repo}: {exc}")
    return context


def pr_context(repo: str, number: int, issue: dict, token: str, log_ctx) -> str:
    """
    A PR's title, description and the start of its diff, or "" on failure.

    These commands were given the title and description only — never the
    code. `/gaps` analysed the PR's prose, `/test` wrote tests for it, and the
    PR report recommends both for "a detailed analysis".
    """
    try:
        from app.github.helpers import pr_files
        from app.handlers.pull_request.analysis import _diff_excerpt

        files = pr_files(repo, number, token, get=gh_get)
        listing = "\n".join(
            f"- {f.get('filename', '?')} (+{f.get('additions', 0)} -{f.get('deletions', 0)})"
            for f in files[:15]
        )
        diff = _diff_excerpt(files, PR_DIFF_CONTEXT_CHARS)
        return (
            f"Title: {issue.get('title', '')}\n"
            f"Body: {(issue.get('body') or '')[:500]}\n\n"
            f"Changed files ({len(files)}):\n{listing}\n\n"
            f"Diff (start of each file):\n{diff or '(no diff available)'}"
        )
    except Exception as exc:
        log_ctx.warning("pr_context_unavailable", reason=str(exc)[:100])
        return ""


def command_disabled_comment(cmd: str) -> str:
    """
    Response when an operator's `commands.enabled` allow-list excludes `cmd`.

    Lives here with the other canned responses so service.py stays a thin
    orchestration layer.
    """
    return (
        f"## 🚫 Command Disabled\n\n"
        f"`{cmd}` is not in this repository's `commands.enabled` list "
        f"in `.ai-repo-manager.yml`."
    )


def empty_response_comment(cmd: str) -> str:
    """
    Reply when a command handler returns nothing at all.

    This should be unreachable: every dispatcher arm returns a non-empty
    string, and a test asserts it. It exists because the alternative when it
    *is* reached is silence, and silence is the worst answer the bot can give
    — the reader cannot tell it apart from the service being down, the webhook
    never arriving, or the command not existing, so they retry, then file an
    issue, then stop using it.

    Says what is known and what to do next, and does not speculate about the
    cause: the handler returned nothing, so there is no cause to report.
    """
    return (
        f"## ⚠️ `{cmd}` Produced No Output\n\n"
        "The command ran and was allowed to run, but its handler returned "
        "nothing to post. That is a bug in this bot, not something you did "
        "wrong.\n\n"
        "**What to try:**\n"
        "- Run the command again — if it was transient, it will work.\n"
        "- Check `/health` for a provider outage or misconfiguration.\n\n"
        "> If it keeps happening, please open an issue with this comment's "
        "link. The failure is recorded in the deployment logs as "
        "`empty_response`."
    )


def providers_down_comment(retry_in: int = 60) -> str:
    """Standard degraded-mode comment when all LLM providers are unavailable."""
    return (
        "## ⚠️ AI Temporarily Unavailable\n\n"
        "All language model providers are currently unavailable "
        f"(circuit breakers open). Earliest retry: **~{retry_in}s**.\n\n"
        "Please try again in a minute.\n\n"
        "> Transient issue — no action needed.\n\n"
        "---\n*🤖 GitHub Autopilot*"
    )


def safe_router_ask(
    system: str,
    user: str,
    task: str,
    max_tokens: int = 1000,
) -> tuple[dict, object]:
    """
    Deprecated alias — the implementation now lives in app/ai/guarded.py.

    It moved down a layer because app.ai must not import from app.handlers:
    guarded.py is imported by generator.py, which app.handlers.comments imports
    at package init, so the old direction was a circular import.

    Prefer app.ai.guarded.guarded_ask(), which adds the hallucination check.
    This shim remains for callers that only need the never-raises behaviour.
    """
    from app.ai.guarded import safe_router_ask as _impl

    return _impl(system, user, task=task, max_tokens=max_tokens)


def is_providers_down(result: dict) -> bool:
    return isinstance(result, dict) and result.get("_providers_down") is True


def make_degraded_response(result: dict) -> str:
    retry_in = result.get("_retry_in", 60) if isinstance(result, dict) else 60
    return providers_down_comment(retry_in)
