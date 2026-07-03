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


# ── 5. Rate-limit fail-open observability ──────────────────────────────────────

def test_rate_limit_failopen_increments_metric(monkeypatch):
    from app.handlers.comments import dispatcher
    from app.core.metrics import metrics

    def _boom(*a, **kw):
        raise RuntimeError("redis down")

    monkeypatch.setattr("app.core.redis_client.get_redis", _boom)

    before = metrics.get("ratelimit.failopen")
    allowed = dispatcher.check_user_rate_limit("o/r", "alice")
    after = metrics.get("ratelimit.failopen")

    assert allowed is True                 # still fail-open (bot usable)
    assert after == before + 1             # but now observable


# ── 6. server._authorized constant-time bearer ─────────────────────────────────

def _req(auth_value):
    return types.SimpleNamespace(headers={"Authorization": auth_value})


def test_authorized_open_when_no_token(monkeypatch):
    import importlib
    import server
    monkeypatch.setattr(server, "METRICS_TOKEN", "")
    assert server._authorized(_req("")) is True


def test_authorized_requires_exact_bearer(monkeypatch):
    import server
    monkeypatch.setattr(server, "METRICS_TOKEN", "s3cret")
    assert server._authorized(_req("Bearer s3cret")) is True
    assert server._authorized(_req("Bearer wrong")) is False
    assert server._authorized(_req("")) is False
