"""
app/ai/metrics.py
V4 Sprint 2: LLM usage tracker + /budget command formatter.

Tracks per-provider daily usage in Redis.
Used by:
  - router.py   → logs every call
  - comments.py → /budget command response
  - /health     → shows provider status
"""

import datetime
import logging

log = logging.getLogger(__name__)

# Free tier daily limits (conservative — 80% of actual)
PROVIDER_LIMITS = {
    "groq_70b": {
        "requests": 5_000,
        "tokens":   80_000,
        "label":    "Groq Llama 70B",
        "cost":     "$0.00 (free)",
    },
    "groq_8b": {
        "requests": 12_000,
        "tokens":   400_000,
        "label":    "Groq Llama 8B",
        "cost":     "$0.00 (free)",
    },
    "gemini": {
        "requests": 1_200,
        "tokens":   800_000,
        "label":    "Gemini Flash",
        "cost":     "$0.00 (free)",
    },
    "openrouter": {
        "requests": 200,
        "tokens":   50_000,
        "label":    "OpenRouter (emergency)",
        "cost":     "~$0.01/1K tokens",
    },
}


def get_usage_today() -> dict:
    """
    Returns per-provider usage for today.
    Format: {provider_key: {requests, tokens, req_pct, tok_pct}}
    """
    today  = datetime.date.today().isoformat()
    result = {}

    try:
        from app.core.redis_client import get_redis
        r = get_redis()

        for pk, limits in PROVIDER_LIMITS.items():
            req_used = int(r.get(f"llm:requests:{pk}:{today}") or 0)
            tok_used = int(r.get(f"llm:tokens:{pk}:{today}") or 0)

            req_limit = limits["requests"]
            tok_limit = limits["tokens"]

            result[pk] = {
                "label":          limits["label"],
                "requests_used":  req_used,
                "requests_limit": req_limit,
                "requests_pct":   round(req_used / req_limit * 100, 1) if req_limit else 0,
                "tokens_used":    tok_used,
                "tokens_limit":   tok_limit,
                "tokens_pct":     round(tok_used / tok_limit * 100, 1) if tok_limit else 0,
                "cost":           limits["cost"],
            }

    except Exception as e:
        log.warning(f"metrics.get_usage_today failed: {e}")

    return result


def format_budget_comment() -> str:
    """
    Formats /budget command GitHub comment.
    Shows per-provider usage with visual bars.
    """
    from app.ai.circuit_breaker import status_all
    import os

    usage    = get_usage_today()
    breakers = status_all()
    today    = datetime.date.today().strftime("%B %d, %Y")

    lines = [f"## 💰 LLM Budget — {today}\n"]

    # Provider table
    lines.append("| Provider | Requests | Tokens | Status |")
    lines.append("|----------|----------|--------|--------|")

    for pk, data in usage.items():
        req_pct = data["requests_pct"]
        tok_pct = data["tokens_pct"]

        # Status emoji
        if req_pct >= 90 or tok_pct >= 90:
            status_emoji = "🔴 Critical"
        elif req_pct >= 70 or tok_pct >= 70:
            status_emoji = "🟡 High"
        else:
            status_emoji = "🟢 OK"

        # Circuit breaker state
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

    # Circuit breaker section
    lines.append("\n### Circuit Breakers\n")
    all_ok = True
    for pk, state in breakers.items():
        label = PROVIDER_LIMITS.get(pk, {}).get("label", pk)
        s     = state.get("state", "unknown")
        if s == "closed":
            icon = "✅"
        elif s == "half_open":
            icon = "🟠"
            all_ok = False
        else:
            icon = "⛔"
            all_ok = False
            retry = state.get("recovers_in_seconds", 0)
            s     = f"OPEN — retries in {retry}s" if retry else "OPEN"
        lines.append(f"- {icon} **{label}**: {s}")

    if all_ok:
        lines.append("\n_All providers healthy_ 🎉")

    # Gemini availability
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if not gemini_key:
        lines.append(
            "\n> ⚠️ **Gemini not configured** — add `GEMINI_API_KEY` in Render env "
            "for long-context fallback. Free at https://aistudio.google.com/app/apikey"
        )

    lines.append("\n---")
    lines.append("🟢 < 70% · 🟡 70–90% · 🔴 > 90% · Resets at midnight UTC")

    return "\n".join(lines)


def record_call(provider_key: str, tokens: int):
    """
    Manual usage recording (used when providers don't self-report).
    router.py calls this automatically — handlers don't need to.
    """
    try:
        from app.core.redis_client import get_redis
        if tokens <= 0:
            return
        r     = get_redis()
        today = datetime.date.today().isoformat()
        for k in (
            f"llm:tokens:{provider_key}:{today}",
            f"llm:requests:{provider_key}:{today}",
        ):
            r.incr(k)
            r.expire(k, 86400)
    except Exception:
        pass
