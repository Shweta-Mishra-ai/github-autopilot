"""
tests/test_idempotency.py — V5
Updated for V5 fixes:
  - Fingerprint is now 32 hex chars (was 16) — collision resistance improved
  - TTL is now 86400s / 24h (was 3600s / 1h) — matches GitHub retry window
  - All patch targets verified to use app.core.redis_client.is_redis_available
"""

import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.core.idempotency as idem_module
from app.core.idempotency import make_fingerprint, is_duplicate, _TTL_SECONDS


def setup_function():
    idem_module._seen_local.clear()


class TestConstants:

    def test_ttl_is_24_hours(self):
        """V5 FIX: TTL must be 86400s (24h) to match GitHub's retry window."""
        assert _TTL_SECONDS == 86400, (
            f"TTL must be 86400s (24h) to match GitHub webhook retry window, got {_TTL_SECONDS}. "
            "V4 used 3600s (1h) which allowed duplicate processing after TTL expiry."
        )

    def test_fingerprint_length_is_32(self):
        """V5 FIX: Fingerprint must be 32 hex chars (128 bits) not 16 (64 bits)."""
        fp = make_fingerprint("delivery-abc", "push", {})
        assert len(fp) == 32, (
            f"Fingerprint must be 32 chars for collision resistance, got {len(fp)}. "
            "V4 used 16 chars (64 bits) which has ~1% birthday collision at 4B events."
        )


class TestMakeFingerprint:

    def test_same_inputs_same_fingerprint(self):
        payload = {"action": "opened", "number": 42}
        assert make_fingerprint("del-1", "pull_request", payload) == \
               make_fingerprint("del-1", "pull_request", payload)

    def test_different_delivery_id_different_fingerprint(self):
        payload = {"action": "opened"}
        assert make_fingerprint("del-A", "push", payload) != \
               make_fingerprint("del-B", "push", payload)

    def test_different_event_type_different_fingerprint(self):
        payload = {"action": "opened"}
        assert make_fingerprint("del-1", "pull_request", payload) != \
               make_fingerprint("del-1", "issues", payload)

    def test_different_action_different_fingerprint(self):
        fp1 = make_fingerprint("del-1", "issues", {"action": "opened"})
        fp2 = make_fingerprint("del-1", "issues", {"action": "closed"})
        assert fp1 != fp2

    def test_fingerprint_is_hex_string(self):
        fp = make_fingerprint("del-abc", "push", {})
        assert all(c in "0123456789abcdef" for c in fp)

    def test_deterministic_across_calls(self):
        payload = {"action": "opened", "repository": {"full_name": "org/repo"}}
        results = [make_fingerprint("del-xyz", "pull_request", payload) for _ in range(5)]
        assert len(set(results)) == 1

    def test_empty_payload_no_crash(self):
        fp = make_fingerprint("", "", {})
        assert isinstance(fp, str) and len(fp) == 32

    def test_pr_payload(self):
        payload = {"action": "opened", "pull_request": {"number": 5},
                   "repository": {"full_name": "user/repo"}}
        fp = make_fingerprint("abc-delivery", "pull_request", payload)
        assert len(fp) == 32

    def test_issue_payload(self):
        payload = {"action": "opened", "issue": {"number": 3},
                   "repository": {"full_name": "user/repo"}}
        fp = make_fingerprint("xyz-delivery", "issues", payload)
        assert len(fp) == 32

    def test_pr_and_issue_same_number_different_fingerprints(self):
        fp_pr = make_fingerprint("del-1", "pull_request", {"action": "opened", "number": 1})
        fp_is = make_fingerprint("del-1", "issues", {"action": "opened", "number": 1})
        assert fp_pr != fp_is


class TestIsDuplicate:

    @patch("app.core.redis_client.is_redis_available", return_value=False)
    def test_first_call_returns_false(self, _):
        idem_module._seen_local.clear()
        assert is_duplicate("unique-fp-001") is False

    @patch("app.core.redis_client.is_redis_available", return_value=False)
    def test_second_call_same_fingerprint_returns_true(self, _):
        idem_module._seen_local.clear()
        is_duplicate("unique-fp-002")
        assert is_duplicate("unique-fp-002") is True

    @patch("app.core.redis_client.is_redis_available", return_value=False)
    def test_different_fingerprints_independent(self, _):
        idem_module._seen_local.clear()
        assert is_duplicate("fp-aaa") is False
        assert is_duplicate("fp-bbb") is False
        assert is_duplicate("fp-aaa") is True
        assert is_duplicate("fp-bbb") is True

    @patch("app.core.redis_client.is_redis_available", return_value=False)
    def test_multiple_unique_events_all_accepted(self, _):
        idem_module._seen_local.clear()
        results = [is_duplicate(f"unique-event-{i}") for i in range(10)]
        assert all(r is False for r in results)

    @patch("app.core.redis_client.is_redis_available", return_value=False)
    def test_repeated_same_event_all_detected_as_duplicate(self, _):
        idem_module._seen_local.clear()
        is_duplicate("same-fp")
        assert all(is_duplicate("same-fp") is True for _ in range(5))

    @patch("app.core.redis_client.is_redis_available", return_value=False)
    def test_full_realistic_dedup_flow(self, _):
        idem_module._seen_local.clear()
        payload = {"action": "opened", "pull_request": {"number": 42},
                   "repository": {"full_name": "org/myrepo"}}
        fp = make_fingerprint("gh-del-abc123", "pull_request", payload)
        assert is_duplicate(fp) is False   # first delivery
        assert is_duplicate(fp) is True    # GitHub retry → duplicate
        fp2 = make_fingerprint("gh-del-xyz999", "pull_request", payload)
        assert is_duplicate(fp2) is False  # different delivery_id → new event
