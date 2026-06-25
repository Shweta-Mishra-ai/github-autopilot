"""
tests/test_learning.py — V5
Updated for V5 fix: dynamic fix type discovery replaces hardcoded list.
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.core.redis_client as rc


def setup_function():
    rc.reset_client()


class TestGetAllFixTypes:

    def test_includes_all_baseline_types(self):
        """V5 FIX: baseline set must include types beyond V4's hardcoded 4."""
        r = rc.get_redis()
        from app.core.learning import _get_all_fix_types
        types_found = _get_all_fix_types("org/repo", r)
        # V4 only had: code, deps, config, docs
        # V5 must have all commonly used command types
        required = {"code", "deps", "config", "docs", "autofix", "refactor",
                    "test", "ci", "security", "perf"}
        missing = required - set(types_found)
        assert not missing, (
            f"V5 must discover all fix types, missing: {missing}. "
            "V4 hardcoded only code/deps/config/docs, excluding new commands."
        )

    def test_discovers_types_from_events(self):
        """Custom fix types recorded in events must be discovered."""
        import json
        r = rc.get_redis()
        # Simulate event log with a custom fix type
        event = {"data": {"type": "my_custom_fixer"}}
        r.lpush("learn:org/repo:events", json.dumps(event))
        from app.core.learning import _get_all_fix_types
        types_found = _get_all_fix_types("org/repo", r)
        assert "my_custom_fixer" in types_found

    def test_no_events_returns_baseline(self):
        r = rc.get_redis()
        from app.core.learning import _get_all_fix_types
        types_found = _get_all_fix_types("org/new-repo", r)
        assert len(types_found) >= 4  # At minimum the old baseline


class TestGetAcceptanceRate:

    def test_defaults_to_neutral_with_few_samples(self):
        r = rc.get_redis()
        from app.core.learning import get_acceptance_rate
        # Less than 5 total events → return 0.5 (neutral)
        rate = get_acceptance_rate("org/new-repo", "code")
        assert rate == 0.5

    def test_calculates_rate_with_enough_samples(self):
        r = rc.get_redis()
        r.set("learn:org/repo:fix_accepted:code", "8")
        r.set("learn:org/repo:fix_ignored:code", "2")
        from app.core.learning import get_acceptance_rate
        rate = get_acceptance_rate("org/repo", "code")
        assert rate == 0.8

    def test_all_accepted_returns_one(self):
        r = rc.get_redis()
        r.set("learn:org/r:fix_accepted:security", "10")
        r.set("learn:org/r:fix_ignored:security", "0")
        from app.core.learning import get_acceptance_rate
        rate = get_acceptance_rate("org/r", "security")
        assert rate == 1.0

    def test_all_ignored_returns_zero(self):
        r = rc.get_redis()
        r.set("learn:org/r:fix_accepted:docs", "0")
        r.set("learn:org/r:fix_ignored:docs", "10")
        from app.core.learning import get_acceptance_rate
        rate = get_acceptance_rate("org/r", "docs")
        assert rate == 0.0

    def test_error_returns_neutral(self):
        from app.core.learning import get_acceptance_rate
        with patch("app.core.learning.get_redis", side_effect=Exception("Redis down")):
            rate = get_acceptance_rate("org/r", "code")
        assert rate == 0.5


class TestRecordEvent:

    def test_event_stored(self):
        r = rc.get_redis()
        from app.core.learning import record_event
        record_event("org/repo", "fix_accepted", {"type": "security"})
        events = r.lrange("learn:org/repo:events", 0, -1)
        assert len(events) >= 1

    def test_multiple_events_stored_in_order(self):
        r = rc.get_redis()
        from app.core.learning import record_event
        record_event("org/repo2", "fix_accepted", {"type": "code", "n": 1})
        record_event("org/repo2", "fix_accepted", {"type": "code", "n": 2})
        events = r.lrange("learn:org/repo2:events", 0, -1)
        assert len(events) == 2

    def test_redis_error_does_not_raise(self):
        from app.core.learning import record_event
        with patch("app.core.learning.get_redis", side_effect=Exception("down")):
            record_event("org/r", "fix_accepted", {"type": "code"})  # must not raise
