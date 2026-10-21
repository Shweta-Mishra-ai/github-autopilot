"""
tests/test_ci.py
Sprint 8 — CI handler tests.
Covers: action filter, skip conclusions, auth failure, no PR associated,
        CI disabled, failure analysis comment, failure pattern tracking.
"""

import pytest
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
    # ci.py now calls config.ci_enabled(), which also honours the
    # bot.enabled kill switch — a raw get() bypassed it.
    cfg.ci_enabled.return_value = ci_enabled
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

    def test_auth_failure_is_raised_before_any_analysis(self):
        # Raised for the queue to retry; returning reported it as handled.
        with patch("app.handlers.ci.get_installation_token",
                   side_effect=Exception("auth fail")), \
             patch("app.ai.guarded.safe_router_ask") as mock_ask, \
             pytest.raises(Exception, match="auth fail"):
            from app.handlers.ci import handle
            handle(_payload())
        mock_ask.assert_not_called()

    def test_ci_disabled_skips_analysis(self):
        with patch("app.handlers.ci.get_installation_token", return_value="tok"), \
             patch("app.handlers.ci.load_config",
                   return_value=_mock_config(ci_enabled=False)), \
             patch("app.ai.guarded.safe_router_ask") as mock_ask:
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
             patch("app.ai.guarded.safe_router_ask") as mock_ask, \
             patch("app.github.sticky.upsert_sticky") as mock_post:
            mock_ask.return_value = (analysis, meta)
            ci_mod.handle(_payload(pr_numbers=[]))
            mock_ask.assert_not_called()
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
        # V7: the analysis goes through app.ai.guarded.guarded_ask, and the
        # comment through the sticky upsert rather than a bare gh_post. The
        # per-SHA dedup is stubbed off so each test starts from a clean slate.
        with patch("app.handlers.ci.get_installation_token", return_value="tok"), \
             patch("app.handlers.ci.load_config", return_value=config), \
             patch("app.handlers.ci._ci_already_alerted", return_value=False), \
             patch("app.handlers.ci._current_ci_report", return_value=""), \
             patch("app.ai.guarded.safe_router_ask",
                   return_value=(analysis, meta)) as mock_ask, \
             patch("app.github.sticky.upsert_sticky") as mock_post:
            from app.handlers.ci import handle
            handle(_payload())
            return mock_ask, mock_post

    def test_failure_posts_comment(self):
        _, mock_post = self._run()
        mock_post.assert_called_once()
        args = mock_post.call_args[0]
        assert args[0] == "org/repo"  # V7: upsert_sticky(repo, pr, token, marker, body)

    def test_comment_contains_root_cause(self):
        analysis = {
            "root_cause": "Missing REDIS_URL environment variable",
            "category": "other",
            "fix": "- Set REDIS_URL in environment",
            "is_flaky": False,
            "confidence": 0.85,
        }
        _, mock_post = self._run(analysis=analysis)
        body = mock_post.call_args[0][4]
        assert "REDIS_URL" in body

    def test_a_model_flaky_guess_is_not_shown(self):
        """Nothing in one failed run shows flakiness; the model's guess was
        published as "Possibly flaky — this might pass on re-run"."""
        analysis = {
            "root_cause": "Race condition in test setup",
            "category": "test_failure",
            "fix": "- Add sleep or use proper test fixtures",
            "is_flaky": True,
        }
        _, mock_post = self._run(analysis=analysis)
        body = mock_post.call_args[0][4]
        assert "flaky" not in body.lower()

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
                 patch("app.handlers.ci._ci_already_alerted", return_value=False), \
             patch("app.handlers.ci._current_ci_report", return_value=""), \
                 patch("app.ai.guarded.safe_router_ask", return_value=(analysis, meta)), \
                 patch("app.github.sticky.upsert_sticky") as mock_post:
                from app.handlers.ci import handle
                handle(_payload())
                body = mock_post.call_args[0][4]
                assert expected in body, f"Expected {expected} for category {category}"

    def test_router_exception_handled(self):
        with patch("app.handlers.ci.get_installation_token", return_value="tok"), \
             patch("app.handlers.ci.load_config", return_value=_mock_config()), \
             patch("app.handlers.ci._ci_already_alerted", return_value=False), \
             patch("app.handlers.ci._current_ci_report", return_value=""), \
             patch("app.ai.guarded.safe_router_ask",
                   side_effect=Exception("LLM timeout")), \
             patch("app.github.sticky.upsert_sticky") as mock_post:
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
             patch("app.handlers.ci._ci_already_alerted", return_value=False), \
             patch("app.handlers.ci._current_ci_report", return_value=""), \
             patch("app.ai.guarded.safe_router_ask", return_value=(analysis, meta)), \
             patch("app.github.sticky.upsert_sticky") as mock_post:
            from app.handlers.ci import handle
            handle(_payload(pr_numbers=[{"number": 42}]))
            assert mock_post.call_args[0][1] == 42  # V7: upsert_sticky(repo, pr, ...)


# ── Failure pattern tracking tests ───────────────────────────────────────────

class TestFailurePatternTracking:
    # ci.py imports get_redis via `from app.core.redis_client import get_redis`
    # inside the function body, so we patch at the source module.

    def test_track_increments_counter(self):
        fake_redis = MagicMock()
        fake_redis.incr.return_value = 1
        with patch("app.core.redis_client.get_redis", return_value=fake_redis):
            from app.handlers.ci import _track_failure_pattern
            _track_failure_pattern("org/repo", 1, "pytest", "import error")
            fake_redis.incr.assert_called_once()

    def test_third_failure_returns_true(self):
        fake_redis = MagicMock()
        fake_redis.incr.return_value = 3
        with patch("app.core.redis_client.get_redis", return_value=fake_redis):
            from app.handlers.ci import _track_failure_pattern
            result = _track_failure_pattern("org/repo", 1, "pytest", "error")
            assert result is True

    def test_first_failure_returns_false(self):
        fake_redis = MagicMock()
        fake_redis.incr.return_value = 1
        with patch("app.core.redis_client.get_redis", return_value=fake_redis):
            from app.handlers.ci import _track_failure_pattern
            result = _track_failure_pattern("org/repo", 1, "pytest", "error")
            assert result is False


class TestCiAudit2:
    """Found by the 2026-10-08 audit."""

    def _run(self, payload, analysis):
        with patch("app.handlers.ci.get_installation_token", return_value="tok"), \
             patch("app.handlers.ci.load_config", return_value=_mock_config()), \
             patch("app.handlers.ci._ci_already_alerted", return_value=False), \
             patch("app.handlers.ci._current_ci_report", return_value=""), \
             patch("app.handlers.ci._release_ci_alert") as release, \
             patch("app.ai.guarded.safe_router_ask", return_value=(analysis, _meta())), \
             patch("app.github.sticky.upsert_sticky") as post:
            from app.handlers.ci import handle

            handle(payload)
        return post, release

    def test_null_output_fields_do_not_crash(self):
        payload = _payload(output={"title": None, "summary": None, "text": None})
        analysis = {"root_cause": "network", "category": "other", "fix": "- retry"}
        with patch("app.handlers.ci._job_log_tail", return_value="E   ConnectionError: refused"):
            post, _ = self._run(payload, analysis)
        post.assert_called_once()

    def test_no_output_and_no_log_posts_no_invented_cause(self):
        """Asked for the "exact reason" of a failure it could see nothing of,
        the model named one from the check's name alone."""
        payload = _payload(output={"title": None, "summary": None, "text": None})
        analysis = {"root_cause": "ImportError in tests/test_cfg.py", "category": "test_failure", "fix": "- x"}
        with patch("app.handlers.ci._job_log_tail", return_value=""):
            post, release = self._run(payload, analysis)
        post.assert_not_called()
        release.assert_called_once()

    def test_a_cause_is_confirmed_only_by_a_line_in_the_log(self):
        log_text = "collected 12 items\nE   ImportError: cannot import name 'make_token'\nFAILED"
        grounded = {"root_cause": "make_token was removed", "evidence": "ImportError: cannot import name 'make_token'",
                    "category": "test_failure", "fix": "- restore it"}
        with patch("app.handlers.ci._job_log_tail", return_value=log_text):
            post, _ = self._run(_payload(), grounded)
        assert "**Likely cause:**" in post.call_args[0][4]

        invented = {**grounded, "evidence": "ModuleNotFoundError: no module named jwt"}
        with patch("app.handlers.ci._job_log_tail", return_value=log_text):
            post, _ = self._run(_payload(), invented)
        assert "not confirmed by the log" in post.call_args[0][4]

    def test_a_second_failing_check_adds_a_section_and_an_old_commit_is_dropped(self):
        from app.handlers.ci import _merge_ci_report

        first = _merge_ci_report("", "abc1234", "lint", "LINT SECTION")
        both = _merge_ci_report(first, "abc1234", "tests", "TESTS SECTION")
        assert "LINT SECTION" in both and "TESTS SECTION" in both
        newer = _merge_ci_report(both, "def5678", "tests", "NEW TESTS")
        assert "LINT SECTION" not in newer and "NEW TESTS" in newer

    def test_a_fix_given_as_a_list_is_rendered(self):
        analysis = {"root_cause": "bad import", "category": "build_error",
                    "fix": ["Install the missing package", "- Pin its version"], "confidence": 0.9}
        post, _ = self._run(_payload(), analysis)
        body = post.call_args[0][4]
        assert "- Install the missing package" in body and "- Pin its version" in body

    def test_is_flaky_false_as_a_string_is_false(self):
        analysis = {"root_cause": "x", "category": "other", "fix": "- y", "is_flaky": "false", "confidence": 0.9}
        post, _ = self._run(_payload(), analysis)
        assert "Possibly flaky" not in post.call_args[0][4]


class TestAnUnreadableReportIsNotOverwritten:
    """The existing report was read with every error turned into "" — so a
    502 on that read merged this check's section into nothing and the upsert
    erased the sections other failing checks had written."""

    def _run(self, report_error):
        with patch("app.handlers.ci.get_installation_token", return_value="tok"), \
             patch("app.handlers.ci.load_config", return_value=_mock_config()), \
             patch("app.handlers.ci._ci_already_alerted", return_value=False), \
             patch("app.handlers.ci._release_ci_alert") as release, \
             patch("app.handlers.ci._job_log_tail", return_value="E   ImportError: x"), \
             patch("app.ai.guarded.safe_router_ask", return_value=(
                 {"root_cause": "x", "evidence": "ImportError: x", "category": "other", "fix": "- y"},
                 _meta())), \
             patch("app.github.sticky.gh_get", side_effect=report_error), \
             patch("app.github.sticky.upsert_sticky") as post:
            from app.handlers.ci import handle

            try:
                handle(_payload())
                raised = None
            except Exception as e:
                raised = e
        return post, release, raised

    def test_a_502_is_raised_for_retry_and_nothing_is_written(self):
        from app.github.client import GitHubError

        post, release, raised = self._run(GitHubError("bad gateway", 502))
        assert isinstance(raised, GitHubError)
        post.assert_not_called()
        release.assert_called_once()

    def test_a_403_writes_nothing_and_is_not_retried(self):
        from app.github.client import GitHubError

        post, release, raised = self._run(GitHubError("forbidden", 403))
        assert raised is None
        post.assert_not_called()
        release.assert_called_once()


class TestEvidenceMustBeSpecific:
    """Round-4 audit M4: evidence that is in every failed log confirmed any
    cause, and a cause naming a test the log never mentions was "Likely"."""

    def _body(self, analysis):
        return TestFailureAnalysis()._run(analysis=analysis)[1].call_args[0][4]

    def test_exit_code_line_does_not_confirm_a_cause(self):
        body = self._body({
            "root_cause": "The login test fails", "category": "test_failure", "fix": "- x",
            "evidence": "3 tests failed",
        })
        assert "not confirmed by the log" in body
        assert "**Likely cause:**" not in body

    def test_a_cause_naming_a_test_absent_from_the_log_is_not_confirmed(self):
        body = self._body({
            "root_cause": "tests/test_billing.py::test_refund_partial fails",
            "category": "test_failure", "fix": "- x",
            "evidence": "FAILED tests/test_auth.py::test_login",
        })
        assert "not confirmed by the log" in body

    def test_a_specific_line_naming_the_cause_confirms_it(self):
        body = self._body({
            "root_cause": "test_login in tests/test_auth.py fails", "category": "test_failure",
            "fix": "- x", "evidence": "FAILED tests/test_auth.py::test_login",
        })
        assert "**Likely cause:**" in body


def test_the_recurring_failure_count_is_per_pr(fake_redis):
    """Round-4 audit M6: "tests" failing on three unrelated PRs told the third
    PR's author their failure was persistent, "not a flake"."""
    from app.handlers.ci import _track_failure_pattern

    assert not _track_failure_pattern("o/r", 1, "tests", "a")
    assert not _track_failure_pattern("o/r", 2, "tests", "b")
    assert not _track_failure_pattern("o/r", 3, "tests", "c")
    assert not _track_failure_pattern("o/r", 3, "tests", "c")
    assert _track_failure_pattern("o/r", 3, "tests", "c")
