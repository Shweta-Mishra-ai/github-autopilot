"""
app/handlers/comments/dispatcher.py
Command extraction, rate limiting, and provider-down handling.
"""

from __future__ import annotations

import re
import time
import logging

from .constants import ALL_COMMANDS, USER_CMD_LIMIT, USER_CMD_WINDOW

log = logging.getLogger(__name__)


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
    Fail-open when Redis unavailable — bot stays usable.
    """
    try:
        from app.core.redis_client import get_redis

        r = get_redis()
        key = f"cmd_rl:{repo}:{author}:{int(time.time() // USER_CMD_WINDOW)}"
        cnt = r.incr(key)
        r.expire(key, USER_CMD_WINDOW)
        return int(cnt) <= USER_CMD_LIMIT
    except Exception:
        return True  # Redis unavailable → allow


def providers_down_comment(retry_in: int = 60) -> str:
    """Standard degraded-mode comment when all LLM providers are unavailable."""
    return (
        "## ⚠️ AI Temporarily Unavailable\n\n"
        "All language model providers are currently unavailable "
        f"(circuit breakers open). Earliest retry: **~{retry_in}s**.\n\n"
        "Please try again in a minute.\n\n"
        "> Transient issue — no action needed.\n\n"
        "---\n*🤖 AI Repo Manager V5*"
    )


def safe_router_ask(
    system: str,
    user: str,
    task: str,
    max_tokens: int = 1000,
) -> tuple[dict, object]:
    """
    Wrapper around router.ask() with structured error handling.

    Returns (result_dict, meta).
    - AllProvidersDown → ({_providers_down: True, _retry_in: N}, None)
    - Other errors    → ({}, None)

    Never raises. Callers check result.get('_providers_down') and post
    a visible degraded message rather than silently doing nothing.
    """
    from app.handlers.comments import router
    from app.ai.circuit_breaker import AllProvidersDown

    try:
        return router.ask(system, user, task=task, max_tokens=max_tokens)
    except AllProvidersDown as exc:
        log.error(f"router.all_providers_down task={task} retry_in={exc.retry_in_seconds}s")
        return {"_providers_down": True, "_retry_in": exc.retry_in_seconds}, None
    except Exception as exc:
        log.error(f"router.ask failed task={task}: {exc}")
        return {}, None


def is_providers_down(result: dict) -> bool:
    return isinstance(result, dict) and result.get("_providers_down") is True


def make_degraded_response(result: dict) -> str:
    retry_in = result.get("_retry_in", 60) if isinstance(result, dict) else 60
    return providers_down_comment(retry_in)
