"""tests/test_memory.py — per-repo private memory + privacy guard + encrypted backup."""

import pytest

from app.intelligence import memory


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    # Isolate each test's Redis state and env.
    from app.core import redis_client

    redis_client.reset_client()
    for var in ("LLM_LOCAL_ONLY", "LLM_PREFER_LOCAL", "MEMORY_ALLOW_CLOUD", "MEMORY_BACKUP_KEY"):
        monkeypatch.delenv(var, raising=False)
    yield
    redis_client.reset_client()


# ── Store & recall ────────────────────────────────────────────────────────────


class TestRememberRecall:
    def test_remember_and_count(self):
        assert memory.remember("o/r", "we use pytest fixtures for redis", kind="pattern")
        assert memory.count("o/r") == 1

    def test_dedup_identical_text(self):
        memory.remember("o/r", "same fact")
        memory.remember("o/r", "same fact")
        assert memory.count("o/r") == 1

    def test_empty_text_rejected(self):
        assert memory.remember("o/r", "   ") is False
        assert memory.count("o/r") == 0

    def test_invalid_kind_falls_back_to_fact(self):
        memory.remember("o/r", "a decision about caching", kind="bogus")
        items = memory.recall("o/r", "caching decision")
        assert items and items[0].kind == "fact"

    def test_recall_ranks_by_relevance(self):
        memory.remember("o/r", "authentication uses JWT tokens with RS256")
        memory.remember("o/r", "the frontend is built with React and Vite")
        items = memory.recall("o/r", "how does JWT auth work", top_k=2)
        assert items
        assert "JWT" in items[0].text  # most relevant first

    def test_recall_empty_store(self):
        assert memory.recall("o/r", "anything") == []

    def test_max_items_cap(self, monkeypatch):
        monkeypatch.setattr(memory, "MEMORY_MAX_ITEMS", 3)
        for i in range(6):
            memory.remember("o/r", f"fact number {i}")
        assert memory.count("o/r") <= 3

    def test_known_repos_indexes(self):
        memory.remember("o/a", "x fact one")
        memory.remember("o/b", "y fact two")
        repos = memory.known_repos()
        assert "o/a" in repos and "o/b" in repos


# ── Privacy guard (the load-bearing rule) ─────────────────────────────────────


class TestPrivacyGuard:
    def test_no_injection_by_default(self):
        memory.remember("o/r", "secret internal architecture detail")
        # Default (cloud) mode → memory must NOT be injected into the prompt.
        assert memory.recall_context("o/r", "architecture") == ""

    def test_injected_in_local_only(self, monkeypatch):
        monkeypatch.setenv("LLM_LOCAL_ONLY", "1")
        memory.remember("o/r", "architecture uses hexagonal ports and adapters")
        ctx = memory.recall_context("o/r", "architecture")
        assert "hexagonal" in ctx

    def test_injected_when_prefer_local(self, monkeypatch):
        monkeypatch.setenv("LLM_PREFER_LOCAL", "1")
        memory.remember("o/r", "db is postgres with pgbouncer")
        assert "postgres" in memory.recall_context("o/r", "postgres pgbouncer setup")

    def test_explicit_cloud_optin(self, monkeypatch):
        monkeypatch.setenv("MEMORY_ALLOW_CLOUD", "1")
        memory.remember("o/r", "we deploy on render free tier")
        assert "render" in memory.recall_context("o/r", "deploy")

    def test_injection_allowed_flag(self, monkeypatch):
        assert memory.injection_allowed() is False
        monkeypatch.setenv("LLM_LOCAL_ONLY", "1")
        assert memory.injection_allowed() is True


# ── Encrypted backup ──────────────────────────────────────────────────────────


class TestEncryptedBackup:
    def test_roundtrip_preserves_memory(self, monkeypatch):
        from app.core import memory_backup

        monkeypatch.setenv("MEMORY_BACKUP_KEY", memory_backup.generate_key())
        memory.remember("o/r", "fix: null check in parser resolved crash", kind="fix")
        memory.remember("o/r", "decision: use redis lists for the queue", kind="decision")

        blob = memory_backup.export_encrypted(["o/r"])
        assert blob is not None

        memory.clear("o/r")
        assert memory.count("o/r") == 0

        restored = memory_backup.import_encrypted(blob)
        assert restored == 1
        assert memory.count("o/r") == 2
        items = memory.recall("o/r", "queue redis decision")
        assert any("redis lists" in it.text for it in items)

    def test_ciphertext_contains_no_plaintext(self, monkeypatch):
        from app.core import memory_backup

        monkeypatch.setenv("MEMORY_BACKUP_KEY", memory_backup.generate_key())
        secret = "TOPSECRET_internal_token_layout"
        memory.remember("o/r", secret)
        blob = memory_backup.export_encrypted(["o/r"])
        assert secret.encode() not in blob  # cloud never sees plaintext

    def test_wrong_key_fails_authentication(self, monkeypatch):
        from cryptography.fernet import Fernet, InvalidToken
        from app.core import memory_backup

        monkeypatch.setenv("MEMORY_BACKUP_KEY", memory_backup.generate_key())
        memory.remember("o/r", "some memory")
        blob = memory_backup.export_encrypted(["o/r"])

        monkeypatch.setenv("MEMORY_BACKUP_KEY", Fernet.generate_key().decode())
        with pytest.raises(InvalidToken):
            memory_backup.import_encrypted(blob)

    def test_export_none_when_unconfigured(self):
        from app.core import memory_backup

        assert memory_backup.is_configured() is False
        assert memory_backup.export_encrypted(["o/r"]) is None

    def test_backup_to_github_pushes_ciphertext(self, monkeypatch):
        from unittest.mock import MagicMock
        from app.core import memory_backup

        monkeypatch.setenv("MEMORY_BACKUP_KEY", memory_backup.generate_key())
        memory.remember("o/r", "backup me")

        gh_get = MagicMock(side_effect=Exception("404 not found"))  # no existing file
        gh_put = MagicMock(return_value={})
        monkeypatch.setattr("app.github.client.gh_get", gh_get)
        monkeypatch.setattr("app.github.client.gh_put", gh_put)

        ok = memory_backup.backup_to_github("o/private-backup", "mem.bin", "tok", ["o/r"])
        assert ok is True
        assert gh_put.called
        sent = gh_put.call_args[0][2]
        assert "content" in sent and sent["content"]  # base64 ciphertext
