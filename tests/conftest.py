"""
tests/conftest.py — V5
Shared pytest fixtures and offline mocks for all test modules.

Why offline mocks exist:
  - redis and structlog may not be installed in CI or local dev
  - All tests must run without any external services
  - The mock implementations are behaviorally correct (thread-safe, proper return values)
  - Tests that specifically need real Redis use the 'integration' mark
    and are skipped in the default test run
"""

import os
import sys
import types
import threading
import time
import pytest
from unittest.mock import patch

# ── Ensure project root is on sys.path ────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── Redis mock ────────────────────────────────────────────────────────────────

def _build_redis_mock():
    redis_mod = types.ModuleType("redis")

    class _FakeRedisInstance:
        def __init__(self, **kw):
            self._d: dict = {}
            self._exp: dict = {}
            self._lock = threading.Lock()

        def _evict(self, key):
            exp = self._exp.get(key)
            if exp and time.time() > exp:
                self._d.pop(key, None)
                self._exp.pop(key, None)

        def ping(self): return True

        def get(self, key):
            with self._lock:
                self._evict(key)
                return self._d.get(key)

        def set(self, key, value, ex=None, nx=False):
            with self._lock:
                self._evict(key)
                if nx and key in self._d:
                    return None
                self._d[key] = str(value)
                if ex:
                    self._exp[key] = time.time() + ex
                return True

        def incr(self, key):
            with self._lock:
                v = int(self._d.get(key, 0)) + 1
                self._d[key] = str(v)
                return v

        def incrby(self, key, amount):
            with self._lock:
                v = int(self._d.get(key, 0)) + amount
                self._d[key] = str(v)
                return v

        def expire(self, key, seconds):
            with self._lock:
                if key in self._d:
                    self._exp[key] = time.time() + seconds

        def delete(self, *keys):
            with self._lock:
                for k in keys:
                    self._d.pop(k, None)
                    self._exp.pop(k, None)

        def exists(self, key):
            with self._lock:
                self._evict(key)
                return 1 if key in self._d else 0

        def lpush(self, key, *values):
            with self._lock:
                lst = self._d.get(key, [])
                if not isinstance(lst, list):
                    lst = []
                for v in values:
                    lst.insert(0, str(v))
                self._d[key] = lst
                return len(lst)

        def lrange(self, key, start, end):
            with self._lock:
                lst = self._d.get(key, [])
                if not isinstance(lst, list):
                    return []
                return lst[start:] if end == -1 else lst[start:end + 1]

        def ltrim(self, key, start, end):
            with self._lock:
                lst = self._d.get(key, [])
                if isinstance(lst, list):
                    self._d[key] = lst[start:end + 1]

        def llen(self, key):
            with self._lock:
                return len(self._d.get(key, []))

        def hset(self, key, mapping=None, **kwargs):
            with self._lock:
                h = self._d.get(key, {})
                if not isinstance(h, dict):
                    h = {}
                if mapping:
                    h.update(mapping)
                h.update(kwargs)
                self._d[key] = h

        def hget(self, key, field):
            with self._lock:
                h = self._d.get(key, {})
                return h.get(field) if isinstance(h, dict) else None

        def hgetall(self, key):
            with self._lock:
                h = self._d.get(key, {})
                return h if isinstance(h, dict) else {}

    class _CP:
        @classmethod
        def from_url(cls, *a, **kw):
            return cls()

    class MockRedis(_FakeRedisInstance):
        def __init__(self, *a, **kw):
            super().__init__(**kw)

    redis_mod.Redis = MockRedis
    redis_mod.ConnectionPool = _CP
    redis_mod.RedisError = Exception
    redis_mod.exceptions = types.SimpleNamespace(
        ConnectionError=ConnectionError,
        TimeoutError=TimeoutError,
    )
    return redis_mod


# ── structlog mock ─────────────────────────────────────────────────────────────

def _build_structlog_mock():
    sl = types.ModuleType("structlog")

    class _NullLogger:
        def bind(self, **kw): return self
        def info(self, *a, **kw): pass
        def warning(self, *a, **kw): pass
        def warn(self, *a, **kw): pass
        def error(self, *a, **kw): pass
        def debug(self, *a, **kw): pass
        def critical(self, *a, **kw): pass

    sl.get_logger = lambda name="": _NullLogger()
    sl.configure = lambda **kw: None
    sl.BoundLogger = _NullLogger
    sl.stdlib = types.SimpleNamespace(
        filter_by_level=None,
        add_logger_name=None,
        add_log_level=None,
        PositionalArgumentsFormatter=lambda: None,
        render_to_log_kwargs=None,
        BoundLogger=_NullLogger,
        LoggerFactory=lambda: None,
    )
    sl.processors = types.SimpleNamespace(
        TimeStamper=lambda **kw: None,
        StackInfoRenderer=lambda: None,
        format_exc_info=None,
        UnicodeDecoder=lambda: None,
    )
    return sl


# ── Install mocks before any app imports ──────────────────────────────────────
# These are installed unconditionally — if real redis/structlog are installed,
# the mocks override them in tests so tests are always offline/deterministic.

if "redis" not in sys.modules:
    sys.modules["redis"] = _build_redis_mock()

if "structlog" not in sys.modules:
    sys.modules["structlog"] = _build_structlog_mock()


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def no_provider_catalogue_calls():
    """
    The unit suite must never ask a provider what it serves.

    OpenRouter's catalogue needs no API key, so the moment providers learned to
    repair a retired model id, any test feeding a 404 would reach out to
    openrouter.ai for real. It did: CI went red on a test that passes locally
    only because the sandbox blocks that host — a test whose result depends on
    the network is not a test.

    Off by default here, so a catalogue is something a test opts INTO by
    patching this function (see tests/test_model_catalog.py). Substitutions are
    process-global, so they are cleared too: one test healing a model must not
    change the model the next test sees.
    """
    import app.ai.model_catalog as mc

    mc.clear_cache()
    mc.clear_substitutions()
    with patch.object(mc, "available_models", lambda *a, **k: []):
        yield
    mc.clear_cache()
    mc.clear_substitutions()


@pytest.fixture(autouse=True)
def clean_redis_singleton():
    """Reset Redis singleton before every test for isolation."""
    import app.core.redis_client as rc
    rc.reset_client()
    yield
    rc.reset_client()


@pytest.fixture
def fake_redis():
    """Provide a fresh FakeRedis instance with REDIS_URL unset."""
    import app.core.redis_client as rc
    rc.reset_client()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("REDIS_URL", "")
        r = rc.get_redis()
    yield r
    rc.reset_client()


@pytest.fixture(autouse=True)
def env_defaults(monkeypatch):
    """Set safe defaults for all required env vars in tests."""
    defaults = {
        "GITHUB_WEBHOOK_SECRET": "test-webhook-secret-32chars-long!!",
        "GITHUB_APP_ID": "12345",
        "GITHUB_PRIVATE_KEY": (
            "-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----"
        ),
        "GROQ_API_KEY": "test_groq_key_not_real",
        "REDIS_URL": "",
        "MCP_API_KEY": "test-mcp-api-key-xyz",
        # Set because CI sets it. Without it the auth-gated endpoints were OPEN
        # locally and CLOSED in CI, so a test could assert on an unauthenticated
        # 400 here and get a 401 there — which is exactly what happened, and the
        # kind of divergence that is only ever found by a red build.
        "METRICS_AUTH_TOKEN": "test_metrics_token",
        "LOG_LEVEL": "WARNING",
        "LOG_FORMAT": "text",
    }
    for k, v in defaults.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def mock_github_client(monkeypatch):
    """Mock gh_get and gh_post to prevent real GitHub API calls."""
    from unittest.mock import MagicMock

    mock_get = MagicMock(return_value={})
    mock_post = MagicMock(return_value={})

    monkeypatch.setattr("app.github.client.gh_get", mock_get)
    monkeypatch.setattr("app.github.client.gh_post", mock_post)
    return {"get": mock_get, "post": mock_post}
