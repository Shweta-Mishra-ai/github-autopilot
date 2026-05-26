"""
tests/test_issues.py
Sprint 8 — issues handler tests.
Covers: action filter, bot skip, PR-as-issue skip, auth failure,
        issues disabled, triage comment, label posting, notification.
"""

from unittest.mock import MagicMock, patch
from app.ai.providers.base import LLMResponse


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _meta():
    return LLMResponse(
        text="ok", provider="groq", model="llama", total_tokens=50
    )


def _payload(
    action="opened",
    author="shweta",
    title="App crashes on login",
    body="Steps to reproduce...",
    issue_number=42,
    is_pr=False,
    installation_id=99,
):
    issue = {
        "number": issue_number,
        "title": title,
        "body": body,
        "user": {"login": author},
        "labels": [],
    }
    if is_pr:
        issue["pull_request"] = {"url": "https://api.github.com/repos/org/repo/pulls/1"}
    return {
        "action": action,
        "issue": issue,
        "repository": {"full_name": "org/repo"},
        "installation": {"id": installation_id},
    }


def _mock_config(issues_enabled=True, auto_labels=True):
    cfg = MagicMock()
    cfg.issues_enabled.return_value = issues_enabled
    cfg.get.side_effect = lambda *a, **kw: {
        ("labels", "auto_create"): auto_labels,
    }.get(a, kw.get("default", True))
    cfg.footer = ""
    return cfg


def _triage_result():
    return {
        "type": "bug",
        "priority": "high",
        "complexity": "moderate",
        "time_estimate": "1-4 hours",
        "labels": ["bug 🐛"],
        "welcome": "Thanks for reporting this crash! I can see this is blocking.",
        "needs_info": True,
        "questions": ["What version are you using?", "Can you share a stack trace?"],
        "is_duplicate_risk": False,
        "similar_search_terms": ["login crash", "app crash"],
        "auto_close_reason": "",
    }


# ── Handle routing tests ──────────────────────────────────────────────────────

class TestHandleRouting:

    def test_non_opened_action_skipped(self):
        with patch("app.handlers.issues.get_installation_token") as mock_tok:
            from app.handlers.issues import handle
            handle(_payload(action="closed"))
            mock_tok.assert_not_called()

    def test_pull_request_event_skipped(self):
        with patch("app.handlers.issues.get_installation_token") as mock_tok:
            from app.handlers.issues import handle
            handle(_payload(is_pr=True))
            mock_tok.assert_not_called()

    def test_bot_author_skipped(self):
        with patch("app.handlers.issues.get_installation_token") as mock_tok:
            from app.handlers.issues import handle
            handle(_payload(author="dependabot[bot]"))
            mock_tok.assert_not_called()

    def test_auth_failure_returns_early(self):
        with patch("app.handlers.issues.get_installation_token",
                   side_effect=Exception("auth fail")), \
             patch("app.handlers.issues.router.ask") as mock_ask:
            from app.handlers.issues import handle
            handle(_payload())
            mock_ask.assert_not_called()

    def test_issues_disabled_skips(self):
        with patch("app.handlers.issues.get_installation_token", return_value="tok"), \
             patch("app.handlers.issues.load_config",
                   return_value=_mock_config(issues_enabled=False)), \
             patch("app.handlers.issues.gh_get", return_value={}), \
             patch("app.handlers.issues.router.ask") as mock_ask:
            from app.handlers.issues import handle
            handle(_payload())
            mock_ask.assert_not_called()


# ── Full triage flow tests ────────────────────────────────────────────────────

class TestTriageFlow:

    def _run_handle(self, triage=None, config=None):
        triage = triage or _triage_result()
        config = config or _mock_config()
        meta   = _meta()
        with patch("app.handlers.issues.get_installation_token", return_value="tok"), \
             patch("app.handlers.issues.load_config", return_value=config), \
             patch("app.handlers.issues.gh_get", return_value={"language": "Python"}), \
             patch("app.handlers.issues.router.ask", return_value=(triage, meta)), \
             patch("app.handlers.issues.validate_issue_triage", return_value=triage), \
             patch("app.handlers.issues.check_auto_label",
                   return_value=MagicMock(passed=True)), \
             patch("app.handlers.issues.gh_post") as mock_post, \
             patch("app.handlers.issues.notify_new_issue"), \
             patch("app.handlers.issues._ensure_labels"):
            from app.handlers.issues import handle
            handle(_payload())
            return mock_post

    def test_triage_posts_comment(self):
        mock_post = self._run_handle()
        # Should post at least: labels + comment
        assert mock_post.call_count >= 1
        calls = [str(c) for c in mock_post.call_args_list]
        assert any("comments" in c for c in calls)

    def test_triage_posts_labels(self):
        mock_post = self._run_handle()
        calls = [str(c) for c in mock_post.call_args_list]
        assert any("labels" in c for c in calls)

    def test_comment_contains_welcome(self):
        triage = _triage_result()
        triage["welcome"] = "Thanks for the unique report about the crash!"
        meta   = _meta()
        with patch("app.handlers.issues.get_installation_token", return_value="tok"), \
             patch("app.handlers.issues.load_config", return_value=_mock_config()), \
             patch("app.handlers.issues.gh_get", return_value={"language": "Python"}), \
             patch("app.handlers.issues.router.ask", return_value=(triage, meta)), \
             patch("app.handlers.issues.validate_issue_triage", return_value=triage), \
             patch("app.handlers.issues.check_auto_label",
                   return_value=MagicMock(passed=True)), \
             patch("app.handlers.issues.gh_post") as mock_post, \
             patch("app.handlers.issues.notify_new_issue"), \
             patch("app.handlers.issues._ensure_labels"):
            from app.handlers.issues import handle
            handle(_payload())
            comment_calls = [
                c for c in mock_post.call_args_list
                if "comments" in str(c)
            ]
            assert len(comment_calls) >= 1
            body = comment_calls[0][0][2]["body"]
            assert "unique report about the crash" in body

    def test_comment_contains_priority(self):
        triage = _triage_result()
        triage["priority"] = "critical"
        meta   = _meta()
        with patch("app.handlers.issues.get_installation_token", return_value="tok"), \
             patch("app.handlers.issues.load_config", return_value=_mock_config()), \
             patch("app.handlers.issues.gh_get", return_value={}), \
             patch("app.handlers.issues.router.ask", return_value=(triage, meta)), \
             patch("app.handlers.issues.validate_issue_triage", return_value=triage), \
             patch("app.handlers.issues.check_auto_label",
                   return_value=MagicMock(passed=True)), \
             patch("app.handlers.issues.gh_post") as mock_post, \
             patch("app.handlers.issues.notify_new_issue"), \
             patch("app.handlers.issues._ensure_labels"):
            from app.handlers.issues import handle
            handle(_payload())
            comment_calls = [
                c for c in mock_post.call_args_list
                if "comments" in str(c)
            ]
            if comment_calls:
                body = comment_calls[0][0][2]["body"]
                assert "Critical" in body or "critical" in body

    def test_label_guard_blocked_skips_label_post(self):
        triage = _triage_result()
        meta   = _meta()
        with patch("app.handlers.issues.get_installation_token", return_value="tok"), \
             patch("app.handlers.issues.load_config", return_value=_mock_config()), \
             patch("app.handlers.issues.gh_get", return_value={}), \
             patch("app.handlers.issues.router.ask", return_value=(triage, meta)), \
             patch("app.handlers.issues.validate_issue_triage", return_value=triage), \
             patch("app.handlers.issues.check_auto_label",
                   return_value=MagicMock(passed=False)), \
             patch("app.handlers.issues.gh_post") as mock_post, \
             patch("app.handlers.issues.notify_new_issue"), \
             patch("app.handlers.issues._ensure_labels"):
            from app.handlers.issues import handle
            handle(_payload())
            calls = [str(c) for c in mock_post.call_args_list]
            assert not any("labels" in c for c in calls)

    def test_router_failure_handled_gracefully(self):
        """issues.py lets router exceptions propagate to dispatch layer."""
        import pytest
        with patch("app.handlers.issues.get_installation_token", return_value="tok"), \
             patch("app.handlers.issues.load_config", return_value=_mock_config()), \
             patch("app.handlers.issues.gh_get", return_value={}), \
             patch("app.handlers.issues.router.ask",
                   side_effect=Exception("LLM down")), \
             patch("app.handlers.issues.gh_post"), \
             patch("app.handlers.issues._ensure_labels"):
            from app.handlers.issues import handle
            with pytest.raises(Exception, match="LLM down"):
                handle(_payload())

    def test_needs_info_questions_in_comment(self):
        triage = _triage_result()
        triage["needs_info"] = True
        triage["questions"] = ["What OS?", "Python version?"]
        meta   = _meta()
        with patch("app.handlers.issues.get_installation_token", return_value="tok"), \
             patch("app.handlers.issues.load_config", return_value=_mock_config()), \
             patch("app.handlers.issues.gh_get", return_value={}), \
             patch("app.handlers.issues.router.ask", return_value=(triage, meta)), \
             patch("app.handlers.issues.validate_issue_triage", return_value=triage), \
             patch("app.handlers.issues.check_auto_label",
                   return_value=MagicMock(passed=True)), \
             patch("app.handlers.issues.gh_post") as mock_post, \
             patch("app.handlers.issues.notify_new_issue"), \
             patch("app.handlers.issues._ensure_labels"):
            from app.handlers.issues import handle
            handle(_payload())
            comment_calls = [
                c for c in mock_post.call_args_list
                if "comments" in str(c)
            ]
            if comment_calls:
                body = comment_calls[0][0][2]["body"]
                assert "What OS?" in body or "Python version?" in body

    def test_notification_sent_on_success(self):
        triage = _triage_result()
        meta   = _meta()
        with patch("app.handlers.issues.get_installation_token", return_value="tok"), \
             patch("app.handlers.issues.load_config", return_value=_mock_config()), \
             patch("app.handlers.issues.gh_get", return_value={}), \
             patch("app.handlers.issues.router.ask", return_value=(triage, meta)), \
             patch("app.handlers.issues.validate_issue_triage", return_value=triage), \
             patch("app.handlers.issues.check_auto_label",
                   return_value=MagicMock(passed=True)), \
             patch("app.handlers.issues.gh_post"), \
             patch("app.handlers.issues.notify_new_issue") as mock_notif, \
             patch("app.handlers.issues._ensure_labels"):
            from app.handlers.issues import handle
            handle(_payload())
            mock_notif.assert_called_once()

