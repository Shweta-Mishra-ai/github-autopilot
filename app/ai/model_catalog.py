"""
app/ai/model_catalog.py
Ask each provider which models it actually serves.

A hardcoded model id is a dated assertion about someone else's product. This
deployment learned that the expensive way: the provider retired every Llama
chat model, every AI command returned 404 for six days, and the fix was a
string in an environment variable that nobody knew to change.

Replacing one hardcoded id with a newer hardcoded id does not fix that — it
resets the timer. What fixes it is asking the provider.

Two uses:

  * `inventory()` — what is served right now, for an operator choosing a model
    or a preflight reporting one that has gone.
  * `best_model()` — the strongest currently-served model for a tier, matched
    by FAMILY pattern rather than exact id, so a version bump inside a family
    (…-120b -> …-121b) is picked up without a code change.

Nothing here is on the hot path. Providers consult it only after the provider
itself has said a model does not exist, so a correctly-configured deployment
never pays for it.

Every function fails open and returns something usable. A catalogue lookup
that raised would replace a precise "the model is gone" with a stack trace
about the lookup, which is the failure this module exists to prevent.
"""

import logging
import os
import re
import threading
import time

log = logging.getLogger(__name__)

# Cached because the answer changes on the provider's release cadence, not
# ours, and because this is consulted while a webhook is waiting.
CATALOG_TTL_SECONDS = float(os.environ.get("LLM_CATALOG_TTL_SECONDS", "21600"))  # 6h
_CATALOG_TIMEOUT = 10

_cache: dict[str, tuple[list[str], float]] = {}
_cache_lock = threading.Lock()


# ── Where each provider publishes its catalogue ───────────────────────────────
#
# `key_env` empty means the endpoint needs no credential.
_ENDPOINTS = {
    "groq": {
        "url": "https://api.groq.com/openai/v1/models",
        "key_env": "GROQ_API_KEY",
        "auth": "bearer",
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/models",
        "key_env": "",  # public catalogue
        "auth": "",
    },
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/models",
        "key_env": "GEMINI_API_KEY",
        "auth": "x-goog-api-key",
    },
}

PROVIDERS = tuple(_ENDPOINTS)


def _extract_ids(provider: str, payload: dict) -> list[str]:
    """Provider catalogues disagree about shape; normalise to a list of ids."""
    if provider == "gemini":
        # {"models": [{"name": "models/gemini-x", "supportedGenerationMethods": [...]}]}
        out = []
        for m in payload.get("models") or []:
            name = str(m.get("name", "")).removeprefix("models/")
            methods = m.get("supportedGenerationMethods") or []
            # Embedding and token-counting models are served here too, and
            # asking one of them to review code returns a 400 that reads like
            # an outage.
            if name and (not methods or "generateContent" in methods):
                out.append(name)
        return out
    # OpenAI-compatible: {"data": [{"id": "..."}]}
    return [str(m.get("id", "")) for m in (payload.get("data") or []) if m.get("id")]


def available_models(provider: str, *, refresh: bool = False) -> list[str]:
    """
    Sorted model ids the provider serves right now, or [] when unknown.

    [] means "could not find out" — never "the provider has no models". Callers
    must treat it as no information rather than as an answer, because acting on
    an empty catalogue would take a working deployment down.
    """
    spec = _ENDPOINTS.get(provider)
    if not spec:
        return []

    now = time.time()
    if not refresh:
        with _cache_lock:
            hit = _cache.get(provider)
        if hit and now - hit[1] < CATALOG_TTL_SECONDS:
            return list(hit[0])

    key = os.environ.get(spec["key_env"], "").strip() if spec["key_env"] else ""
    if spec["key_env"] and not key:
        return []

    headers = {}
    if spec["auth"] == "bearer":
        headers["Authorization"] = f"Bearer {key}"
    elif spec["auth"] == "x-goog-api-key":
        headers["x-goog-api-key"] = key

    try:
        import requests

        r = requests.get(spec["url"], headers=headers, timeout=_CATALOG_TIMEOUT)
        if r.status_code != 200:
            log.warning(f"model_catalog.unavailable provider={provider} status={r.status_code}")
            return []
        ids = sorted({m for m in _extract_ids(provider, r.json()) if m})
    except Exception as exc:
        # Redacted: the Gemini endpoint took its key in the query string once,
        # and requests quotes the URL in every exception it raises.
        from app.core.redaction import redact_secrets

        log.warning(f"model_catalog.failed provider={provider}: {redact_secrets(str(exc))[:120]}")
        return []

    with _cache_lock:
        _cache[provider] = (ids, now)
    return list(ids)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ── Preference, by family rather than by exact id ─────────────────────────────
#
# Ordered most-preferred first, each entry a regex matched against the served
# id. Families, not exact ids: a provider bumping a version inside a family
# (qwen3.6 -> qwen3.8, gpt-oss-120b -> a larger successor) is picked up with no
# code change, which is the entire point of this module.
#
# Derived from each provider's own catalogue as printed by
# `python -m app.ai.model_catalog` — not from memory. An id matching nothing
# here is still selectable; it just sorts last, so a provider shipping a family
# nobody anticipated degrades to "some served model" rather than to nothing.
#
# The gemini patterns are the one unverified set: no GEMINI_API_KEY is
# configured in CI, so its catalogue could not be read. That costs nothing —
# a pattern that matches no served id simply never fires, and selection falls
# through to the generic ordering below.
_PREFERENCES: dict[tuple[str, str], tuple[str, ...]] = {
    ("groq", "quality"): (
        r"^openai/gpt-oss-\d{3,}b$",  # 120b, and any larger successor
        r"^qwen/qwen[\d.]+-\d+b$",
        r"^groq/compound$",
        r"^openai/gpt-oss-\d{1,2}b$",  # 20b
        r"^groq/compound-mini$",
    ),
    ("groq", "speed"): (
        r"^openai/gpt-oss-\d{1,2}b$",
        r"^groq/compound-mini$",
        r"^qwen/qwen[\d.]+-\d+b$",
        r"^groq/compound$",
        r"^openai/gpt-oss-\d{3,}b$",
    ),
    ("openrouter", "quality"): (
        # `super` before `ultra` on purpose. This provider is the EMERGENCY
        # fallback, reached only when the other two are already failing, so the
        # job is to answer at all. The largest free model on a free tier is
        # also the most contended one, and a 550B that queues is worth less
        # here than a 120B that replies.
        r"nemotron-[\d.]+-super",
        r"nemotron-[\d.]+-ultra",
        r"^minimax/",
        r"^z-ai/glm-",
        r"^thinkingmachines/inkling(:|$)",
        r"^google/gemma-",
        r"^poolside/laguna-s",
    ),
    ("openrouter", "speed"): (
        r"nemotron-[\d.]+-lightning",
        r"^google/gemma-",
        r"^liquid/lfm",
        r"^poolside/laguna-xs",
        r"^thinkingmachines/inkling-small",
        r"^minimax/",
    ),
    ("gemini", "quality"): (r"flash-latest", r"pro-latest", r"flash", r"pro"),
    ("gemini", "speed"): (r"flash-lite", r"flash-latest", r"flash", r"pro"),
}

# Served here, but never a sane automatic choice: not chat models at all
# (speech, embeddings, rerankers) or classifiers that answer a different
# question. Picking one produces a 400 that reads like an outage rather than
# like a bad choice, which is the worst way to fail.
_EXCLUDE = re.compile(
    r"(whisper|/tts|-tts|text-to-speech|orpheus|guard|embed|rerank|aqa|moderation"
    r"|content-safety|speech|transcri)",
    re.IGNORECASE,
)

# Usable, but narrow: tuned for one language, one domain, or one modality. Fine
# as a last resort, wrong as a default for reviewing code in English.
_NARROW = re.compile(
    r"(allam|arabic|saudi|-fin\b|-fin[:-]|omni|vision|note-preview)", re.IGNORECASE
)


def _rank(provider: str, tier: str, model_id: str) -> tuple[int, int]:
    prefs = _PREFERENCES.get((provider, tier), ())
    for index, pattern in enumerate(prefs):
        if re.search(pattern, model_id, re.IGNORECASE):
            return (1 if _NARROW.search(model_id) else 0), index
    return (1 if _NARROW.search(model_id) else 0), len(prefs)


def best_model(provider: str, tier: str = "quality", *, exclude: tuple[str, ...] = ()) -> str:
    """
    The best currently-served model for this tier, or "" when unknown.

    "" is returned whenever the catalogue could not be read or nothing passes
    the filters. Callers keep their configured model in that case: a guess is
    worse than the id an operator chose, and silently switching models on an
    unreadable catalogue would be a change nobody asked for.
    """
    candidates = [
        m for m in available_models(provider) if not _EXCLUDE.search(m) and m not in exclude
    ]
    if not candidates:
        return ""
    if provider == "openrouter":
        # Only the `:free` variants cost nothing, and this provider is the
        # emergency fallback — reaching it should never start a bill.
        free = [m for m in candidates if m.endswith(":free")]
        candidates = free or candidates

    # Descending, so that within one preference bucket the later version wins
    # (qwen3.8 over qwen3.6). `min` is stable and returns the first of equals,
    # so iteration order IS the tie-break.
    candidates.sort(reverse=True)
    return min(candidates, key=lambda m: _rank(provider, tier, m))


def inventory() -> dict[str, list[str]]:
    """Everything every configured provider serves. For operators and CI."""
    return {p: available_models(p) for p in PROVIDERS}


def format_inventory() -> str:
    lines = []
    for provider, models in inventory().items():
        spec = _ENDPOINTS[provider]
        if not models:
            why = (
                f"{spec['key_env']} not set"
                if spec["key_env"] and not os.environ.get(spec["key_env"], "").strip()
                else "catalogue unavailable"
            )
            lines.append(f"{provider}: — ({why})")
            continue
        lines.append(f"{provider}: {len(models)} models")
        for m in models:
            mark = "  " if _EXCLUDE.search(m) else "* "
            lines.append(f"    {mark}{m}")
        for tier in ("quality", "speed"):
            pick = best_model(provider, tier)
            lines.append(f"    -> best[{tier}]: {pick or '(none)'}")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - operator tool
    print(format_inventory())


# ── Substitution: what to do when the configured model is gone ────────────────
#
# A retired model id is unusable however it was chosen, so this applies to an
# operator-pinned id as much as to the built-in default. Refusing to work does
# not honour the operator's intent; it just goes down, which is precisely the
# six-day outage this module exists to prevent.
#
# The substitution is loud, in-process only, and lasts until the process
# restarts or the configuration is corrected. It is never silent: it logs at
# ERROR and is reported by /health and the doctor, because a bot quietly
# answering on a model nobody chose is its own kind of incident.
_substitutions: dict[str, dict] = {}
_sub_lock = threading.Lock()


def autoheal_enabled() -> bool:
    return os.environ.get("LLM_MODEL_AUTOHEAL", "1").strip() not in ("0", "false", "no")


def effective_model(provider_key: str, configured: str) -> str:
    """The model to actually ask for: a substitution if one is active."""
    with _sub_lock:
        entry = _substitutions.get(provider_key)
    return entry["to"] if entry else configured


def substitute(provider_key: str, provider: str, tier: str, failed_model: str) -> str:
    """
    Pick a replacement for a model the provider says it does not serve.

    Returns "" when there is nothing better to do — no catalogue, autoheal
    disabled, or the catalogue offers nothing but the model that just failed.
    "" means the caller reports the configuration error exactly as before, so
    this can only ever turn a hard failure into a working request or leave it
    unchanged.
    """
    if not autoheal_enabled():
        return ""

    with _sub_lock:
        active = _substitutions.get(provider_key)
    if active and active["to"] != failed_model:
        # Something already substituted; use it rather than re-deriving.
        return active["to"]

    replacement = best_model(provider, tier, exclude=(failed_model,))
    if not replacement or replacement == failed_model:
        return ""

    with _sub_lock:
        _substitutions[provider_key] = {
            "from": failed_model,
            "to": replacement,
            "provider": provider,
            "at": time.time(),
        }
    log.error(
        f"model_catalog.substituted provider_key={provider_key} "
        f"from={failed_model} to={replacement} — the configured model is not "
        f"served any more; set it to a current id to silence this."
    )
    return replacement


def active_substitutions() -> dict[str, dict]:
    with _sub_lock:
        return {k: dict(v) for k, v in _substitutions.items()}


def clear_substitutions() -> None:
    with _sub_lock:
        _substitutions.clear()
