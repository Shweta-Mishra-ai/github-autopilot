"""
tests/test_snapshot.py — V5
Tests for atomic snapshot bot_actions (V5 fix: lpush replaces read-modify-write).
"""

import json
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.core.redis_client as rc


def setup_function():
    rc.reset_client()


def _fake_redis():
    with patch.dict(os.environ, {"REDIS_URL": ""}):
        rc.reset_client()
        return rc.get_redis()


class TestRecordBotAction:

    def test_action_stored(self):
        r = _fake_redis()
        from app.core.snapshot import record_bot_action, _get_bot_actions
        record_bot_action("org/repo", "snap1", {"type": "autofix", "issue": 1})
        actions = _get_bot_actions(r, "org/repo", "snap1")
        assert len(actions) == 1
        assert actions[0]["type"] == "autofix"

    def test_multiple_actions_all_stored(self):
        r = _fake_redis()
        from app.core.snapshot import record_bot_action, _get_bot_actions
        for i in range(5):
            record_bot_action("org/repo", "snap2", {"type": "fix", "i": i})
        actions = _get_bot_actions(r, "org/repo", "snap2")
        assert len(actions) == 5

    def test_actions_newest_first(self):
        """lpush prepends → index 0 is the most recent action."""
        r = _fake_redis()
        from app.core.snapshot import record_bot_action, _get_bot_actions
        record_bot_action("org/repo", "snap3", {"seq": 1})
        record_bot_action("org/repo", "snap3", {"seq": 2})
        actions = _get_bot_actions(r, "org/repo", "snap3")
        assert actions[0]["seq"] == 2  # newest first
        assert actions[1]["seq"] == 1

    def test_concurrent_actions_all_recorded(self):
        """V5 FIX: concurrent writes must not lose actions (atomic lpush)."""
        import threading
        r = _fake_redis()
        from app.core.snapshot import record_bot_action, _get_bot_actions
        errors = []

        def worker(idx):
            try:
                record_bot_action("org/repo", "snap_concurrent",
                                  {"worker": idx, "type": "fix"})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        actions = _get_bot_actions(r, "org/repo", "snap_concurrent")
        assert len(actions) == 20, (
            f"Expected 20 actions from concurrent writes, got {len(actions)}. "
            "V4 non-atomic get-modify-set would lose some under concurrency."
        )

    def test_actions_different_snapshots_independent(self):
        r = _fake_redis()
        from app.core.snapshot import record_bot_action, _get_bot_actions
        record_bot_action("org/repo", "snapA", {"tag": "A"})
        record_bot_action("org/repo", "snapB", {"tag": "B"})
        a_actions = _get_bot_actions(r, "org/repo", "snapA")
        b_actions = _get_bot_actions(r, "org/repo", "snapB")
        assert len(a_actions) == 1 and a_actions[0]["tag"] == "A"
        assert len(b_actions) == 1 and b_actions[0]["tag"] == "B"

    def test_missing_snapshot_id_returns_empty(self):
        r = _fake_redis()
        from app.core.snapshot import _get_bot_actions
        actions = _get_bot_actions(r, "org/repo", "nonexistent_snap")
        assert actions == []

    def test_action_json_survives_roundtrip(self):
        r = _fake_redis()
        from app.core.snapshot import record_bot_action, _get_bot_actions
        action = {
            "type": "security_scan",
            "severity": "high",
            "files": ["app/main.py", "config.json"],
            "count": 3,
            "nested": {"key": "value"},
        }
        record_bot_action("org/repo", "snapJ", action)
        actions = _get_bot_actions(r, "org/repo", "snapJ")
        assert actions[0] == action


class TestGetSnapshot:

    def test_nonexistent_snapshot_returns_none(self):
        _fake_redis()
        from app.core.snapshot import get_snapshot
        result = get_snapshot("org/repo", "does-not-exist")
        assert result is None

    def test_snapshot_includes_bot_actions(self):
        """Snapshot dict must have bot_actions key populated from list key."""
        r = _fake_redis()
        from app.core import snapshot as snap_mod
        # Inject a fake snapshot into Redis
        snap_data = {
            "id": "abc12345",
            "repo": "org/repo",
            "trigger": "test",
            "timestamp": "2025-01-01T00:00:00+00:00",
            "timestamp_ts": 1735689600,
            "state": {
                "open_issues_count": 2,
                "open_prs_count": 1,
                "open_issues": [],
                "open_prs": [],
                "latest_commit": "deadbeef1234",
                "default_branch": "main",
                "stars": 0,
            },
        }
        r.set("snapshot:org/repo:abc12345", json.dumps(snap_data))
        r.lpush("snapshot_actions:org/repo:abc12345",
                json.dumps({"type": "label_added"}))

        with patch("app.core.snapshot.get_redis", return_value=r):
            result = snap_mod.get_snapshot("org/repo", "abc12345")

        assert result is not None
        assert "bot_actions" in result
        assert len(result["bot_actions"]) == 1
        assert result["bot_actions"][0]["type"] == "label_added"


class TestListSnapshots:

    def test_empty_repo_returns_empty_list(self):
        _fake_redis()
        from app.core.snapshot import list_snapshots
        with patch("app.core.snapshot.get_redis", return_value=rc.get_redis()):
            result = list_snapshots("org/empty-repo")
        assert result == []

    def test_format_snapshot_list_empty(self):
        _fake_redis()
        from app.core.snapshot import format_snapshot_list
        with patch("app.core.snapshot.get_redis", return_value=rc.get_redis()):
            result = format_snapshot_list("org/empty")
        assert "No Snapshots" in result
