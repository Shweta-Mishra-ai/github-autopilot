"""
app/ai/metrics.py
Today's LLM usage per provider, for /budget.

The limits are routing_policy.DAILY_LIMITS — the deployment's assumptions,
overridable per provider — not a second hard-coded table. This module had its
own copy, labelled "Groq Llama 70B" for a slot that serves gpt-oss-120b, with
"$0.00 (free)" cost strings and limits presented as the provider's quota.
"""

import datetime
import logging
import os

log = logging.getLogger(__name__)


def _model_labels() -> dict:
    """The model each provider slot is configured to call."""
    from app.ai.providers.gemini import gemini_model
    from app.ai.providers.openrouter import openrouter_model
    from app.ai.router import DEFAULT_FALLBACK_MODEL, DEFAULT_PRIMARY_MODEL

    return {
        "groq_70b": f"Groq · {os.environ.get('LLM_PRIMARY_MODEL', DEFAULT_PRIMARY_MODEL)}",
        "groq_8b": f"Groq · {os.environ.get('LLM_FALLBACK_MODEL', DEFAULT_FALLBACK_MODEL)}",
        "gemini": f"Gemini · {gemini_model()}",
        "openrouter": f"OpenRouter · {openrouter_model()}",
    }


def get_usage_today() -> dict:
    """Per-provider usage for today (UTC). Raises when Redis cannot be read —
    a failed read used to be reported as zero use."""
    from app.ai.routing_policy import DAILY_LIMITS
    from app.core.redis_client import get_redis

    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    labels = _model_labels()
    r = get_redis()
    result = {}
    for pk, limits in DAILY_LIMITS.items():
        req_used = int(r.get(f"llm:requests:{pk}:{today}") or 0)
        tok_used = int(r.get(f"llm:tokens:{pk}:{today}") or 0)
        req_limit = limits.get("requests") or 0
        tok_limit = limits.get("tokens") or 0
        result[pk] = {
            "label": labels.get(pk, pk),
            "requests_used": req_used,
            "requests_limit": req_limit,
            "requests_pct": round(req_used / req_limit * 100, 1) if req_limit else 0,
            "tokens_used": tok_used,
            "tokens_limit": tok_limit,
            "tokens_pct": round(tok_used / tok_limit * 100, 1) if tok_limit else 0,
        }
    return result


def format_budget_comment() -> str:
    """Formats /budget command GitHub comment."""
    from app.ai.circuit_breaker import status_all
    import os

    try:
        usage = get_usage_today()
    except Exception as e:
        log.warning(f"metrics.usage_unreadable: {e}")
        return (
            "## 💰 LLM Budget\n\nToday's usage could not be read from Redis "
            f"(`{type(e).__name__}`). This is not a report of zero use."
        )
    breakers = status_all()
    today = datetime.datetime.now(datetime.timezone.utc).date().strftime("%B %d, %Y")

    lines = [f"## 💰 LLM Budget — {today}\n"]

    lines.append("| Provider | Requests | Tokens | Status |")
    lines.append("|----------|----------|--------|--------|")

    for pk, data in usage.items():
        req_pct = data["requests_pct"]
        tok_pct = data["tokens_pct"]

        if req_pct >= 90 or tok_pct >= 90:
            status_emoji = "🔴 Critical"
        elif req_pct >= 70 or tok_pct >= 70:
            status_emoji = "🟡 High"
        else:
            status_emoji = "🟢 OK"

        cb = breakers.get(pk, {})
        if cb.get("state") == "open":
            status_emoji = "⛔ Circuit Open"
        elif cb.get("state") == "half_open":
            status_emoji = "🟠 Recovering"

        lines.append(
            f"| **{data['label']}** | "
            f"{data['requests_used']:,}/{data['requests_limit']:,} ({req_pct}%) | "
            f"{data['tokens_used']:,}/{data['tokens_limit']:,} ({tok_pct}%) | "
            f"{status_emoji} |"
        )

    lines.append("\n### Circuit Breakers\n")
    all_ok = True
    for pk, state in breakers.items():
        label = usage.get(pk, {}).get("label", pk)
        s = state.get("state", "unknown")
        if s == "closed":
            icon = "✅"
        elif s == "half_open":
            icon = "🟠"
            all_ok = False
        else:
            icon = "⛔"
            all_ok = False
            retry = state.get("recovers_in_seconds", 0)
            s = f"OPEN — retries in {retry}s" if retry else "OPEN"
        lines.append(f"- {icon} **{label}**: {s}")

    if all_ok:
        lines.append("\n_All providers healthy_ 🎉")

    if not os.environ.get("GEMINI_API_KEY"):
        lines.append(
            "\n> ⚠️ **Gemini not configured** — add `GEMINI_API_KEY` in Render env "
            "for long-context fallback."
        )

    lines.append("\n---")
    lines.append(
        "Limits are this deployment's assumptions (`LLM_DAILY_LIMIT_<PROVIDER>_TOKENS`/"
        "`_REQUESTS`), not read from the provider — set them to your plan's quotas. "
        "Counts reset at midnight UTC. 🟢 < 70% · 🟡 70–90% · 🔴 > 90%"
    )

    return "\n".join(lines)
