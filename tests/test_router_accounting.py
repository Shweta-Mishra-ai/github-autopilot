"""
AI-layer accounting defects found by the 2026-10-08 audit, each pinned here.
"""

import datetime
from unittest.mock import MagicMock, patch

from app.ai.providers.base import LLMResponse
from app.ai.router import LLMRouter, _utc_today


class _KV(dict):
    def get(self, k):
        return super().get(k)

    def set(self, k, v, **kw):
        self[k] = str(v)
        return True

    def incr(self, k):
        self[k] = str(int(super().get(k) or 0) + 1)
        return int(self[k])

    def incrby(self, k, n):
        self[k] = str(int(super().get(k) or 0) + int(n))
        return int(self[k])

    def expire(self, *a):
        return True


def _provider(key, error=None, tokens=100):
    p = MagicMock()
    p.provider_key = key
    meta = LLMResponse(text='{"ok": 1}', provider="groq" if key.startswith("groq") else key,
                       model="m", total_tokens=tokens, error=error)
    p.ask.return_value = ({"ok": 1} if not error else {"error": error}, meta)
    return p


class TestFallbackSkipsTheSlotThatFailed:
    def test_a_failed_groq_slot_is_not_called_again(self):
        r = LLMRouter()
        failed = _provider("groq_70b", error="TIMEOUT")
        backup = _provider("groq_8b")
        with patch.object(r, "_select_provider", return_value=failed), \
             patch.object(r, "_fallback_candidates", return_value=[failed, backup]), \
             patch.object(r, "_call_provider", side_effect=lambda p, *a: p.ask(*a)), \
             patch.object(r, "_log_and_track") as track, \
             patch("app.ai.router._charge"):
            r.ask("s", "u", task="explain")
        assert failed.ask.call_count == 1, "the failed slot was retried by the fallback"
        assert backup.ask.call_count == 1
        assert track.call_args[0][2] == "groq_8b", "usage is charged to the slot that answered"


class TestUsageIsCountedOnceUnderTheProvidersKey:
    def test_groq_usage_and_cost_land_on_its_slot(self, monkeypatch):
        kv = _KV()
        monkeypatch.setattr("app.core.redis_client.get_redis", lambda: kv)
        meta = LLMResponse(text="x", provider="groq", model="m", total_tokens=1000)
        LLMRouter()._log_and_track("explain", meta, "groq_70b")
        today = _utc_today()
        assert kv.get(f"llm:requests:groq_70b:{today}") == "1"
        assert kv.get(f"llm:tokens:groq_70b:{today}") == "1000"
        assert int(kv.get(f"llm:cost_mc:groq_70b:{today}") or 0) > 0, "Groq cost was always $0"
        assert kv.get(f"llm:requests:groq:{today}") is None

    def test_a_gemini_call_is_counted_once(self, monkeypatch):
        """The provider and the router both counted it; usage read double."""
        from app.ai.providers import gemini

        kv = _KV()
        monkeypatch.setattr("app.core.redis_client.get_redis", lambda: kv)
        assert not hasattr(gemini.GeminiProvider, "_track")
        meta = LLMResponse(text="x", provider="gemini", model="m", total_tokens=1000)
        LLMRouter()._log_and_track("explain", meta, "gemini")
        assert kv.get(f"llm:requests:gemini:{_utc_today()}") == "1"


class TestRoutingReadsTheTokenQuota:
    def test_tokens_near_the_limit_count_as_high_usage(self, monkeypatch):
        from app.ai.routing_policy import DAILY_LIMITS

        kv = _KV()
        monkeypatch.setattr("app.core.redis_client.get_redis", lambda: kv)
        today = _utc_today()
        kv.set(f"llm:tokens:groq_70b:{today}", DAILY_LIMITS["groq_70b"]["tokens"] - 1000)
        kv.set(f"llm:requests:groq_70b:{today}", 30)
        assert LLMRouter()._usage_pct("groq_70b") > 0.9


def test_the_counter_day_is_utc():
    assert _utc_today() == datetime.datetime.now(datetime.timezone.utc).date().isoformat()


class TestJsonExtraction:
    def test_a_brace_inside_a_string_after_a_preamble(self):
        from app.ai.providers.base import _extract_json

        text = 'Here is the review:\n{"summary": "s", "issues": [{"code": "if (x) {", "line": 3}]}\nThanks!'
        assert _extract_json(text) == {"summary": "s", "issues": [{"code": "if (x) {", "line": 3}]}

    def test_fenced_with_trailing_text(self):
        from app.ai.providers.base import _extract_json

        text = '```json\n{"a": "}"}\n```\nDone.'
        assert _extract_json(text) == {"a": "}"}

    def test_never_returns_a_non_dict(self):
        from app.ai.providers.base import _extract_json

        for text in ("null", "42", "[1, 2]", '"s"'):
            assert _extract_json(text) == {"raw": text}

    def test_the_first_object_wins(self):
        from app.ai.providers.base import _extract_json

        assert _extract_json('x {"a": 1} y {"b": 2}') == {"a": 1}
