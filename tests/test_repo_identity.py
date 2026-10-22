"""
Round-3 audit C9: per-repository state is keyed by name, and a name can be
reused by a different repository (deleted or renamed, then recreated — by a
different owner if the account name was re-registered). The new repository
inherited the old one's memory, rollback snapshots and alert issue number.
"""

from unittest.mock import patch

from app.core import repo_identity


def _seed(r, repo):
    from app.intelligence import memory

    memory.remember(repo, "the old owner's internal decision", kind="decision")
    r.set(f"secret_alert:{repo}", "17")
    r.set(f"snapshot:{repo}:5", "{}")
    r.set(f"learn:{repo}:autofix_merged", "3")
    r.set(f"pr_opened:{repo}#4", "1")
    r.set("secret_alert:o/r-other", "99")  # a different repository


def test_same_id_keeps_state(fake_redis):
    _seed(fake_redis, "o/r")
    assert repo_identity.ensure("o/r", 111) is False
    assert repo_identity.ensure("o/r", 111) is False
    assert fake_redis.get("secret_alert:o/r") in ("17", b"17")


def test_a_reused_name_starts_empty(fake_redis):
    from app.intelligence import memory

    repo_identity.ensure("o/r", 111)
    _seed(fake_redis, "o/r")
    assert repo_identity.ensure("o/r", 222) is True
    for key in ("secret_alert:o/r", "snapshot:o/r:5", "learn:o/r:autofix_merged", "pr_opened:o/r#4"):
        assert fake_redis.get(key) is None, key
    assert memory.recall_context("o/r", "internal decision") == ""
    assert fake_redis.get("secret_alert:o/r-other") in ("99", b"99"), "only that name is cleared"


def test_the_first_sighting_clears_nothing(fake_redis):
    _seed(fake_redis, "o/r")
    assert repo_identity.ensure("o/r", 111) is False
    assert fake_redis.get("snapshot:o/r:5") is not None


def test_the_webhook_path_runs_the_check(fake_redis):
    import server

    repo_identity.ensure("o/r", 111)
    _seed(fake_redis, "o/r")
    payload = {"action": "opened", "repository": {"full_name": "o/r", "id": 222}, "installation": {"id": 1}}
    with patch("app.handlers.issues.handle"):
        server._run_handler("issues", payload, "o/r")
    assert fake_redis.get("secret_alert:o/r") is None


def test_an_unreadable_store_does_not_block_the_event(fake_redis):
    import server

    payload = {"action": "opened", "repository": {"full_name": "o/r", "id": 222}, "installation": {"id": 1}}
    with patch("app.core.repo_identity.ensure", side_effect=ConnectionError("down")), \
         patch("app.handlers.issues.handle") as handle:
        server._run_handler("issues", payload, "o/r")
    handle.assert_called_once()
