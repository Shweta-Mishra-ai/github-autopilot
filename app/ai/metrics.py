"""
app/ai/metrics.py
V4 Sprint 3: LLM usage tracker + /budget formatter.

FIXED (Sprint 3 — bot review):
  - Variable `r` renamed to `redis_client` (more descriptive)
  - Added try/except inside get_usage_today() inner loop
  - Added None/missing key check before division
"""

import datetime
import logging

log = logging.getLogger(__name__)

PROVIDER_LIMITS = {
    "groq_70b": {
        "requests": 5_000,
        "tokens": 80_000,
        "label": "Groq Llama 70B",
        "cost": "$0.00 (free)",
    },
    "groq_8b": {
        "requests": 12_000,
        "tokens": 400_000,
        "label": "Groq Llama 8B",
        "cost": "$0.00 (free)",
    },
    "gemini": {
        "requests": 1_200,
        "tokens": 800_000,
        "label": "Gemini Flash",
        "cost": "$0.00 (free)",
    },
    "openrouter": {
        "requests": 200,
        "tokens": 50_000,
        "label": "OpenRouter (emergency)",
        "cost": "~$0.01/1K tokens",
    },
}


def get_usage_today() -> dict:
    """
    Returns per-provider usage for today.
    FIXED: renamed `r` → `redis_client`, added inner try/except per provider.
    """
    today = datetime.date.today().isoformat()
    result = {}

    try:
        from app.core.redis_client import get_redis

        redis_client = get_redis()  # FIXED: was `r`

        for pk, limits in PROVIDER_LIMITS.items():
            try:
                req_used = int(redis_client.get(f"llm:requests:{pk}:{today}") or 0)
                tok_used = int(redis_client.get(f"llm:tokens:{pk}:{today}") or 0)
            except Exception as inner_e:
                log.warning(f"metrics.get_usage_failed provider={pk}: {inner_e}")
                req_used = 0
                tok_used = 0

            req_limit = limits.get("requests") or 1  # FIXED: avoid div by zero
            tok_limit = limits.get("tokens") or 1

            result[pk] = {
                "label": limits["label"],
                "requests_used": req_used,
                "requests_limit": req_limit,
                "requests_pct": round(req_used / req_limit * 100, 1),
                "tokens_used": tok_used,
                "tokens_limit": tok_limit,
                "tokens_pct": round(tok_used / tok_limit * 100, 1),
                "cost": limits["cost"],
            }

    except Exception as e:
        log.warning(f"metrics.get_usage_today failed: {e}")

    return result


def format_budget_comment() -> str:
    """Formats /budget command GitHub comment."""
    from app.ai.circuit_breaker import status_all
    import os

    usage = get_usage_today()
    breakers = status_all()
    today = datetime.date.today().strftime("%B %d, %Y")

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
        label = PROVIDER_LIMITS.get(pk, {}).get("label", pk)
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
    lines.append("🟢 < 70% · 🟡 70–90% · 🔴 > 90% · Resets at midnight UTC")

    return "\n".join(lines)


def record_call(provider_key: str, tokens: int):
    """Manual usage recording."""
    try:
        from app.core.redis_client import get_redis

        if tokens <= 0:
            return
        redis_client = get_redis()  # FIXED: was `r`
        today = datetime.date.today().isoformat()
        for k in (
            f"llm:tokens:{provider_key}:{today}",
            f"llm:requests:{provider_key}:{today}",
        ):
            redis_client.incr(k)
            redis_client.expire(k, 86400)
    except Exception:
        pass
