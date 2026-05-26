"""
tests/test_ci.py
Sprint 8 — CI handler tests.
Covers: action filter, skip conclusions, auth failure, no PR associated,
        CI disabled, failure analysis comment, failure pattern tracking.
"""

from unittest.mock import MagicMock, patch
from app.ai.providers.base import LLMResponse


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _meta():
    return LLMResponse(
        text="ok", provider="groq", model="llama", total_tokens=50
    )


def _payload(
    action="completed",
    conclusion="failure",
    check_name="pytest",
    pr_numbers=None,
    installation_id=42,
    output=None,
):
    return {
        "action": action,
        "check_run": {
            "name": check_name,
            "conclusion": conclusion,
            "output": output or {
                "title": "Tests failed",
                "summary": "3 tests failed",
                "text": "FAILED tests/test_auth.py::test_login",
            },
            "pull_requests": pr_numbers if pr_numbers is not None else [{"number": 7}],
        },
        "repository": {"full_name": "org/repo"},
        "installation": {"id": installation_id},
    }


def _mock_config(ci_enabled=True):
    cfg = MagicMock()
    cfg.get.side_effect = lambda *a, **kw: {
        ("ci", "enabled"): ci_enabled,
    }.get(a, kw.get("default", True))
    cfg.footer = ""
    return cfg


# ── Handle routing tests ──────────────────────────────────────────────────────

class TestHandleRouting:

    def test_non_completed_action_skipped(self):
        with patch("app.handlers.ci.get_installation_token") as mock_tok:
            from app.handlers.ci import handle
            handle(_payload(action="created"))
            mock_tok.assert_not_called()

    def test_skipped_conclusion_ignored(self):
        with patch("app.handlers.ci.get_installation_token") as mock_tok:
            from app.handlers.ci import handle
            handle(_payload(conclusion="skipped"))
            mock_tok.assert_not_called()

    def test_neutral_conclusion_ignored(self):
        with patch("app.handlers.ci.get_installation_token") as mock_tok:
            from app.handlers.ci import handle
            handle(_payload(conclusion="neutral"))
            mock_tok.assert_not_called()

    def test_cancelled_conclusion_ignored(self):
        with patch("app.handlers.ci.get_installation_token") as mock_tok:
            from app.handlers.ci import handle
            handle(_payload(conclusion="cancelled"))
            mock_tok.assert_not_called()

    def test_success_conclusion_ignored(self):
        with patch("app.handlers.ci.get_installation_token") as mock_tok:
            from app.handlers.ci import handle
            handle(_payload(conclusion="success"))
            mock_tok.assert_not_called()

    def test_missing_installation_id_ignored(self):
        payload = _payload()
        payload.pop("installation", None)
        with patch("app.handlers.ci.get_installation_token") as mock_tok:
            from app.handlers.ci import handle
            handle(payload)
            mock_tok.assert_not_called()

    def test_auth_failure_returns_early(self):
        with patch("app.handlers.ci.get_installation_token",
                   side_effect=Exception("auth fail")), \
             patch("app.handlers.ci.router.ask") as mock_ask:
            from app.handlers.ci import handle
            handle(_payload())
            mock_ask.assert_not_called()

    def test_ci_disabled_skips_analysis(self):
        with patch("app.handlers.ci.get_installation_token", return_value="tok"), \
             patch("app.handlers.ci.load_config",
                   return_value=_mock_config(ci_enabled=False)), \
             patch("app.handlers.ci.router.ask") as mock_ask:
            from app.handlers.ci import handle
            handle(_payload())
            mock_ask.assert_not_called()

    def test_no_pull_requests_skips_comment(self):
        import app.handlers.ci as ci_mod
        from app.ai.providers.base import LLMResponse
        meta = LLMResponse(text="ok", provider="groq", model="llama", total_tokens=10)
        analysis = {"root_cause": "err", "category": "other", "fix": "-f", "is_flaky": False, "confidence": 0.8}
        with patch.object(ci_mod, "get_installation_token", return_value="tok"), \
             patch.object(ci_mod, "load_config", return_value=_mock_config()), \
             patch.object(ci_mod, "router") as mock_router, \
             patch.object(ci_mod, "gh_post") as mock_post:
            mock_router.ask.return_value = (analysis, meta)
            ci_mod.handle(_payload(pr_numbers=[]))
            mock_router.ask.assert_not_called()
            mock_post.assert_not_called()


# ── Failure analysis tests ────────────────────────────────────────────────────

class TestFailureAnalysis:

    def _run(self, analysis=None, config=None):
        analysis = analysis or {
            "root_cause": "ImportError in test_auth.py line 12",
            "category": "test_failure",
            "fix": "- Install missing dependency\n- Run pip install -r requirements.txt",
            "is_flaky": False,
            "confidence": 0.9,
        }
        config = config or _mock_config()
        meta   = _meta()
        with patch("app.handlers.ci.get_installation_token", return_value="tok"), \
             patch("app.handlers.ci.load_config", return_value=config), \
             patch("app.handlers.ci.router.ask",
                   return_value=(analysis, meta)) as mock_ask, \
             patch("app.handlers.ci.gh_post") as mock_post:
            from app.handlers.ci import handle
            handle(_payload())
            return mock_ask, mock_post

    def test_failure_posts_comment(self):
        _, mock_post = self._run()
        mock_post.assert_called_once()
        args = mock_post.call_args[0]
        assert "comments" in args[0]

    def test_comment_contains_root_cause(self):
        analysis = {
            "root_cause": "Missing REDIS_URL environment variable",
            "category": "other",
            "fix": "- Set REDIS_URL in environment",
            "is_flaky": False,
            "confidence": 0.85,
        }
        _, mock_post = self._run(analysis=analysis)
        body = mock_post.call_args[0][2]["body"]
        assert "REDIS_URL" in body

    def test_flaky_note_included(self):
        analysis = {
            "root_cause": "Race condition in test setup",
            "category": "test_failure",
            "fix": "- Add sleep or use proper test fixtures",
            "is_flaky": True,
            "confidence": 0.5,
        }
        _, mock_post = self._run(analysis=analysis)
        body = mock_post.call_args[0][2]["body"]
        assert "flaky" in body.lower() or "Flaky" in body

    def test_category_emoji_in_comment(self):
        for category, expected in [
            ("test_failure", "🧪"),
            ("build_error", "🏗️"),
            ("lint_error", "🔍"),
            ("dependency", "📦"),
            ("timeout", "⏱️"),
        ]:
            analysis = {
                "root_cause": f"{category} root cause",
                "category": category,
                "fix": "- fix step",
                "is_flaky": False,
                "confidence": 0.8,
            }
            meta = _meta()
            with patch("app.handlers.ci.get_installation_token", return_value="tok"), \
                 patch("app.handlers.ci.load_config", return_value=_mock_config()), \
                 patch("app.handlers.ci.router.ask", return_value=(analysis, meta)), \
                 patch("app.handlers.ci.gh_post") as mock_post:
                from app.handlers.ci import handle
                handle(_payload())
                body = mock_post.call_args[0][2]["body"]
                assert expected in body, f"Expected {expected} for category {category}"

    def test_router_exception_handled(self):
        with patch("app.handlers.ci.get_installation_token", return_value="tok"), \
             patch("app.handlers.ci.load_config", return_value=_mock_config()), \
             patch("app.handlers.ci.router.ask",
                   side_effect=Exception("LLM timeout")), \
             patch("app.handlers.ci.gh_post") as mock_post:
            from app.handlers.ci import handle
            handle(_payload())  # Must not raise
            mock_post.assert_not_called()

    def test_comment_posted_to_correct_pr(self):
        analysis = {
            "root_cause": "Test error",
            "category": "test_failure",
            "fix": "- Fix it",
            "is_flaky": False,
            "confidence": 0.9,
        }
        meta = _meta()
        with patch("app.handlers.ci.get_installation_token", return_value="tok"), \
             patch("app.handlers.ci.load_config", return_value=_mock_config()), \
             patch("app.handlers.ci.router.ask", return_value=(analysis, meta)), \
             patch("app.handlers.ci.gh_post") as mock_post:
            from app.handlers.ci import handle
            handle(_payload(pr_numbers=[{"number": 42}]))
            url = mock_post.call_args[0][0]
            assert "/42/comments" in url


# ── Failure pattern tracking tests ───────────────────────────────────────────

class TestFailurePatternTracking:
    # ci.py imports get_redis via `from app.core.redis_client import get_redis`
    # inside the function body, so we patch at the source module.

    def test_track_increments_counter(self):
        fake_redis = MagicMock()
        fake_redis.incr.return_value = 1
        with patch("app.core.redis_client.get_redis", return_value=fake_redis):
            from app.handlers.ci import _track_failure_pattern
            _track_failure_pattern("org/repo", "pytest", "import error")
            fake_redis.incr.assert_called_once()

    def test_third_failure_returns_true(self):
        fake_redis = MagicMock()
        fake_redis.incr.return_value = 3
        with patch("app.core.redis_client.get_redis", return_value=fake_redis):
            from app.handlers.ci import _track_failure_pattern
            result = _track_failure_pattern("org/repo", "pytest", "error")
            assert result is True

    def test_first_failure_returns_false(self):
        fake_redis = MagicMock()
        fake_redis.incr.return_value = 1
        with patch("app.core.redis_client.get_redis", return_value=fake_redis):
            from app.handlers.ci import _track_failure_pattern
            result = _track_failure_pattern("org/repo", "pytest", "error")
            assert result is False

    def test_get_failure_count_returns_zero_on_error(self):
        with patch("app.core.redis_client.get_redis", side_effect=Exception("redis down")):
            from app.handlers.ci import _get_failure_count
            result = _get_failure_count("org/repo", "pytest")
            assert result == 0

    def test_get_failure_count_returns_value(self):
        fake_redis = MagicMock()
        fake_redis.get.return_value = "5"
        with patch("app.core.redis_client.get_redis", return_value=fake_redis):
            from app.handlers.ci import _get_failure_count
            result = _get_failure_count("org/repo", "pytest")
            assert result == 5

