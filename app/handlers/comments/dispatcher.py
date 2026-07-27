"""
app/handlers/comments/dispatcher.py
Command extraction, rate limiting, and provider-down handling.
"""

from __future__ import annotations

import re
import threading
import time
import logging

from .constants import ALL_COMMANDS, USER_CMD_LIMIT, USER_CMD_WINDOW

log = logging.getLogger(__name__)

# In-memory fallback for the per-user command rate limit when Redis is down.
# Bounded: entries are pruned on every check, and the dict is hard-capped so a
# spray of unique (repo, author) pairs cannot grow it without limit.
_local_cmd_counts: dict[str, list] = {}
_local_cmd_lock = threading.Lock()
_LOCAL_CMD_MAX_KEYS = 5000


def extract_command(body: str) -> str | None:
    """
    Word-boundary command extraction.
    Longest-match first prevents '/fix' matching inside '/autofix'.
    Negative lookbehind prevents matching substrings like 'prefix'.
    """
    body_lower = body.lower()
    # Sort by length descending so /autofix is tried before /fix
    for cmd in sorted(ALL_COMMANDS, key=len, reverse=True):
        if re.search(r"(?<![/\w])" + re.escape(cmd) + r"\b", body_lower):
            return cmd
    return None


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
