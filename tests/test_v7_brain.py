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
