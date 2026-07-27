"""
tests/test_v7_brain.py — V7 Phase 3.

The brain shipped with no write path (nothing in the application ever called
remember(); only the backup module touched the store) and a read path that
returned "" unless a local-model env var was set. It could neither learn nor
recall in any standard deployment. These tests pin both ends down.
"""

import pytest
from unittest.mock import MagicMock, patch

from app.core.redis_client import _FakeRedis
from app.intelligence import memory


@pytest.fixture
def store():
    """
    A private in-memory store for each test.

    Injected explicitly rather than relying on the real get_redis(): other
    modules in the suite set FLASK_ENV=production, which makes get_redis()
    raise when REDIS_URL is unset, and memory swallows that — leaving tests
    silently operating on nothing.
    """
    fake = _FakeRedis()
    with patch("app.core.redis_client.get_redis", return_value=fake):
        yield fake


class TestRecallOnByDefault:
    def test_enabled_without_any_env_var(self, monkeypatch):
        for var in ("LLM_LOCAL_ONLY", "LLM_PREFER_LOCAL", "MEMORY_ALLOW_CLOUD"):
            monkeypatch.delenv(var, raising=False)
        assert memory.injection_allowed() is True

    def test_explicit_opt_out_disables(self, monkeypatch):
        monkeypatch.setenv("MEMORY_ALLOW_CLOUD", "0")
        assert memory.injection_allowed() is False

    def test_false_and_no_also_opt_out(self, monkeypatch):
        for val in ("false", "no", "FALSE"):
            monkeypatch.setenv("MEMORY_ALLOW_CLOUD", val)
            assert memory.injection_allowed() is False

    def test_local_only_still_enabled(self, monkeypatch):
        monkeypatch.delenv("MEMORY_ALLOW_CLOUD", raising=False)
        monkeypatch.setenv("LLM_LOCAL_ONLY", "1")
        assert memory.injection_allowed() is True


class TestMemoryWrites:
    def test_secrets_never_reach_the_store(self, store):
        memory.remember("o/r", "deploy key is AKIAIOSFODNN7REALKEY do not share", kind="fact")
        stored = " ".join(i.text for i in memory.recall("o/r", "deploy key"))
        assert "AKIAIOSFODNN7REALKEY" not in stored

    def test_code_bodies_are_not_stored(self, store):
        memory.remember(
            "o/r",
            "accepted fix for login\n```python\ndef proprietary(): ...\n```",
            kind="fix",
        )
        stored = " ".join(i.text for i in memory.recall("o/r", "accepted fix login"))
        assert "proprietary" not in stored
        assert "accepted fix" in stored

    def test_duplicate_text_is_not_stored_twice(self, store):
        assert memory.remember("o/r", "the session handler rejects expired tokens") is True
        assert memory.remember("o/r", "the session handler rejects expired tokens") is False

    def test_clear_also_drops_the_dedup_index(self, store):
        """Clearing only the list would make a re-store silently no-op."""
        assert memory.remember("o/r", "the session handler rejects expired tokens") is True
        memory.clear("o/r")
        assert memory.remember("o/r", "the session handler rejects expired tokens") is True

    def test_recall_scan_is_bounded(self):
        fake = MagicMock()
        fake.lrange.return_value = []
        with (
            patch.object(memory, "MEMORY_RECALL_SCAN", 10),
            patch("app.core.redis_client.get_redis", return_value=fake),
        ):
            memory.recall("o/r", "anything")
        assert fake.lrange.call_args[0][2] == 9  # 0..MEMORY_RECALL_SCAN-1


class TestWriteSitesAreWired:
    def test_merge_of_bot_branch_records_a_memory(self):
        from app.handlers.comments import publisher

        with (
            patch.object(
                publisher,
                "gh_get",
                return_value={"head": {"sha": "s", "ref": "fix/bot-issue-7"}, "base": {"ref": "main"}},
            ),
            patch.object(publisher, "gh_put", return_value={"merged": True, "sha": "abc123"}),
            patch.object(publisher, "gh_delete"),
            patch("app.core.guardrails.check_pr_auto_merge", return_value=MagicMock(passed=True)),
            patch("app.intelligence.memory.remember") as remember,
        ):
            publisher.cmd_merge(
                "o/r", 9, {"pull_request": {}, "title": "fix null deref"}, "tok", "dev", MagicMock()
            )
        remember.assert_called()

    def test_apply_records_a_memory(self):
        from app.handlers.comments import publisher

        def _gh_get(path, _token):
            if path == "/repos/o/r":
                return {"default_branch": "main"}
            if "/branches/" in path:
                return {"name": "fix/bot-issue-7"}
            return []

        with (
            patch.object(publisher, "gh_get", side_effect=_gh_get),
            patch.object(publisher, "gh_post", return_value={"number": 5, "html_url": "u", "title": "t"}),
            patch("app.intelligence.memory.remember") as remember,
        ):
            publisher.cmd_apply("o/r", 7, "tok", "fix/bot-issue-7")
        remember.assert_called()

    def test_memory_failure_never_breaks_the_command(self):
        """Memory is an enhancement — a write failure must not fail /merge."""
        from app.handlers.comments import publisher

        with (
            patch.object(
                publisher,
                "gh_get",
                return_value={"head": {"sha": "s", "ref": "fix/bot-issue-7"}, "base": {"ref": "main"}},
            ),
            patch.object(publisher, "gh_put", return_value={"merged": True, "sha": "abc123"}),
            patch.object(publisher, "gh_delete"),
            patch("app.core.guardrails.check_pr_auto_merge", return_value=MagicMock(passed=True)),
            patch("app.intelligence.memory.remember", side_effect=Exception("redis gone")),
        ):
            out = publisher.cmd_merge(
                "o/r", 9, {"pull_request": {}, "title": "t"}, "tok", "dev", MagicMock()
            )
        assert "Merged" in out


# ── Confidence ────────────────────────────────────────────────────────────────

from app.ai.hallucination import HallucinationResult  # noqa: E402
from app.core.confidence import ConfidenceGate, compute_confidence  # noqa: E402


class TestComputedConfidence:
    def test_self_reported_confidence_cannot_carry_a_bad_payload(self):
        """A hallucinating model happily reports 0.99."""
        payload = {"confidence": 0.99, "summary": "", "issues": []}
        score = compute_confidence(
            payload,
            hallucination=HallucinationResult(confidence=0.1, is_acceptable=False),
            anchor_rate=0.0,
            required_fields=("summary",),
        )
        assert score < 0.5

    def test_strong_evidence_scores_high(self):
        payload = {
            "confidence": 0.8,
            "summary": "clear and specific assessment of the change",
            "issues": [{"severity": "major"}],
        }
        score = compute_confidence(
            payload,
            hallucination=HallucinationResult(confidence=0.95, is_acceptable=True),
            anchor_rate=1.0,
            required_fields=("summary",),
        )
        assert score > 0.8

    def test_degraded_payload_scores_zero(self):
        assert compute_confidence({"_degraded": True}) == 0.0

    def test_non_dict_scores_zero(self):
        assert compute_confidence("not a dict") == 0.0

    def test_missing_signals_are_dropped_not_penalised(self):
        """A caller that can't supply anchor_rate must not be punished for it."""
        payload = {"confidence": 0.9, "summary": "a clear and specific assessment"}
        with_signal = compute_confidence(
            payload,
            hallucination=HallucinationResult(confidence=0.9, is_acceptable=True),
            required_fields=("summary",),
        )
        assert with_signal > 0.85

    def test_non_numeric_reported_confidence_does_not_crash(self):
        assert 0.0 <= compute_confidence({"confidence": "high", "summary": "x"}) <= 1.0

    def test_gate_uses_computed_not_reported(self):
        gate = ConfidenceGate(None)
        out = gate.evaluate(
            "code_review",
            {"confidence": 0.99, "summary": "", "issues": []},
            hallucination=HallucinationResult(confidence=0.1, is_acceptable=False),
            anchor_rate=0.0,
        )
        assert out["auto_apply"] is False
        assert out["confidence_score"] < 0.99

    def test_gate_without_signals_still_works(self):
        """Existing callers pass no signals — they must not break."""
        gate = ConfidenceGate(None)
        out = gate.evaluate("pr_title_rewrite", {"confidence": 0.9, "suggested_title": "feat: x"})
        assert "confidence_score" in out
        assert isinstance(out["auto_apply"], bool)


class TestGateIsWiredIntoReview:
    def test_review_code_actually_calls_the_gate(self):
        """The gate was passed to _review_code and never used."""
        from app.handlers import pull_request as pr_mod

        files = [{"filename": "app/a.py", "patch": "@@ -1,1 +1,1 @@\n-x = 0\n+x = 1\n"}]
        payload = {
            "files": [{"file": "app/a.py", "score": 7, "issues": [], "summary": "looks fine"}]
        }
        cfg = MagicMock()
        cfg.footer = ""
        cfg.get.return_value = 4
        gate = MagicMock()
        gate.evaluate.return_value = {"auto_apply": True, "confidence_score": 0.9}

        with patch.object(pr_mod.router, "ask", return_value=(payload, MagicMock())):
            pr_mod._review_code(
                {"head": {"sha": "s"}}, "o/r", 1, files, "t", cfg, gate, "", MagicMock()
            )

        gate.evaluate.assert_called()
        assert gate.evaluate.call_args[0][0] == "code_review"
