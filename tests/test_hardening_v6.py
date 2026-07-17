"""
tests/test_hardening_v6.py

Covers the v6 hardening pass:
  1. Version single-source-of-truth (no 4.2.0/5.0.0 drift).
  2. MCP endpoint fails CLOSED when MCP_API_KEY is unset, constant-time auth.
  3. MCP installation-id allowlist (MCP_ALLOWED_INSTALLATIONS).
  4. Config deep-merge no longer leaks nested mutations into DEFAULTS.
  5. Per-user rate-limit fail-open is observable (metric increments).
  6. server._authorized constant-time bearer behaviour.
"""

import types

import pytest


# ── 1. Version SSOT ────────────────────────────────────────────────────────────

def test_version_single_source():
    import app
    import server
    from app.mcp import mcp_server

    assert app.__version__ == server.VERSION
    resp, status = mcp_server.handle_mcp_request(
        "initialize", {}, mcp_server._mcp_api_key()
    )
    assert status == 200
    assert resp["serverInfo"]["version"] == app.__version__


def test_no_stale_version_literal():
    import pathlib
    src = pathlib.Path(mcp_source_path())
    text = src.read_text(encoding="utf-8")
    assert "4.2.0" not in text, "stale hardcoded MCP version still present"


def mcp_source_path():
    from app.mcp import mcp_server
    return mcp_server.__file__


# ── 2. MCP fail-closed + constant-time auth ────────────────────────────────────

def test_mcp_fails_closed_when_key_unset(monkeypatch):
    from app.mcp import mcp_server
    monkeypatch.setenv("MCP_API_KEY", "")
    resp, status = mcp_server.handle_mcp_request("initialize", {}, "")
    assert status == 503
    assert "not configured" in resp["error"]["message"].lower()


def test_mcp_rejects_wrong_token(monkeypatch):
    from app.mcp import mcp_server
    monkeypatch.setenv("MCP_API_KEY", "real-key-123")
    resp, status = mcp_server.handle_mcp_request("initialize", {}, "wrong-key")
    assert status == 401


def test_mcp_accepts_correct_token(monkeypatch):
    from app.mcp import mcp_server
    monkeypatch.setenv("MCP_API_KEY", "real-key-123")
    resp, status = mcp_server.handle_mcp_request("tools/list", {}, "real-key-123")
    assert status == 200
    assert "tools" in resp


# ── 3. Installation allowlist ──────────────────────────────────────────────────

def test_installation_allowed_when_unset(monkeypatch):
    from app.mcp import mcp_server
    monkeypatch.delenv("MCP_ALLOWED_INSTALLATIONS", raising=False)
    assert mcp_server._installation_allowed(12345) is True


def test_installation_allowlist_enforced(monkeypatch):
    from app.mcp import mcp_server
    monkeypatch.setenv("MCP_ALLOWED_INSTALLATIONS", "111, 222")
    assert mcp_server._installation_allowed(111) is True
    assert mcp_server._installation_allowed("222") is True
    assert mcp_server._installation_allowed(999) is False


def test_run_command_blocks_disallowed_installation(monkeypatch):
    from app.mcp import mcp_server
    monkeypatch.setenv("MCP_ALLOWED_INSTALLATIONS", "111")
    out = mcp_server._handle_run_command(
        {
            "repo": "o/r",
            "issue_number": 1,
            "command": "/fix",
            "installation_id": 999,
        }
    )
    assert "not permitted" in out.lower()


# ── 4. Config deep-merge isolation ─────────────────────────────────────────────

def test_deep_merge_does_not_mutate_defaults():
    from app.core.config import Config, DEFAULTS

    before = DEFAULTS["pull_requests"]["max_files_reviewed"]
    # Override a nested value for one "tenant"
    Config({"pull_requests": {"max_files_reviewed": 2}})
    after = DEFAULTS["pull_requests"]["max_files_reviewed"]
    assert before == after, "override leaked into shared DEFAULTS"


def test_two_configs_are_independent():
    from app.core.config import Config

    a = Config({"pull_requests": {"max_files_reviewed": 3}})
    b = Config({})
    assert a.get("pull_requests", "max_files_reviewed") == 3
    # b must still see the default, not a's override
    assert b.get("pull_requests", "max_files_reviewed") == 6


# ── 5. Rate limit still ENFORCED when Redis is down (was fail-open until V6.2) ─

def test_rate_limit_redis_down_enforced_locally(monkeypatch):
    from app.handlers.comments import dispatcher
    from app.handlers.comments.constants import USER_CMD_LIMIT
    from app.core.metrics import metrics

    def _boom(*a, **kw):
        raise RuntimeError("redis down")

    monkeypatch.setattr("app.core.redis_client.get_redis", _boom)
    dispatcher._local_cmd_counts.clear()

    before = metrics.get("ratelimit.redis_fallback")
    # Within the limit: allowed (bot stays usable during a Redis outage)
    for _ in range(USER_CMD_LIMIT):
        assert dispatcher.check_user_rate_limit("o/r", "alice") is True
    # Over the limit: DENIED — the outage no longer disables the guard
    assert dispatcher.check_user_rate_limit("o/r", "alice") is False
    # And the fallback path is observable
    assert metrics.get("ratelimit.redis_fallback") == before + USER_CMD_LIMIT + 1
    # Other users are unaffected by alice's window
    assert dispatcher.check_user_rate_limit("o/r", "bob") is True
    dispatcher._local_cmd_counts.clear()


# ── 5b. Redis memory watermark (V6.2) ──────────────────────────────────────────

def test_redis_memory_status_warn_at_watermark(monkeypatch):
    from unittest.mock import MagicMock
    from app.core import redis_client

    fake = MagicMock()
    fake.info.return_value = {"used_memory": 22 * 1024 * 1024, "maxmemory": 25 * 1024 * 1024}
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)

    status = redis_client.redis_memory_status()
    assert status["level"] == "warn"          # 88% > 80% threshold
    assert status["used_pct"] > 80


def test_redis_memory_status_ok_below_watermark(monkeypatch):
    from unittest.mock import MagicMock
    from app.core import redis_client

    fake = MagicMock()
    fake.info.return_value = {"used_memory": 5 * 1024 * 1024, "maxmemory": 25 * 1024 * 1024}
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)

    assert redis_client.redis_memory_status()["level"] == "ok"


def test_redis_memory_status_unknown_on_fake_or_error(monkeypatch):
    from app.core import redis_client

    monkeypatch.setattr(redis_client, "get_redis", redis_client._FakeRedis)
    assert redis_client.redis_memory_status()["level"] == "unknown"

    def _boom():
        raise RuntimeError("down")
    monkeypatch.setattr(redis_client, "get_redis", _boom)
    assert redis_client.redis_memory_status()["level"] == "unknown"


# ── 5c. Quality floor + model disclosure (V6.2) ────────────────────────────────

def test_quality_floor_refuses_basic_tier(monkeypatch):
    """LLM_QUALITY_FLOOR=high + only 8B available → AllProvidersDown, not a
    silent 8B code review."""
    from app.ai.router import LLMRouter
    from app.ai.circuit_breaker import AllProvidersDown

    monkeypatch.setenv("LLM_QUALITY_FLOOR", "high")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    r = LLMRouter()
    # 70B breaker open (down), 8B available
    monkeypatch.setattr(
        "app.ai.router.get_breaker",
        lambda key: types.SimpleNamespace(is_available=lambda: key == "groq_8b"),
    )
    monkeypatch.setattr(r, "_usage_pct", lambda k: 0.0)

    with pytest.raises(AllProvidersDown):
        r._select_provider("code_review")


def test_quality_floor_off_still_degrades(monkeypatch):
    """Default (no floor): 8B fallback still allowed — behaviour unchanged."""
    from app.ai.router import LLMRouter

    monkeypatch.delenv("LLM_QUALITY_FLOOR", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    r = LLMRouter()
    monkeypatch.setattr(
        "app.ai.router.get_breaker",
        lambda key: types.SimpleNamespace(is_available=lambda: key == "groq_8b"),
    )
    monkeypatch.setattr(r, "_usage_pct", lambda k: 0.0)

    assert r._select_provider("code_review").provider_key == "groq_8b"


def test_quality_floor_does_not_affect_fast_tasks(monkeypatch):
    from app.ai.router import LLMRouter

    monkeypatch.setenv("LLM_QUALITY_FLOOR", "high")
    r = LLMRouter()
    monkeypatch.setattr(
        "app.ai.router.get_breaker",
        lambda key: types.SimpleNamespace(is_available=lambda: key == "groq_8b"),
    )
    monkeypatch.setattr(r, "_usage_pct", lambda k: 0.0)

    assert r._select_provider("issue_label").provider_key == "groq_8b"


def test_fallback_candidates_respect_floor(monkeypatch):
    from app.ai.router import LLMRouter

    monkeypatch.setenv("LLM_QUALITY_FLOOR", "high")
    r = LLMRouter()
    keys = [p.provider_key for p in r._fallback_candidates("code_review") if p is not None]
    assert "groq_8b" not in keys
    assert "openrouter" not in keys
    assert "groq_70b" in keys

    monkeypatch.delenv("LLM_QUALITY_FLOOR", raising=False)
    keys = [p.provider_key for p in r._fallback_candidates("code_review") if p is not None]
    assert "groq_8b" in keys


def test_model_disclosure_roundtrip():
    from app.ai import router as router_mod

    router_mod.reset_last_call()
    assert router_mod.last_model_disclosure() == ""

    router_mod._last_call.provider = "groq_70b"
    router_mod._last_call.model = "llama-3.3-70b-versatile"
    assert "llama-3.3-70b-versatile" in router_mod.last_model_disclosure()

    router_mod.reset_last_call()
    assert router_mod.last_model_disclosure() == ""


# ── 6. server._authorized constant-time bearer ─────────────────────────────────

def _req(auth_value):
    return types.SimpleNamespace(headers={"Authorization": auth_value})


def test_authorized_open_when_no_token(monkeypatch):
    import server
    monkeypatch.setattr(server, "METRICS_TOKEN", "")
    assert server._authorized(_req("")) is True


def test_authorized_requires_exact_bearer(monkeypatch):
    import server
    monkeypatch.setattr(server, "METRICS_TOKEN", "s3cret")
    assert server._authorized(_req("Bearer s3cret")) is True
    assert server._authorized(_req("Bearer wrong")) is False
    assert server._authorized(_req("")) is False
