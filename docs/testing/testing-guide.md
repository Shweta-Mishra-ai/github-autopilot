# Testing Guide

> How tests are structured, how to run them, how to write new ones,
> and the six critical patterns every contributor must know before
> touching the test suite.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Running Tests](#2-running-tests)
3. [Test File Map](#3-test-file-map)
4. [Critical Pattern 1 — Circuit Breaker Injection](#4-critical-pattern-1--circuit-breaker-injection)
5. [Critical Pattern 2 — Router Mock Returns Tuple](#5-critical-pattern-2--router-mock-returns-tuple)
6. [Critical Pattern 3 — Module Import Caching](#6-critical-pattern-3--module-import-caching)
7. [Critical Pattern 4 — Falsy Empty List in Fixtures](#7-critical-pattern-4--falsy-empty-list-in-fixtures)
8. [Critical Pattern 5 — Patch at the Source Module](#8-critical-pattern-5--patch-at-the-source-module)
9. [Critical Pattern 6 — Secret Scanner Source Safety](#9-critical-pattern-6--secret-scanner-source-safety)
10. [Writing a New Handler Test — Template](#10-writing-a-new-handler-test--template)
11. [Known Gotchas](#11-known-gotchas)
12. [Coverage Targets](#12-coverage-targets)
13. [CI Configuration](#13-ci-configuration)

---

## 1. Overview

```
Tests across 18 files (count updated automatically by CI)
Zero network calls — all GitHub API and LLM calls mocked
Zero environment variables required
Execution time: ~2 seconds
```

The test suite runs anywhere: locally, in CI, without Redis, without API
keys, without a GitHub App. Every external dependency is mocked. Tests
are fast, deterministic, and never fail due to infrastructure issues.

---

## 2. Running Tests

```bash
# Full suite — run before every commit
python -m pytest -v

# Single file
python -m pytest tests/test_push.py -v

# Single class
python -m pytest tests/test_push.py::TestScanSecrets -v

# Single test
python -m pytest tests/test_push.py::TestScanSecrets::test_dedup_suppresses_second_issue -v

# With coverage
python -m pytest --cov=app --cov-report=term-missing tests/

# HTML coverage report (open htmlcov/index.html)
python -m pytest --cov=app --cov-report=html tests/

# Lint — must match CI exactly
ruff check app/ --select E,F,W --ignore E501
```

---

## 3. Test File Map

| File | Tests | What is covered |
|------|-------|----------------|
| `test_webhook_security.py` | 35 | Signature verify, empty-secret regression, replay, rate limit, bot detection, pipeline, startup, authorization |
| `test_enhanced_secrets.py` | 26 | 10+ credential patterns, false positives, entropy, redaction, dedup |
| `test_push.py` | 25 | Secret scan dedup regression, dep scan, commit lint, skip guards |
| `test_pull_request.py` | 22 | PR routing, blast radius, code review, test gap detection |
| `test_issues.py` | 15 | Issue triage, labels, welcome comment, notifications |
| `test_ci.py` | 18 | CI failure analysis, pattern tracking, skip conditions |
| `test_autofix.py` | 15 | Fix plan, apply fix, 70% safety guard, branch creation |
| `test_router.py` | — | Provider selection, task routing, fallback chain |
| `test_hallucination.py` | — | Confidence scoring, pattern detection, thresholds |
| `test_idempotency.py` | — | Redis SET NX, in-memory fallback, fingerprinting |
| `test_guardrails.py` | — | PR merge guards, confidence thresholds |
| `test_analytics.py` | — | Record/retrieve analytics, weekly report format |
| `test_secrets.py` | — | Original secrets.py (kept alongside enhanced) |
| `test_providers.py` | — | Per-provider API call mocking, response parsing |
| `test_confidence.py` | — | Per-action confidence gates, threshold enforcement |
| `test_comments.py` | — | Slash command dispatch, permission denial, rate limit |
| `test_storage.py` | — | SQLite event log, fixture capture, replay |
| `test_validator.py` | — | JSON schema validation, type checking |

---

## 4. Critical Pattern 1 — Circuit Breaker Injection

The circuit breaker stores per-provider state in a module-level
`_breakers` dict in `app/ai/circuit_breaker.py`. Tests must inject a
`FakeBreaker` to control availability and prevent real circuit state
from leaking between tests.

**Why `patch()` does not work here:**

`patch("app.ai.circuit_breaker.groq_70b")` cannot intercept a
`@property` method that reads from the internal dict. Direct dict
injection is the only reliable approach.

```python
from app.ai.circuit_breaker import _breakers

class FakeBreaker:
    """Controls circuit breaker state in tests."""
    def is_available(self) -> bool:
        return True    # always available — no tripping in tests

    def record_success(self) -> None:
        pass           # no state change

    def record_failure(self, reason: str = "") -> None:
        pass           # no state change

# Inject before any test that calls router.ask()
_breakers["groq_70b"] = FakeBreaker()
_breakers["groq_8b"]  = FakeBreaker()
_breakers["gemini"]   = FakeBreaker()
```

**As a pytest fixture:**

```python
@pytest.fixture(autouse=True)
def mock_circuit_breakers():
    from app.ai.circuit_breaker import _breakers
    original = dict(_breakers)
    _breakers["groq_70b"] = FakeBreaker()
    _breakers["groq_8b"]  = FakeBreaker()
    _breakers["gemini"]   = FakeBreaker()
    yield
    _breakers.clear()
    _breakers.update(original)
```

**When you need this:** Any test that exercises code that eventually
calls `router.ask()` — directly or via a handler. If you see
`AllProvidersDown` raised unexpectedly in a test, you forgot to inject
`FakeBreaker`.

---

## 5. Critical Pattern 2 — Router Mock Returns Tuple

`router.ask()` **always** returns a 2-tuple: `(dict, LLMResponse)`.
Never just a dict. Every mock must return a tuple.

```python
from app.ai.providers.base import LLMResponse

def _meta(
    provider: str = "groq",
    model: str = "llama-3.3-70b",
    tokens: int = 50,
) -> LLMResponse:
    return LLMResponse(
        text="ok",
        provider=provider,
        model=model,
        total_tokens=tokens,
        latency_ms=1200,
        used_fallback=False,
        warnings=[],
    )

# CORRECT — return_value is a 2-tuple
response = {"root_cause": "missing null check", "fix": "add guard clause"}
with patch("app.handlers.comments.router.ask",
           return_value=(response, _meta())):
    ...

# WRONG — return_value is a dict
with patch("app.handlers.comments.router.ask",
           return_value=response):
    # Raises: ValueError: not enough values to unpack (expected 2, got 1)
    ...
```

**Same for `router.ask_text()`:**

```python
# ask_text returns (str, LLMResponse)
with patch("app.handlers.comments.router.ask_text",
           return_value=("Summary text here", _meta())):
    ...
```

---

## 6. Critical Pattern 3 — Module Import Caching

Python caches imports. When a test patches
`"app.handlers.ci.gh_post"` but the `ci` module was already imported
in a prior test, the patch may not intercept calls because the module's
local namespace already holds the original reference.

**Symptom:** A test passes in isolation but fails in the full suite,
because a prior test left the module in a different state.

**Fix — use `patch.object` with the already-imported module:**

```python
# UNRELIABLE when module already imported by a prior test
with patch("app.handlers.ci.gh_post") as mock_post:
    from app.handlers.ci import handle
    handle(payload)

# RELIABLE — targets the bound name in the module object directly
import app.handlers.ci as ci_mod

with patch.object(ci_mod, "gh_post") as mock_post:
    ci_mod.handle(payload)

# Multiple patches on the same module
with patch.object(ci_mod, "gh_post") as mock_post, \
     patch.object(ci_mod, "router") as mock_router, \
     patch.object(ci_mod, "get_installation_token", return_value="tok"):
    mock_router.ask.return_value = (analysis, _meta())
    ci_mod.handle(payload)
    mock_post.assert_called_once()
```

`patch.object` is always safe to use and is recommended over
string-based `patch()` for all handler tests.

---

## 7. Critical Pattern 4 — Falsy Empty List in Fixtures

Python's `or` operator treats an empty list as falsy. This causes a
common fixture bug when testing empty-list edge cases.

**Example bug caught during test writing:**

```python
# WRONG — empty list is falsy, falls through to default
def _payload(commits=None):
    return {
        "commits": commits or [{"id": "abc", "message": "feat: x"}],
    }

_payload(commits=[])
# Returns: {"commits": [{"id": "abc", "message": "feat: x"}]}
# The test for "bot handles empty commits" fails —
# because commits is NOT empty in the payload!

# CORRECT — explicit None check
def _payload(commits=None):
    return {
        "commits": (
            commits if commits is not None
            else [{"id": "abc", "message": "feat: x"}]
        ),
    }

_payload(commits=[])
# Returns: {"commits": []}  ← correct
```

**Same for any list-type fixture parameter:**

```python
# WRONG
def _payload(pr_numbers=None):
    return {
        "check_run": {
            "pull_requests": pr_numbers or [{"number": 7}]
        }
    }

_payload(pr_numbers=[])   # returns [{"number": 7}] — WRONG

# CORRECT
def _payload(pr_numbers=None):
    return {
        "check_run": {
            "pull_requests": (
                pr_numbers if pr_numbers is not None
                else [{"number": 7}]
            )
        }
    }

_payload(pr_numbers=[])   # returns [] — correct
```

**Rule:** Whenever a fixture parameter has a list default and you need
to test the empty-list case, always use `x if x is not None else
[default]` instead of `x or [default]`.

---

## 8. Critical Pattern 5 — Patch at the Source Module

When a handler uses a local import inside a function body, you must
patch at the source module, not at the handler module.

**The pattern in the codebase:**

```python
# Inside app/handlers/ci.py
def _track_failure_pattern(repo: str, check_name: str, error: str) -> bool:
    from app.core.redis_client import get_redis   # LOCAL IMPORT
    r = get_redis()
    ...
```

Local imports are used to avoid circular imports and defer loading
until the function is actually called.

**How to patch correctly:**

```python
# WRONG — patches a name that does not exist at module level in ci.py
with patch("app.handlers.ci.get_redis", return_value=fake_redis):
    _track_failure_pattern(...)
    # No effect — ci.py imports get_redis inside the function

# CORRECT — patches the function at its definition location
with patch("app.core.redis_client.get_redis", return_value=fake_redis):
    _track_failure_pattern(...)
    # Correct — every caller gets fake_redis
```

**General rule:** Always patch where the function is **defined**, not
where it is **used**. Check the import statement:

- `from app.core.redis_client import get_redis` at **module level**
  → patch `"app.handlers.ci.get_redis"`
- `from app.core.redis_client import get_redis` **inside a function**
  → patch `"app.core.redis_client.get_redis"`

---

## 9. Critical Pattern 6 — Secret Scanner Source Safety

All credential-format strings in test files must be assembled via
helper functions — **never stored as string literals**.

GitHub Secret Scanning reads source file text at rest. It matches
patterns against raw file contents. A test input string like a Stripe
live key format stored as a literal will trigger a GitHub security
alert on the test file itself — even though it is not a real key.

**Note:** A literal Stripe-format string in
`test_enhanced_secrets.py` caused GitHub to create a "Publicly leaked
secret" alert, requiring a forced commit and manual alert dismissal.

### Wrong approaches

```python
# WRONG — literal string triggers GitHub scanner
STRIPE_TEST_KEY = "sk" + "_live_" + "AbCdEfGhIjKlMnOpQrStUvWxYz1234"

# STILL WRONG — even inside a list, still a scannable literal
FALSE_POSITIVES = ["sk_live_" + "X" * 24]
# Some scanners match the prefix 'sk_live_' as a trigger regardless
```

### Correct approach — runtime assembly

```python
def _stripe_live_key() -> str:
    """
    Assembles a valid-format Stripe live key for tests.
    Not a real credential — constructed to match the pattern format only.
    Split across concatenations so no source line contains the
    full triggering prefix as a literal.
    """
    return "sk" + "_live_" + "AbCdEfGh" + "IjKlMnOpQrStUvWxYz1234"

def _github_token() -> str:
    """Valid-format GitHub classic token. Not a real token."""
    return "gh" + "p_" + "aBcDeFgHiJkLmNoPqRsTuVw" + "XyZ12345678"

def _slack_bot_token() -> str:
    """Valid-format Slack bot token. Not a real token."""
    return "xo" + "xb-12345678901-12345678901-ABCDefGhIjKlMnOpQrStUvWx"

def _sendgrid_key() -> str:
    """Valid-format SendGrid key. Not a real key."""
    part1 = "abcdefghijklmnopqrstuv"            # 22 chars
    part2 = "abcdefghijklmnopqrstuvwxyz1234567890ABCDEFG"  # 43 chars
    return "SG" + "." + part1 + "." + part2

def _anthropic_key() -> str:
    """Valid-format Anthropic key. Not a real key."""
    return "sk" + "-ant-api03-" + ("a" * 93) + "AA"
```

### In source files — `_fp()` helper

For `enhanced_secrets.py` itself (which contains the whitelist of
known-false-positive strings), use the `_fp()` function:

```python
def _fp(parts: list[str]) -> str:
    """
    Joins string fragments at runtime.
    Source file contains function calls, not assembled literals.
    GitHub scanner reads source text — it cannot match _fp([...]).
    """
    return "".join(parts)

# Source line: _fp(["sk" + "_live_", "X" * 24])
# At runtime:  the assembled Stripe placeholder (all X's)
# Scanner sees only the function call — no match
FALSE_POSITIVE_VALUES = {
    _fp(["sk" + "_live_", "X" * 24]),
    _fp(["gh" + "p_", "X" * 36]),
}
```

### Checklist before committing any test file

- [ ] No credential-format string stored as a module-level literal
- [ ] No credential-format string stored as a class-level literal
- [ ] Every test input that looks like a token is assembled by a function
- [ ] Helper functions have a docstring stating "Not a real credential"
- [ ] Prefix is split across at least 2 concatenations in the function

---

## 10. Writing a New Handler Test — Template

```python
"""
tests/test_my_handler.py
Covers: my_handler.handle() — routing, main flow, edge cases.
"""

from unittest.mock import MagicMock, patch
from app.ai.providers.base import LLMResponse


# ── Helpers ──────────────────────────────────────────────────────────────────

def _meta(tokens: int = 50) -> LLMResponse:
    """Minimal LLMResponse for router.ask() mock return values."""
    return LLMResponse(
        text="ok",
        provider="groq",
        model="llama-3.3-70b",
        total_tokens=tokens,
        latency_ms=1200,
        used_fallback=False,
        warnings=[],
    )


def _payload(
    action: str = "opened",
    author: str = "shweta",
    number: int = 1,
    installation_id: int = 42,
) -> dict:
    """Minimal GitHub webhook payload."""
    return {
        "action": action,
        "issue": {
            "number": number,
            "title": "Test issue",
            "body": "Issue description",
            "user": {"login": author},
            "labels": [],
        },
        "repository": {"full_name": "org/repo"},
        "installation": {"id": installation_id},
    }


def _mock_config(enabled: bool = True) -> MagicMock:
    """Minimal Config mock with sensible defaults."""
    cfg = MagicMock()
    cfg.issues_enabled.return_value = enabled
    cfg.get.side_effect = lambda *args, **kw: kw.get("default", True)
    cfg.footer = ""
    return cfg


# ── Skip condition tests ──────────────────────────────────────────────────────

class TestSkipConditions:
    """Every handler must silently skip certain events."""

    def test_bot_author_skipped(self):
        with patch("app.handlers.my_handler.get_installation_token") as mock_tok:
            from app.handlers.my_handler import handle
            handle(_payload(author="dependabot[bot]"))
            mock_tok.assert_not_called()

    def test_wrong_action_skipped(self):
        with patch("app.handlers.my_handler.get_installation_token") as mock_tok:
            from app.handlers.my_handler import handle
            handle(_payload(action="closed"))
            mock_tok.assert_not_called()

    def test_auth_failure_returns_early(self):
        with patch("app.handlers.my_handler.get_installation_token",
                   side_effect=Exception("auth failed")), \
             patch("app.handlers.my_handler.router.ask") as mock_ask:
            from app.handlers.my_handler import handle
            handle(_payload())
            mock_ask.assert_not_called()


# ── Main flow tests ───────────────────────────────────────────────────────────

class TestMainFlow:

    def test_success_posts_comment(self):
        llm_response = {"summary": "Good issue", "priority": "high"}
        import app.handlers.my_handler as handler_mod

        with patch.object(handler_mod, "get_installation_token",
                          return_value="tok"), \
             patch.object(handler_mod, "load_config",
                          return_value=_mock_config()), \
             patch.object(handler_mod, "gh_get", return_value={}), \
             patch.object(handler_mod, "router") as mock_router, \
             patch.object(handler_mod, "gh_post") as mock_post:

            mock_router.ask.return_value = (llm_response, _meta())
            handler_mod.handle(_payload())

            mock_post.assert_called_once()
            url = mock_post.call_args[0][0]
            assert "/issues/1/comments" in url

    def test_comment_contains_priority(self):
        llm_response = {"summary": "Critical bug", "priority": "critical"}
        import app.handlers.my_handler as handler_mod

        with patch.object(handler_mod, "get_installation_token",
                          return_value="tok"), \
             patch.object(handler_mod, "load_config",
                          return_value=_mock_config()), \
             patch.object(handler_mod, "gh_get", return_value={}), \
             patch.object(handler_mod, "router") as mock_router, \
             patch.object(handler_mod, "gh_post") as mock_post:

            mock_router.ask.return_value = (llm_response, _meta())
            handler_mod.handle(_payload())

            body = mock_post.call_args[0][2]["body"]
            assert "critical" in body.lower() or "Critical" in body

    def test_llm_failure_propagates(self):
        """LLM exceptions propagate to the dispatch layer."""
        import app.handlers.my_handler as handler_mod
        import pytest

        with patch.object(handler_mod, "get_installation_token",
                          return_value="tok"), \
             patch.object(handler_mod, "load_config",
                          return_value=_mock_config()), \
             patch.object(handler_mod, "gh_get", return_value={}), \
             patch.object(handler_mod, "router") as mock_router:

            mock_router.ask.side_effect = Exception("LLM timeout")

            with pytest.raises(Exception, match="LLM timeout"):
                handler_mod.handle(_payload())


# ── Feature flag tests ────────────────────────────────────────────────────────

class TestFeatureFlags:

    def test_disabled_skips_ai_call(self):
        import app.handlers.my_handler as handler_mod

        with patch.object(handler_mod, "get_installation_token",
                          return_value="tok"), \
             patch.object(handler_mod, "load_config",
                          return_value=_mock_config(enabled=False)), \
             patch.object(handler_mod, "router") as mock_router:

            handler_mod.handle(_payload())
            mock_router.ask.assert_not_called()
```

---

## 11. Known Gotchas

### `format_budget_comment` patches at wrong location

```python
# WRONG — function not in comments namespace
with patch("app.handlers.comments.format_budget_comment",
           return_value="## Budget"):

# CORRECT — patch at definition site
with patch("app.ai.metrics.format_budget_comment",
           return_value="## Budget"):
```

### `_extract_json` returns `{"raw": text}` not `None` on failure

```python
r, meta = router.ask(...)

# WRONG — r is never None
if r is None:
    handle_failure()

# CORRECT — check for the raw key
if "raw" in r and "expected_field" not in r:
    # LLM returned prose instead of JSON
    handle_failure()
```

### `router.ask` always returns a 2-tuple

See [Pattern 2](#5-critical-pattern-2--router-mock-returns-tuple).
Every mock must use `return_value=(dict, LLMResponse)`.

### SQLite cleanup on Windows raises `PermissionError`

```python
import gc

def teardown_method(self):
    try:
        gc.collect()        # force GC to release SQLite file handles
        os.remove(db_path)
    except PermissionError:
        pass   # Windows GC timing — file still open briefly
```

### Redis mock values are strings, not bytes

The Redis client uses `decode_responses=True`. All keys and values
are Python strings. Mock accordingly:

```python
fake_redis = MagicMock()
fake_redis.get.return_value = "5"     # str, not b"5"
fake_redis.incr.return_value = 1      # int — INCR returns integer
fake_redis.set.return_value = True    # SET returns True on success
```

### Secret scanner tests — no literal credential strings

See [Pattern 6](#9-critical-pattern-6--secret-scanner-source-safety).
Every credential-like test input must be assembled by a helper
function. A literal Stripe-format string in a
test file caused a GitHub security alert that required a forced commit
and manual dismissal to resolve.

---

## 12. Coverage Targets

Run: `python -m pytest --cov=app --cov-report=term-missing tests/`

| Module | Current | Target |
|--------|---------|--------|
| `app/handlers/comments.py` | ~55% | 70% |
| `app/handlers/pull_request.py` | ~60% | 70% |
| `app/handlers/issues.py` | ~55% | 65% |
| `app/handlers/push.py` | ~60% | 70% |
| `app/handlers/ci.py` | ~60% | 70% |
| `app/handlers/autofix.py` | ~65% | 75% |
| `app/ai/router.py` | ~70% | 80% |
| `app/ai/circuit_breaker.py` | ~85% | 90% |
| `app/ai/hallucination.py` | ~80% | 85% |
| `app/core/idempotency.py` | ~90% | 90% |
| `app/core/config.py` | ~75% | 80% |
| `app/security/enhanced_secrets.py` | ~80% | 85% |
| `server.py` | ~40% | 60% |

---

## 13. CI Configuration

```yaml
# .github/workflows/ci.yml

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v4
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt
      - run: python -m pytest tests/ -q
        # Zero network calls, runs in < 30s

  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install ruff
      - run: |
          ruff check app/ \
            --select E,F,W \
            --ignore E501 \
            --output-format=github
```

**Rules enforced:**
- `E` — pycodestyle errors (syntax, spacing, indentation)
- `F` — pyflakes (undefined names, unused imports, unused variables)
- `W` — pycodestyle warnings
- `E501` ignored — line length not enforced

**Reproduce CI locally:**

```bash
python -m pytest tests/ -q
pip install ruff
ruff check app/ --select E,F,W --ignore E501
```

**Common CI failures and fixes:**

| Error | Cause | Fix |
|-------|-------|-----|
| `F401 imported but unused` | Import added but never used | Remove the import |
| `F841 local variable assigned but never used` | Variable assigned, never read | Remove the assignment or use `_` |
| `E711 comparison to None` | `x == None` used | Change to `x is None` |
| `W291 trailing whitespace` | Editor leaving spaces at line end | Configure editor to trim on save |
| Test `FAILED` in suite but passes alone | Module import caching | Switch to `patch.object` with module reference |
| `AllProvidersDown` in handler test | Circuit breaker not mocked | Inject `FakeBreaker` into `_breakers` dict |
