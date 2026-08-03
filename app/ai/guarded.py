"""
app/ai/guarded.py — the single seam where an LLM answer becomes publishable.

Before V7 the hallucination detector guarded exactly one of ~30 output paths
(/fix). Everything else — code review, triage, CI analysis, /impact, /arch —
published whatever came back, including responses the model never managed to
format as JSON.

Routing every command through guarded_ask() makes the check structural rather
than something each new command has to remember: a test enumerates the command
registry and asserts this seam is used.
"""

from __future__ import annotations

import logging

from app.ai.circuit_breaker import AllProvidersDown
from app.ai.hallucination import HallucinationResult, check_response
from app.ai.validator import is_unusable

log = logging.getLogger(__name__)


def safe_router_ask(
    system: str,
    user: str,
    task: str,
    max_tokens: int = 1000,
) -> tuple[dict, object]:
    """
    router.ask() that never raises.

    Lives here rather than in app/handlers/comments/dispatcher.py because the
    AI layer must not import from the handlers layer — doing so created a
    circular import (handlers.comments imports generator, which imports this
    module). dispatcher.safe_router_ask now delegates here.

    Returns (result_dict, meta):
      - AllProvidersDown → ({_providers_down: True, _retry_in: N}, None)
      - other errors     → ({}, None)
    """
    from app.ai.router import router

    try:
        return router.ask(system, user, task=task, max_tokens=max_tokens)
    except AllProvidersDown as exc:
        log.error(f"router.all_providers_down task={task} retry_in={exc.retry_in_seconds}s")
        return {"_providers_down": True, "_retry_in": exc.retry_in_seconds}, None
    except Exception as exc:
        log.error(f"router.ask failed task={task}: {exc}")
        return {}, None


# One alert per outage, not one per affected request. A total provider outage
# affects every command at once, so an un-deduplicated notification would page
# the operator dozens of times for a single incident — the same noise problem
# this release exists to fix.
_PROVIDERS_DOWN_ALERT_TTL = 900  # 15 minutes


def _alert_providers_down() -> None:
    """Notify once per window that every provider is unavailable. Never raises."""
    try:
        from app.core.redis_client import get_redis

        if (
            get_redis().set("alert:providers_down", "1", nx=True, ex=_PROVIDERS_DOWN_ALERT_TTL)
            is None
        ):
            return  # already alerted inside this window

        from app.github.notifications import notify_all_providers_down

        notify_all_providers_down()
        log.error("guarded.all_providers_down_alert_sent")
    except Exception as e:
        log.debug(f"guarded.providers_down_alert_failed: {e}")


def guarded_ask(
    system: str,
    user: str,
    task: str,
    response_type: str = "generic",
    context: dict | None = None,
    max_tokens: int = 1000,
) -> tuple[dict, HallucinationResult]:
    """
    router.ask() + unusable-payload guard + hallucination check.

    Returns (payload, verdict). The payload carries "_degraded": True when it
    must not be rendered as a real answer; callers check that flag and post an
    honest "couldn't analyse this" message instead of a fabricated one.

    Never raises — safe_router_ask already absorbs AllProvidersDown.
    """
    payload, _meta = safe_router_ask(system, user, task=task, max_tokens=max_tokens)

    if isinstance(payload, dict) and payload.get("_providers_down"):
        _alert_providers_down()
        return (
            {
                "_degraded": True,
                "_reason": "providers_down",
                "_retry_in": payload.get("_retry_in", 60),
            },
            HallucinationResult(
                confidence=0.0,
                warnings=["all providers down"],
                is_acceptable=False,
            ),
        )

    if is_unusable(payload):
        log.warning(f"guarded.unusable_payload task={task} type={response_type}")
        return (
            {"_degraded": True, "_reason": "unparseable"},
            HallucinationResult(
                confidence=0.0,
                warnings=["unparseable response"],
                is_acceptable=False,
            ),
        )

    verdict = check_response(payload, context=context, response_type=response_type)
    if verdict.should_block:
        log.warning(
            f"guarded.blocked task={task} type={response_type} "
            f"confidence={verdict.confidence} warnings={verdict.warnings[:3]}"
        )
        return (
            {
                "_degraded": True,
                "_reason": "low_confidence",
                "_confidence": verdict.confidence,
            },
            verdict,
        )

    return payload, verdict


def is_degraded(payload) -> bool:
    """True when a payload must not be rendered as a real answer."""
    return isinstance(payload, dict) and payload.get("_degraded") is True


def degraded_comment(payload: dict, what: str = "analysis") -> str:
    """
    Honest replacement text for a degraded payload.

    Never fabricates a result. The whole point of the guard is that saying
    nothing is better than saying something wrong confidently.
    """
    reason = payload.get("_reason", "unknown") if isinstance(payload, dict) else "unknown"

    if reason == "providers_down":
        retry = payload.get("_retry_in", 60)
        return (
            f"## ⚠️ AI Temporarily Unavailable\n\n"
            f"All model providers are currently down (earliest retry ~{retry}s). "
            f"No {what} was produced.\n\n"
            "> Transient issue — please try again shortly."
        )

    if reason == "unparseable":
        return (
            f"## ⚠️ {what.capitalize()} Unavailable\n\n"
            "The model returned a response that could not be parsed. Nothing was "
            "inferred from it — please retry.\n\n"
            "> Reporting this honestly rather than guessing."
        )

    return (
        f"## ⚠️ {what.capitalize()} Withheld\n\n"
        f"The generated {what} did not pass the reliability check, so it was not "
        "published rather than risk a misleading answer.\n\n"
        "> Please retry, or review manually."
    )
