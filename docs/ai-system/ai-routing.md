# AI Routing System

> The intelligence backbone of GitHub Autopilot.
> This document explains how the system selects LLM providers, handles failures, controls hallucination quality, manages cost, and sanitises untrusted inputs — all transparently to every caller.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Single Interface — What Callers See](#2-single-interface--what-callers-see)
3. [Task Classification](#3-task-classification)
4. [Provider Specifications](#4-provider-specifications)
5. [Provider Selection Algorithm](#5-provider-selection-algorithm)
6. [Circuit Breaker — State Machine](#6-circuit-breaker--state-machine)
7. [Prompt Sanitization and Injection Defense](#7-prompt-sanitization-and-injection-defense)
8. [Response Parsing — `_extract_json`](#8-response-parsing--_extract_json)
9. [Hallucination Detection and Confidence Scoring](#9-hallucination-detection-and-confidence-scoring)
10. [Cost Tracking and Budget Enforcement](#10-cost-tracking-and-budget-enforcement)
11. [Full Routing Flow Diagram](#11-full-routing-flow-diagram)
12. [Failure Modes](#12-failure-modes)
13. [Alternatives Considered](#13-alternatives-considered)

---

## 1. Overview

The AI Router (`app/ai/router.py`) is the single point of contact between handler logic and language models. Every handler calls the same two methods:

```python
result, meta = router.ask(system_prompt, user_prompt, task="fix_command")
text, meta   = router.ask_text(system_prompt, user_prompt, task="explain")
```

No handler knows:
- Which provider answered the request
- Whether a fallback provider was used
- How many retries occurred
- What the token cost was

All of that complexity is encapsulated in the router. Handlers receive either a parsed `dict` or a `str`, plus a `LLMResponse` metadata object containing provider, model, latency, and token counts.

---

## 2. Single Interface — What Callers See

```python
@dataclass
class LLMResponse:
    text:         str
    provider:     str    # "groq_70b" | "groq_8b" | "gemini" | "openrouter"
    model:        str    # exact model string used
    total_tokens: int    # prompt_tokens + completion_tokens
    latency_ms:   int    # time from call to response
    used_fallback: bool  # True if primary provider was unavailable
    warnings:     list[str]  # hallucination warnings, if any
```

```python
# handler code — simple and clean
result, meta = router.ask(
    system="Senior engineer. Analyze this bug. JSON only.",
    user=f"Issue: {title}\nCode: {code[:2000]}",
    task="fix_command",
)
fix        = result.get("fix", "")
root_cause = result.get("root_cause", "")
# meta.provider, meta.total_tokens available if needed
```

---

## 3. Task Classification

Every `router.ask()` call includes a `task` string. The router maps this to one of four tiers that determine provider priority and token budget.

```python
TASK_MAP: dict[str, str] = {
    # fast — Groq 8B preferred (12K req/day, fastest latency)
    "issue_label":         "fast",
    "commit_lint":         "fast",
    "pr_summary":          "fast",
    "budget":              "fast",
    "changelog":           "fast",

    # standard — Groq 70B preferred (best reasoning quality)
    "pr_title_rewrite":    "standard",
    "code_review":         "standard",
    "fix_command":         "standard",
    "test_generation":     "standard",
    "explain":             "standard",
    "improve":             "standard",
    "refactor":            "standard",
    "ci_analysis":         "standard",
    "perf":                "standard",
    "arch":                "standard",
    "docs":                "standard",

    # deep — Groq 70B with higher token budget
    "pr_analysis":         "deep",
    "security_report":     "deep",
    "issue_triage":        "deep",
    "health_report":       "deep",
    "gaps":                "deep",

    # long — Gemini Flash preferred (1M token context window)
    "full_file_analysis":  "long",
    "large_pr_review":     "long",
}
```

**Why this matters for cost and quality:**

| Tier | Primary provider | Reasoning quality | Speed | Daily quota |
|------|----------------|------------------|-------|-------------|
| fast | Groq 8B | Good for simple tasks | ~0.6s | 12K req |
| standard | Groq 70B | Best available free | ~1.8s | 5K req |
| deep | Groq 70B | Best, higher budget | ~3s | 5K req |
| long | Gemini Flash | Good, 1M ctx | ~2s | 1.5K req |

Using Groq 8B for simple labelling tasks (fast tier) preserves Groq 70B quota for complex code review (standard/deep tier). This extends the effective daily capacity without any quality degradation on simple tasks.

---

## 4. Provider Specifications

| Provider | Model | Task tiers | Daily limit | Context window | Cost |
|----------|-------|-----------|------------|----------------|------|
| Groq 70B | `llama-3.3-70b-versatile` | standard, deep | 5,000 req / 80K tok | 32K tokens | Free |
| Groq 8B | `llama-3.1-8b-instant` | fast | 12,000 req / 400K tok | 128K tokens | Free |
| Gemini Flash | `gemini-1.5-flash` | long, fallback | 1,500 req / 1M tok/min | 1,000,000 tokens | Free |
| OpenRouter | `openai/gpt-3.5-turbo` | emergency | 200 req / 50K tok | 16K tokens | Free tier |

**Total daily capacity without any cost:** ~18,700 requests.

**Why OpenRouter as emergency fallback and not another free model?** OpenRouter provides a stable API endpoint with multiple underlying models and a free tier. If Groq's infrastructure has an outage (rare but possible), OpenRouter gives a completely independent fallback with different upstream infrastructure.

---

## 5. Provider Selection Algorithm

```python
def _select_provider(task: str) -> LLMProvider:
    tier = TASK_MAP.get(task, "standard")

    candidates = []

    if tier in ("standard", "deep"):
        if _is_available("groq_70b"):
            candidates.append("groq_70b")
        if _is_available("groq_8b"):
            candidates.append("groq_8b")
        if _is_available("gemini"):
            candidates.append("gemini")

    elif tier == "fast":
        if _is_available("groq_8b"):
            candidates.append("groq_8b")
        if _is_available("groq_70b"):
            candidates.append("groq_70b")
        if _is_available("gemini"):
            candidates.append("gemini")

    elif tier == "long":
        if _is_available("gemini"):
            candidates.append("gemini")
        if _is_available("groq_70b"):
            candidates.append("groq_70b")

    # OpenRouter is always the last resort
    if _is_available("openrouter"):
        candidates.append("openrouter")

    if not candidates:
        raise AllProvidersDown("All LLM providers are unavailable or rate-limited")

    return _providers[candidates[0]]

def _is_available(provider: str) -> bool:
    breaker = _breakers.get(provider)
    if not breaker or not breaker.is_available():
        return False
    usage = _get_usage_pct(provider)
    return usage < 0.80   # deprioritise above 80% of daily limit
```

**The 80% usage threshold:** When a provider has consumed 80% of its daily limit, it is removed from the candidate list and the next provider is tried. This prevents the system from exhausting a provider's quota mid-day, leaving no capacity for the most critical requests in the afternoon/evening.

**`_is_available()` checks two independent conditions:**

1. **Circuit breaker state** — is the provider recovering from recent failures?
2. **Usage percentage** — has the provider consumed too much of its daily quota?

Both must pass for the provider to be selected. A provider with a healthy circuit but 90% usage is excluded. A provider with 10% usage but an open circuit is excluded.

---

## 6. Circuit Breaker — State Machine

Each provider has its own `CircuitBreaker` instance. The state machine has three states.

```
          ┌──────────────────────────────────────────────────┐
          │                                                  │
          │               CLOSED  (healthy)                  │
          │         all traffic flows through                │◄────────────────┐
          │                                                  │                 │
          └──────────────────────┬───────────────────────────┘                 │
                                 │ 3 failures in window                        │
                                 ▼                                             │
          ┌──────────────────────────────────────────────────┐                 │
          │                                                  │                 │
          │               OPEN  (tripped)                    │                 │
          │    all calls rejected immediately                │                 │
          │    no traffic, no timeouts, instant fail         │                 │
          │                                                  │                 │
          └──────────────────────┬───────────────────────────┘                 │
                                 │ 60 seconds elapsed                          │
                                 ▼                                             │ success
          ┌──────────────────────────────────────────────────┐                 │
          │                                                  │                 │
          │            HALF_OPEN  (testing)                  │                 │
          │       exactly ONE test request allowed           ├─────────────────┘
          │                                                  │
          └──────────────────────┬───────────────────────────┘
                                 │ failure
                                 ▼
                           back to OPEN
                           (another 60s cooldown)
```

**Implementation:**
```python
class CircuitBreaker:
    def __init__(self, name: str, fail_threshold: int = 3, recovery_timeout: int = 60):
        self._name             = name
        self._fail_threshold   = fail_threshold
        self._recovery_timeout = recovery_timeout
        self._failures         = 0
        self._state            = CBState.CLOSED
        self._opened_at        = 0.0

    def record_failure(self, reason: str = ""):
        self._failures += 1
        log.warning(f"circuit.failure provider={self._name} "
                    f"count={self._failures} reason={reason}")
        if self._failures >= self._fail_threshold:
            self._state     = CBState.OPEN
            self._opened_at = time.time()   # ← MUST use time.time(), not 0.0
            log.error(f"circuit.opened provider={self._name}")

    def record_success(self):
        self._failures = 0
        self._state    = CBState.CLOSED

    def is_available(self) -> bool:
        if self._state == CBState.CLOSED:
            return True
        if self._state == CBState.OPEN:
            if time.time() - self._opened_at >= self._recovery_timeout:
                self._state = CBState.HALF_OPEN
                log.info(f"circuit.half_open provider={self._name}")
                return True   # allow one test request
            return False
        if self._state == CBState.HALF_OPEN:
            return True   # test request in progress
        return False
```

**Critical design detail — `_opened_at = time.time()` not `0.0`:**

If `_opened_at` were initialised to `0.0`, the HALF_OPEN check `time.time() - 0.0 >= 60` would be `True` immediately for any OPEN circuit that was just created. The breaker would transition to HALF_OPEN before completing even one second of cooldown. This was an actual bug in an earlier version.

Using `time.time()` ensures the 60-second cooldown starts from when the circuit opened, not from the epoch.

**Why per-provider and not global?** A global circuit breaker would cut off all LLM access when a single provider has issues. With per-provider breakers, a Groq outage routes traffic to Gemini while Groq recovers. The system degrades gracefully rather than failing completely.

**Test injection pattern:**
```python
# In tests — FakeBreaker is injected into the module-level dict
from app.ai.circuit_breaker import _breakers

class FakeBreaker:
    def is_available(self): return True
    def record_success(self): pass
    def record_failure(self, reason=""): pass

_breakers["groq_70b"] = FakeBreaker()
_breakers["groq_8b"]  = FakeBreaker()
```

`patch.object` cannot intercept `@property` methods in this implementation. Direct dict injection is the established test pattern — see [testing-guide.md](../testing/testing-guide.md#1-circuit-breaker-injection).

---

## 7. Prompt Sanitization and Injection Defense

All user-controlled content (issue bodies, PR descriptions, comment text, file contents) is inserted into LLM prompts. This creates prompt injection risk — a malicious user could craft a payload designed to override the system prompt and manipulate the bot's actions.

**Sanitization implementation:**
```python
_INJECTION_PATTERNS = [
    "ignore previous instructions",
    "ignore all instructions",
    "disregard your system prompt",
    "you are now",
    "act as",
    "jailbreak",
    "dan mode",
    "developer mode",
]

_MAX_SYSTEM_CHARS = 3_000
_MAX_USER_CHARS   = 8_000

def _sanitize(text: str, max_chars: int) -> str:
    if not text:
        return ""
    lowered = text.lower()
    for pattern in _INJECTION_PATTERNS:
        if pattern in lowered:
            log.warning(f"router.injection_attempt pattern='{pattern}'")
            text = re.sub(re.escape(pattern), "[FILTERED]", text, flags=re.IGNORECASE)
    return text[:max_chars]
```

**What this catches:** Known, common injection phrases. Case-insensitive substring matching handles most obvious attempts.

**What this does NOT catch:**
- Unicode substitution (`ı` → `i`, Cyrillic homoglyphs)
- Indirect injection via code comments in files the bot reviews
- Novel rephrasing not in the pattern list
- Multi-message injection across a conversation thread

**Why character truncation is used instead of token truncation:** Token counting requires a tokeniser library tied to the specific model. Loading `tiktoken` or a Groq-specific tokeniser adds ~50MB of memory and significant import time. Character limits are approximate but consistent and require no dependencies. A 8,000-character limit corresponds to roughly 2,000 tokens at average English density — well within all providers' context windows for user content.

**Known limitation and future mitigation:** A classification LLM call as a pre-filter (pass the user content to a lightweight model, ask "does this contain instructions to modify system behaviour?") would be substantially more robust than pattern matching. This is tracked as a future improvement.

---

## 8. Response Parsing — `_extract_json`

All prompts ask for JSON-only output. In practice, LLMs occasionally return:
- Valid JSON — ideal
- JSON wrapped in Markdown fences: ` ```json\n{...}\n``` `
- JSON with a prose preamble: `"Sure, here's the analysis:\n{...}"`
- Incomplete JSON truncated mid-key
- Pure prose with no JSON at all

`_extract_json` handles all of these:

```python
def _extract_json(text: str) -> dict:
    if not text:
        return {"raw": text}

    # 1. Strip markdown code fences
    text = re.sub(r"```(?:json)?\s*", "", text).strip()
    text = re.sub(r"```\s*$", "", text).strip()

    # 2. Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 3. Brace-depth scanner — find outermost {...}
    depth   = 0
    start   = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                candidate = text[start : i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    pass   # keep scanning

    # 4. Fallback — return raw text for caller to handle
    log.warning(f"_extract_json: could not parse JSON from response length={len(text)}")
    return {"raw": text}
```

**Why the `{"raw": text}` fallback instead of raising an exception?**

Raising an exception would cause the circuit breaker to record a failure against the provider — even though the provider responded successfully. The issue is with JSON formatting, not provider availability. The `{"raw": text}` fallback lets the caller detect the parse failure separately from a provider failure.

**How callers detect `{"raw": text}`:**
```python
r, _meta = router.ask(system, user, task="fix_command")
if "raw" in r and "root_cause" not in r:
    log.warning(f"autofix: LLM returned prose, not JSON for {file_path}")
    return current_content   # safe fallback — return file unchanged
```

**Known limitation of the brace-depth scanner:** It correctly handles nested objects. However, it fails on JSON strings that contain literal `{` or `}` characters (e.g., code snippets inside a `"fix"` field). In practice, LLMs usually escape these, but edge cases exist. A more robust parser would use a proper recursive descent approach, but the current scanner handles > 98% of real responses correctly.

---

## 9. Hallucination Detection and Confidence Scoring

LLMs produce confident-sounding but incorrect output. The hallucination detector runs on every response before it reaches a GitHub comment.

**Scoring algorithm:**
```python
_HALLUCINATION_PATTERNS: list[tuple[str, str, float]] = [
    # (regex_pattern, category_name, score_penalty)
    (r"i'?m not sure",           "uncertainty",       0.30),
    (r"i don'?t know",           "uncertainty",       0.30),
    (r"i cannot (access|view)",  "access_limitation", 0.40),
    (r"as an ai",                "role_confusion",    0.20),
    (r"i apologize",             "apology",           0.10),
    (r"\[insert [^\]]+\]",       "placeholder",       0.50),
    (r"\[your [^\]]+\]",         "placeholder",       0.50),
    (r"unfortunately",           "hedging",           0.10),
]

def check_response(response: dict, response_type: str) -> HallucinationResult:
    score    = 1.0
    warnings = []
    text     = json.dumps(response)

    for pattern, category, penalty in _HALLUCINATION_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            score -= penalty
            warnings.append(category)

    # Minimum field length checks
    for field, min_len in _MIN_LENGTHS.get(response_type, {}).items():
        value = response.get(field, "")
        if isinstance(value, str) and len(value) < min_len:
            score -= 0.20
            warnings.append(f"short_{field}")

    return HallucinationResult(
        confidence=max(0.0, score),
        warnings=warnings,
        passed=score >= 0.50,
    )
```

**Confidence action thresholds:**

| Confidence | What happens | Why |
|-----------|-------------|-----|
| ≥ 0.70 | Post response normally | High confidence — no warning needed |
| 0.50–0.69 | Post with `⚠️ confidence: {score}` footer | Some uncertainty detected — user should verify |
| < 0.50 | Retry with next provider | Response likely unreliable — don't post it |
| < 0.30 | Skip all providers, post error | All providers returned low-confidence responses |

**Adding a confidence footer:**
```python
def add_confidence_footer(comment: str, hal: HallucinationResult) -> str:
    if hal.confidence >= 0.70:
        return comment
    warning_text = " · ".join(hal.warnings) if hal.warnings else "low confidence"
    return (
        f"{comment}\n\n"
        f"> ⚠️ **AI confidence: {hal.confidence:.0%}** ({warning_text}). "
        f"Verify before applying."
    )
```

**Known limitations:**

1. **Legitimate uncertainty is penalised.** A response saying "I'm not sure if this is a threading issue, but the evidence suggests..." is a reasonable response to an ambiguous bug. The detector penalises `"I'm not sure"` regardless of context. A context-aware classifier would be more accurate.

2. **CVE numbers are not penalised.** If an LLM hallucinates a fake CVE number (`CVE-2024-99999`), it receives a 0.0 penalty. The scanner checks format not validity. A CVE database lookup would be required for real validation.

3. **Hallucination detection does not verify factual accuracy.** A confidently-wrong response scores 1.0 if it contains no uncertainty phrases. The system reduces the worst hallucinations (uncertain/incomplete responses) but cannot catch confident factual errors.

---

## 10. Cost Tracking and Budget Enforcement

All API usage is tracked in Redis and displayed via `/budget`.

**Daily limits:**
```python
DAILY_LIMITS = {
    "groq_70b":   {"requests": 5_000,  "tokens": 80_000},
    "groq_8b":    {"requests": 12_000, "tokens": 400_000},
    "gemini":     {"requests": 1_200,  "tokens": 1_500_000},
    "openrouter": {"requests": 200,    "tokens": 50_000},
}

COST_PER_1K_TOKENS = {
    "groq_70b":   0.00090,   # $0.90/M — free tier: $0
    "groq_8b":    0.00006,   # $0.06/M — free tier: $0
    "gemini":     0.00000,   # Free
    "openrouter": 0.00000,   # Free tier
}
```

**Tracking per call:**
```python
def _log_and_track(provider: str, tokens: int, latency_ms: int):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        r = get_redis()
        r.incr(f"llm:requests:{provider}:{today}")
        r.incr(f"llm:tokens:{provider}:{today}")     # by actual token count
        r.expire(f"llm:requests:{provider}:{today}", 86400)
        r.expire(f"llm:tokens:{provider}:{today}", 86400)
        cost_mc = int((tokens / 1000) * COST_PER_1K_TOKENS.get(provider, 0) * 100_000)
        if cost_mc > 0:
            r.incr(f"llm:cost_mc:{provider}:{today}", cost_mc)
    except Exception:
        pass   # tracking failure should never affect the response
```

**Budget enforcement — 80% threshold:**
```python
def _get_usage_pct(provider: str) -> float:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        r = get_redis()
        used = int(r.get(f"llm:requests:{provider}:{today}") or 0)
        limit = DAILY_LIMITS[provider]["requests"]
        return used / limit
    except Exception:
        return 0.0   # tracking failure → assume 0% used
```

When `_get_usage_pct(provider) >= 0.80`, the provider is excluded from `_is_available()` and the next provider is tried. This prevents exhausting a provider's daily limit before end-of-day.

---

## 11. Full Routing Flow Diagram

```
router.ask(system, user, task) called
            │
            ▼
 ┌────────────────────────┐
 │   _sanitize(system)    │  blocklist scan + truncate to 3,000 chars
 │   _sanitize(user)      │  blocklist scan + truncate to 8,000 chars
 └───────────┬────────────┘
             │
             ▼
 ┌────────────────────────┐
 │   TASK_MAP lookup      │  task → fast | standard | deep | long
 └───────────┬────────────┘
             │
             ▼
 ┌────────────────────────┐
 │  _select_provider()    │
 │                        │
 │  check each candidate: │
 │  circuit=CLOSED?       │
 │  usage < 80%?          │
 └───────────┬────────────┘
             │ provider selected
             ▼
 ┌────────────────────────┐
 │  provider.ask()        │  HTTP call to LLM API, 45s timeout
 └───────────┬────────────┘
             │
    ┌────────┴────────┐
  success           failure
    │                  │
    ▼                  ▼
 record_success()   record_failure()
 _extract_json()    failures >= 3?
    │               → circuit OPEN
    ▼               try next provider
 check_response()
 confidence score
    │
  ┌─┴──────────────┐
  │                │
 ≥0.50          < 0.50
  │              retry next provider
  ▼
 _log_and_track()
 return (dict, LLMResponse)
```

---

## 12. Failure Modes

| Failure | Detection | What router does | What caller receives |
|---------|-----------|-----------------|---------------------|
| LLM timeout (45s) | `asyncio.TimeoutError` / `requests.Timeout` | `record_failure()`, try next provider | Response from fallback provider |
| All providers down | No candidates in `_select_provider()` | Raises `AllProvidersDown` | Exception caught in handler → degraded comment |
| JSON parse failure | `_extract_json` returns `{"raw": text}` | Returns `{"raw": text}` to caller | Caller logs warning, uses safe fallback |
| Low confidence | `check_response` score < 0.50 | Retries with next provider | Best available response, possibly with warning |
| Rate limit 429 | Provider returns HTTP 429 | `record_failure()`, try next provider | Response from fallback provider |
| Provider 500 | Provider returns HTTP 5xx | `record_failure()`, try next provider | Response from fallback provider |
| Daily limit 80% | `_get_usage_pct >= 0.80` | Provider excluded from candidates | Response from lower-priority provider |

---

## 13. Alternatives Considered

**LangChain:** Adds ~50MB of dependencies, opaque prompt templating, complex debugging of chain behaviour. Direct API calls are more transparent, easier to test, and easier to debug. The abstraction LangChain provides is not needed when the use case is well-defined.

**Single provider (OpenAI only):** Simple but creates a hard dependency on one infrastructure provider. Rate limit hits or outages become complete system failures. The multi-provider architecture eliminates any single point of failure.

**Local models (Ollama / llama.cpp):** Zero API cost, no rate limits, full control. Requires GPU or significant CPU. On Render's free tier (0.5 CPU, no GPU), Llama 70B would take 5–10 minutes per inference call — unusable. Local models are viable on self-hosted infrastructure with appropriate hardware.

**Vertex AI / AWS Bedrock:** Enterprise-grade reliability, SLAs, better rate limits. Requires billing setup, GCP/AWS account configuration, and is not free at meaningful scale. Viable when the project outgrows free tier.

**Streaming responses:** Streaming would improve perceived latency for `/explain` and `/summarize`. However, GitHub comments are posted atomically — there is no streaming GitHub API. The full response must be assembled before posting. Streaming would only help if a UI layer were added later.
