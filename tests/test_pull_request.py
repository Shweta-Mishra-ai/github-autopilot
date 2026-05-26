"""
tests/test_pull_request.py
Sprint 8 — pull_request handler tests.
Covers: handle() routing, _analyze_pr, _review_code, _detect_test_gaps,
        bot skip, action filter, auth failure, confidence gate.
"""

from unittest.mock import MagicMock, patch
from app.ai.providers.base import LLMResponse


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _meta():
    return LLMResponse(
        text="ok", provider="groq", model="llama", total_tokens=50
    )


def _pr(number=1, title="feat: add login", action="opened",
        author="shweta", head="feat/login", base="main"):
    return {
        "action": action,
        "pull_request": {
            "number": number,
            "title": title,
            "body": "This adds login functionality.",
            "user": {"login": author},
            "head": {"ref": head, "sha": "abc1234"},
            "base": {"ref": base},
        },
        "repository": {"full_name": "org/repo"},
        "installation": {"id": 42},
    }


def _mock_config(pr_enabled=True, code_review=True, test_gaps=True):
    cfg = MagicMock()
    cfg.pr_enabled.return_value = pr_enabled
    cfg.get.side_effect = lambda *a, **kw: {
        ("pull_requests", "code_review"): code_review,
        ("pull_requests", "detect_test_gaps"): test_gaps,
    }.get(a, kw.get("default", True))
    cfg.footer = ""
    return cfg


def _fake_router_response(data: dict):
    return data, _meta()


# ── Handle routing tests ──────────────────────────────────────────────────────

class TestHandleRouting:

    def test_unsupported_action_skipped(self):
        with patch("app.handlers.pull_request.get_installation_token") as mock_tok:
            from app.handlers.pull_request import handle
            handle(_pr(action="closed"))
            mock_tok.assert_not_called()

    def test_bot_author_skipped(self):
        with patch("app.handlers.pull_request.get_installation_token") as mock_tok:
            from app.handlers.pull_request import handle
            handle(_pr(author="dependabot[bot]"))
            mock_tok.assert_not_called()

    def test_auth_failure_returns_early(self):
        with patch("app.handlers.pull_request.get_installation_token",
                   side_effect=Exception("auth failed")), \
             patch("app.handlers.pull_request._analyze_pr") as mock_analyze:
            from app.handlers.pull_request import handle
            handle(_pr())
            mock_analyze.assert_not_called()

    def test_pr_disabled_skips_analysis(self):
        with patch("app.handlers.pull_request.get_installation_token", return_value="tok"), \
             patch("app.handlers.pull_request.load_config",
                   return_value=_mock_config(pr_enabled=False)), \
             patch("app.handlers.pull_request._analyze_pr") as mock_analyze:
            from app.handlers.pull_request import handle
            handle(_pr())
            mock_analyze.assert_not_called()

    def test_opened_triggers_analyze_and_summary(self):
        with patch("app.handlers.pull_request.get_installation_token", return_value="tok"), \
             patch("app.handlers.pull_request.load_config", return_value=_mock_config()), \
             patch("app.handlers.pull_request.gh_get", return_value=[]), \
             patch("app.handlers.pull_request._analyze_pr") as mock_analyze, \
             patch("app.handlers.pull_request._post_pr_summary") as mock_sum, \
             patch("app.handlers.pull_request._review_code"), \
             patch("app.handlers.pull_request._detect_test_gaps"), \
             patch("app.handlers.pull_request.notify_pr_opened"):
            from app.handlers.pull_request import handle
            handle(_pr(action="opened"))
            mock_analyze.assert_called_once()
            mock_sum.assert_called_once()

    def test_synchronize_skips_analyze(self):
        with patch("app.handlers.pull_request.get_installation_token", return_value="tok"), \
             patch("app.handlers.pull_request.load_config", return_value=_mock_config()), \
             patch("app.handlers.pull_request.gh_get", return_value=[]), \
             patch("app.handlers.pull_request._analyze_pr") as mock_analyze, \
             patch("app.handlers.pull_request._review_code"), \
             patch("app.handlers.pull_request._detect_test_gaps"):
            from app.handlers.pull_request import handle
            handle(_pr(action="synchronize"))
            mock_analyze.assert_not_called()

    def test_code_review_disabled_skips_review(self):
        with patch("app.handlers.pull_request.get_installation_token", return_value="tok"), \
             patch("app.handlers.pull_request.load_config",
                   return_value=_mock_config(code_review=False)), \
             patch("app.handlers.pull_request.gh_get", return_value=[]), \
             patch("app.handlers.pull_request._analyze_pr"), \
             patch("app.handlers.pull_request._post_pr_summary"), \
             patch("app.handlers.pull_request._review_code") as mock_review, \
             patch("app.handlers.pull_request._detect_test_gaps"), \
             patch("app.handlers.pull_request.notify_pr_opened"):
            from app.handlers.pull_request import handle
            handle(_pr(action="opened"))
            mock_review.assert_not_called()


# ── _analyze_pr tests ─────────────────────────────────────────────────────────

class TestAnalyzePR:

    def _files(self):
        return [
            {"filename": "app/auth.py", "additions": 10, "deletions": 2,
             "patch": "+def login(): pass"}
        ]

    def test_analyze_posts_comment(self):
        analysis = {
            "title_suggestion": "feat(auth): add login",
            "risk_level": "low",
            "risk_reasons": ["small change"],
            "pr_type": "feature",
            "summary": "Adds login function",
            "breaking_changes": [],
            "score": 8.0,
        }
        cfg = _mock_config()
        cfg.get.side_effect = lambda *a, **kw: kw.get("default", True)
        with patch("app.handlers.pull_request.router.ask",
                   return_value=_fake_router_response(analysis)), \
             patch("app.handlers.pull_request.validate_pr_analysis",
                   return_value=analysis), \
             patch("app.handlers.pull_request.gh_put") as mock_put, \
             patch("app.handlers.pull_request.gh_post") as mock_post, \
             patch("app.handlers.pull_request.check_pr_title_update",
                   return_value=MagicMock(allowed=True)), \
             patch("app.handlers.pull_request.notify_high_risk_pr"):
            from app.handlers.pull_request import _analyze_pr
            pr = _pr()["pull_request"]
            log = MagicMock()
            _analyze_pr(pr, "org/repo", 1, self._files(), "tok", cfg,
                        MagicMock(), "", log)
            # Should attempt to post/update something
            assert mock_post.called or mock_put.called

    def test_analyze_high_risk_sends_notification(self):
        analysis = {
            "title_suggestion": "refactor: overhaul",
            "risk_level": "high",
            "risk_reasons": ["300+ lines changed", "core module"],
            "pr_type": "refactor",
            "summary": "Major overhaul",
            "breaking_changes": ["API changed"],
            "score": 4.0,
        }
        cfg = _mock_config()
        cfg.get.side_effect = lambda *a, **kw: kw.get("default", True)
        with patch("app.handlers.pull_request.router.ask",
                   return_value=_fake_router_response(analysis)), \
             patch("app.handlers.pull_request.validate_pr_analysis",
                   return_value=analysis), \
             patch("app.handlers.pull_request.gh_put"), \
             patch("app.handlers.pull_request.gh_post"), \
             patch("app.handlers.pull_request.check_pr_title_update",
                   return_value=MagicMock(allowed=True)), \
             patch("app.handlers.pull_request.notify_high_risk_pr") as mock_notif:
            from app.handlers.pull_request import _analyze_pr
            pr = _pr()["pull_request"]
            log = MagicMock()
            _analyze_pr(pr, "org/repo", 1, self._files(), "tok", cfg,
                        MagicMock(), "", log)
            mock_notif.assert_called_once()

    def test_analyze_router_error_propagates(self):
        """_analyze_pr lets LLM errors propagate — caught by server.py dispatch layer."""
        import pytest
        cfg = _mock_config()
        cfg.get.side_effect = lambda *a, **kw: kw.get("default", True)
        with patch("app.handlers.pull_request.router.ask",
                   side_effect=Exception("LLM timeout")), \
             patch("app.handlers.pull_request.gh_post"):
            from app.handlers.pull_request import _analyze_pr
            pr = _pr()["pull_request"]
            log = MagicMock()
            with pytest.raises(Exception, match="LLM timeout"):
                _analyze_pr(pr, "org/repo", 1, self._files(), "tok", cfg,
                            MagicMock(), "", log)


# ── _blast_radius tests ───────────────────────────────────────────────────────

class TestBlastRadius:

    def test_categories_detected(self):
        from app.handlers.pull_request import _blast_radius
        files = [
            {"filename": "app/handlers/auth.py"},
            {"filename": "tests/test_auth.py"},
            {"filename": "requirements.txt"},
            {"filename": "app/core/config.py"},
        ]
        result = _blast_radius(files)
        assert "handler" in result.lower() or "Handler" in result
        assert isinstance(result, str)
        assert len(result) > 0

    def test_empty_files(self):
        from app.handlers.pull_request import _blast_radius
        result = _blast_radius([])
        assert isinstance(result, str)

    def test_unknown_files_categorized(self):
        from app.handlers.pull_request import _blast_radius
        files = [{"filename": "some/random/file.xyz"}]
        result = _blast_radius(files)
        assert isinstance(result, str)


# ── _review_code tests ────────────────────────────────────────────────────────

class TestReviewCode:

    def test_review_posts_comment(self):
        review = {
            "overall_score": 8.5,
            "summary": "Good PR overall",
            "issues": [],
            "suggestions": ["Add docstrings"],
            "security_concerns": [],
            "approved": True,
        }
        files = [
            {"filename": "app/auth.py", "patch": "+def login(): pass",
             "additions": 1, "deletions": 0}
        ]
        cfg = _mock_config()
        cfg.get.side_effect = lambda *a, **kw: kw.get("default", True)
        with patch("app.handlers.pull_request.router.ask",
                   return_value=_fake_router_response(review)), \
             patch("app.handlers.pull_request.validate_code_review",
                   return_value=review), \
             patch("app.handlers.pull_request.gh_post") as mock_post:
            from app.handlers.pull_request import _review_code
            pr = _pr()["pull_request"]
            log = MagicMock()
            _review_code(pr, "org/repo", 1, files, "tok", cfg,
                         MagicMock(), "", log)
            mock_post.assert_called_once()

    def test_no_files_with_patches_skips(self):
        files = [{"filename": "app/auth.py"}]  # no patch key
        cfg = _mock_config()
        with patch("app.handlers.pull_request.router.ask") as mock_ask, \
             patch("app.handlers.pull_request.gh_post") as mock_post:
            from app.handlers.pull_request import _review_code
            pr = _pr()["pull_request"]
            log = MagicMock()
            _review_code(pr, "org/repo", 1, files, "tok", cfg,
                         MagicMock(), "", log)
            mock_ask.assert_not_called()
            mock_post.assert_not_called()


# ── _detect_test_gaps tests ───────────────────────────────────────────────────

class TestDetectTestGaps:

    def test_no_python_files_skips(self):
        files = [{"filename": "README.md"}]
        cfg = _mock_config()
        with patch("app.handlers.pull_request.router.ask") as mock_ask:
            from app.handlers.pull_request import _detect_test_gaps
            pr = _pr()["pull_request"]
            log = MagicMock()
            _detect_test_gaps(pr, "org/repo", 1, files, "tok", cfg, log)
            mock_ask.assert_not_called()

    def test_test_files_only_skips(self):
        files = [{"filename": "tests/test_auth.py",
                  "patch": "+def test_login(): pass"}]
        cfg = _mock_config()
        with patch("app.handlers.pull_request.router.ask") as mock_ask:
            from app.handlers.pull_request import _detect_test_gaps
            pr = _pr()["pull_request"]
            log = MagicMock()
            _detect_test_gaps(pr, "org/repo", 1, files, "tok", cfg, log)
            mock_ask.assert_not_called()

    def test_source_file_triggers_gap_analysis(self):
        files = [
            {"filename": "app/auth.py",
             "patch": "+def login(user, pwd): return True"}
        ]
        gaps = {
            "has_gaps": True,
            "coverage_score": 4,
            "gaps": [{"file": "app/auth.py", "function": "login",
                      "risk": "high", "suggested_test": "test wrong password"}],
            "summary": "Missing edge case tests",
        }
        cfg = _mock_config()
        cfg.footer = ""
        with patch("app.handlers.pull_request.router.ask",
                   return_value=_fake_router_response(gaps)), \
             patch("app.handlers.pull_request.gh_post") as mock_post:
            from app.handlers.pull_request import _detect_test_gaps
            pr = _pr()["pull_request"]
            log = MagicMock()
            _detect_test_gaps(pr, "org/repo", 1, files, "tok", cfg, log)
            mock_post.assert_called_once()

    def test_no_gaps_detected_no_comment(self):
        files = [
            {"filename": "app/auth.py",
             "patch": "+def login(user, pwd): return True"}
        ]
        gaps = {
            "has_gaps": False,
            "coverage_score": 9,
            "gaps": [],
            "summary": "Good coverage",
        }
        cfg = _mock_config()
        with patch("app.handlers.pull_request.router.ask",
                   return_value=_fake_router_response(gaps)), \
             patch("app.handlers.pull_request.gh_post") as mock_post:
            from app.handlers.pull_request import _detect_test_gaps
            pr = _pr()["pull_request"]
            log = MagicMock()
            _detect_test_gaps(pr, "org/repo", 1, files, "tok", cfg, log)
            mock_post.assert_not_called()

