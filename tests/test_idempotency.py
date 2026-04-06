"""
tests/test_idempotency.py
Pure unit tests for idempotency logic. V4.

FIXED: `_seen` → `_seen_local` (V4 renamed internal variable)
FIXED: fingerprint length 64 → 16 (V4 uses hexdigest()[:16])
FIXED: `setup_function` now clears `_seen_local` directly
FIXED: Tests patch is_redis_available=False to force deterministic in-memory path

Run: python -m pytest tests/test_idempotency.py -v
"""

import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.core.idempotency as idem_module
from app.core.idempotency import make_fingerprint, is_duplicate


def setup_function():
    """Clear in-memory cache before each test for isolation."""
    idem_module._seen_local.clear()


# ── Tests: make_fingerprint ───────────────────────────────────────────────────

class TestMakeFingerprint:

    def test_same_inputs_produce_same_fingerprint(self):
        payload = {"action": "opened", "number": 42}
        fp1 = make_fingerprint("delivery-123", "pull_request", payload)
        fp2 = make_fingerprint("delivery-123", "pull_request", payload)
        assert fp1 == fp2

    def test_different_delivery_id_produces_different_fingerprint(self):
        payload = {"action": "opened", "number": 42}
        fp1 = make_fingerprint("delivery-111", "pull_request", payload)
        fp2 = make_fingerprint("delivery-222", "pull_request", payload)
        assert fp1 != fp2

    def test_different_event_type_produces_different_fingerprint(self):
        payload = {"action": "opened", "number": 42}
        fp1 = make_fingerprint("delivery-123", "pull_request", payload)
        fp2 = make_fingerprint("delivery-123", "issues", payload)
        assert fp1 != fp2

    def test_different_action_produces_different_fingerprint(self):
        payload1 = {"action": "opened", "number": 42}
        payload2 = {"action": "closed", "number": 42}
        fp1 = make_fingerprint("delivery-123", "issues", payload1)
        fp2 = make_fingerprint("delivery-123", "issues", payload2)
        assert fp1 != fp2

    def test_fingerprint_is_16_char_hex_string(self):
        """V4 returns first 16 chars of sha256 hex (was 64 in V3)."""
        fp = make_fingerprint("delivery-abc", "push", {})
        assert isinstance(fp, str)
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)

    def test_empty_payload_handled(self):
        fp = make_fingerprint("", "", {})
        assert isinstance(fp, str)
        assert len(fp) == 16

    def test_fingerprint_is_deterministic(self):
        payload = {"action": "opened", "repository": {"full_name": "org/repo"}}
        results = [make_fingerprint("del-xyz", "pull_request", payload) for _ in range(5)]
        assert len(set(results)) == 1


# ── Tests: is_duplicate (in-memory fallback path) ────────────────────────────

class TestIsDuplicate:

    @patch("app.core.idempotency.is_redis_available", return_value=False)
    def test_first_call_returns_false(self, _):
        idem_module._seen_local.clear()
        assert is_duplicate("unique-fp-001") is False

    @patch("app.core.idempotency.is_redis_available", return_value=False)
    def test_second_call_same_fingerprint_returns_true(self, _):
        idem_module._seen_local.clear()
        is_duplicate("unique-fp-002")
        assert is_duplicate("unique-fp-002") is True

    @patch("app.core.idempotency.is_redis_available", return_value=False)
    def test_different_fingerprints_are_independent(self, _):
        idem_module._seen_local.clear()
        assert is_duplicate("fp-aaa") is False
        assert is_duplicate("fp-bbb") is False
        assert is_duplicate("fp-aaa") is True
        assert is_duplicate("fp-bbb") is True

    @patch("app.core.idempotency.is_redis_available", return_value=False)
    def test_cache_cleared_between_tests(self, _):
        idem_module._seen_local.clear()
        assert is_duplicate("fp-cleared-check") is False

    @patch("app.core.idempotency.is_redis_available", return_value=False)
    def test_multiple_unique_events_all_accepted(self, _):
        idem_module._seen_local.clear()
        results = [is_duplicate(f"unique-event-{i}") for i in range(10)]
        assert all(r is False for r in results)

    @patch("app.core.idempotency.is_redis_available", return_value=False)
    def test_all_same_events_detected_as_duplicate(self, _):
        idem_module._seen_local.clear()
        is_duplicate("same-fp-always")
        results = [is_duplicate("same-fp-always") for _ in range(5)]
        assert all(r is True for r in results)


# ── Tests: Real webhook payloads ──────────────────────────────────────────────

class TestFingerprintRealWebhookPayloads:

    def test_pr_opened_event(self):
        payload = {
            "action":       "opened",
            "number":       5,
            "pull_request": {"title": "feat: add auth"},
            "repository":   {"full_name": "user/repo"},
        }
        fp = make_fingerprint("abc-delivery-id", "pull_request", payload)
        assert len(fp) == 16

    def test_issue_created_event(self):
        payload = {
            "action":     "opened",
            "issue":      {"number": 3, "title": "Bug in login"},
            "repository": {"full_name": "user/repo"},
        }
        fp = make_fingerprint("xyz-delivery-id", "issues", payload)
        assert len(fp) == 16

    def test_pr_and_issue_same_number_different_fingerprints(self):
        pr_payload    = {"action": "opened", "number": 1}
        issue_payload = {"action": "opened", "number": 1}
        fp_pr    = make_fingerprint("delivery-1", "pull_request", pr_payload)
        fp_issue = make_fingerprint("delivery-1", "issues", issue_payload)
        assert fp_pr != fp_issue

    @patch("app.core.idempotency.is_redis_available", return_value=False)
    def test_full_dedup_flow_realistic_payload(self, _):
        idem_module._seen_local.clear()
        payload = {
            "action":       "opened",
            "pull_request": {"number": 42},
            "repository":   {"full_name": "org/myrepo"},
        }
        fp = make_fingerprint("gh-delivery-abc123", "pull_request", payload)
        # First time: new event
        assert is_duplicate(fp) is False
        # GitHub retry: duplicate
        assert is_duplicate(fp) is True
        # Different delivery ID: new event
        fp2 = make_fingerprint("gh-delivery-xyz999", "pull_request", payload)
        assert is_duplicate(fp2) is False
