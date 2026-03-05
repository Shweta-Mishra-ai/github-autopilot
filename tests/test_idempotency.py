"""
tests/test_idempotency.py
Pure unit tests for idempotency logic.
No network calls needed.

Run: python -m pytest tests/test_idempotency.py -v
"""

import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.idempotency import make_fingerprint, is_duplicate, _seen


def setup_function():
    """Clear cache before each test."""
    _seen.clear()


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

    def test_fingerprint_is_64_char_hex_string(self):
        fp = make_fingerprint("delivery-abc", "push", {})
        assert isinstance(fp, str)
        assert len(fp) == 64
        assert all(c in "0123456789abcdef" for c in fp)

    def test_empty_payload_handled(self):
        fp = make_fingerprint("", "", {})
        assert isinstance(fp, str)
        assert len(fp) == 64


class TestIsDuplicate:

    def test_first_call_returns_false(self):
        assert is_duplicate("unique-fp-001") is False

    def test_second_call_same_fingerprint_returns_true(self):
        is_duplicate("unique-fp-002")
        assert is_duplicate("unique-fp-002") is True

    def test_different_fingerprints_are_independent(self):
        assert is_duplicate("fp-aaa") is False
        assert is_duplicate("fp-bbb") is False
        assert is_duplicate("fp-aaa") is True
        assert is_duplicate("fp-bbb") is True

    def test_cache_cleared_between_tests(self):
        # After setup_function clears cache, this should be False
        assert is_duplicate("fp-cleared-check") is False

    def test_multiple_unique_events_all_accepted(self):
        results = [is_duplicate(f"unique-event-{i}") for i in range(10)]
        assert all(r is False for r in results)

    def test_all_same_events_all_detected_as_duplicate(self):
        is_duplicate("same-fp-always")
        results = [is_duplicate("same-fp-always") for _ in range(5)]
        assert all(r is True for r in results)


class TestFingerprintRealWebhookPayloads:
    """Test with realistic GitHub webhook payloads."""

    def test_pr_opened_event(self):
        payload = {
            "action": "opened",
            "number": 5,
            "pull_request": {"title": "feat: add auth"},
            "repository": {"full_name": "user/repo"}
        }
        fp = make_fingerprint("abc-delivery-id", "pull_request", payload)
        assert len(fp) == 64

    def test_issue_created_event(self):
        payload = {
            "action": "opened",
            "issue": {"number": 3, "title": "Bug in login"},
            "repository": {"full_name": "user/repo"}
        }
        fp = make_fingerprint("xyz-delivery-id", "issues", payload)
        assert len(fp) == 64

    def test_pr_and_issue_same_number_different_fingerprints(self):
        pr_payload = {"action": "opened", "number": 1}
        issue_payload = {"action": "opened", "number": 1}
        fp_pr = make_fingerprint("delivery-1", "pull_request", pr_payload)
        fp_issue = make_fingerprint("delivery-1", "issues", issue_payload)
        assert fp_pr != fp_issue

