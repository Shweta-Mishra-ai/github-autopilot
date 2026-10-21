"""
Outputs that printed numbers nobody measured, or leaked what they should not.
"""

from unittest.mock import MagicMock, patch

from app.handlers.comments import reviewer as rv


class TestHealthCountsAreCounts:
    def _health(self, open_issues_count, pr_total):
        def gh(path, token):
            if path == "/repos/o/r":
                return {"open_issues_count": open_issues_count, "license": None, "description": ""}
            if path.startswith("/search/issues"):
                return {"total_count": pr_total, "items": []}
            raise AssertionError(f"unexpected call {path}")

        with patch.object(rv, "gh_get", side_effect=gh):
            return rv.cmd_health("o/r", "tok")

    def test_more_than_fifty_issues_are_not_reported_as_fifty(self):
        out = self._health(open_issues_count=340, pr_total=40)
        assert "300 open issues" in out and "40 open PRs" in out

    def test_prs_are_not_counted_as_issues(self):
        out = self._health(open_issues_count=12, pr_total=12)
        assert "✅ 0 open issues" in out


class TestCiConfidenceIsNeverInvented:
    def _ci(self, answer, context="Traceback: boom"):
        with patch("app.ai.guarded.guarded_ask", return_value=(answer, MagicMock())):
            return rv.cmd_ci(context)

    def test_no_confidence_given_prints_none(self):
        out = self._ci({"root_cause": "x", "fix": "y", "prevention": "z"})
        assert "Confidence" not in out

    def test_a_given_confidence_is_not_printed(self):
        """Round-4 audit M5: the model's rating of itself is not evidence."""
        out = self._ci({"root_cause": "x", "fix": "y", "prevention": "z", "confidence": 0.4})
        assert "40%" not in out and "self-estimate" not in out

    def test_a_cause_naming_what_the_log_lacks_is_marked(self):
        out = self._ci(
            {"root_cause": "tests/test_billing.py::test_refund fails", "fix": "y"},
            context="FAILED tests/test_orders.py::test_lookup - assert 404 == 200",
        )
        assert "Root Cause (not supported by the log)" in out
        assert "`tests/test_billing.py`" in out and "`test_refund`" in out

    def test_a_cause_the_log_shows_is_not_marked(self):
        out = self._ci(
            {"root_cause": "tests/test_orders.py::test_lookup gets a 404", "fix": "y"},
            context="FAILED tests/test_orders.py::test_lookup - assert 404 == 200",
        )
        assert "**Root Cause:**" in out and "Not in the log" not in out

    def test_metadata_only_analysis_says_so(self):
        runs = {
            "workflow_runs": [
                {"name": "CI", "head_branch": "b", "conclusion": "failure", "head_commit": None}
            ]
        }
        seen = {}

        def ask(system, user, **kw):
            seen["user"] = user
            return {"root_cause": "Unknown without the log", "fix": "f"}, MagicMock()

        with (
            patch.object(rv, "gh_get", return_value=runs),
            patch("app.ai.guarded.guarded_ask", side_effect=ask),
        ):
            out = rv.cmd_ci("", repo="o/r", token="t")
        assert "no log output" in seen["user"]
        assert "Based on the run's metadata only" in out


class TestChangelogKnowsTheDate:
    def _changelog(self, answer, message="fix: crash in the order lookup"):
        commits = [{"sha": "a", "commit": {"message": message}}]
        with (
            patch.object(rv, "gh_get", side_effect=[[], commits]),
            patch.object(rv.router, "ask_text", return_value=(answer, MagicMock())),
        ):
            return rv.cmd_changelog("o/r", "t")

    def test_the_heading_is_written_by_the_code_with_todays_date(self):
        """The model's "## [X.Y.Z] - YYYY-MM-DD" placeholder was published."""
        import datetime

        out = self._changelog("## [X.Y.Z] - YYYY-MM-DD\n### Fixed\n- Crash in the order lookup")
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        assert f"## [Unreleased] - {today}" in out
        assert "X.Y.Z" not in out and "YYYY" not in out

    def test_entries_that_match_no_commit_are_removed(self):
        """Round-4 audit M3: "OAuth2 login support" and "Order export to CSV"
        were published for a release whose only commit fixed a README typo."""
        out = self._changelog(
            "### Added\n- OAuth2 login support\n- Export to CSV\n### Fixed\n- Typo in README",
            message="fix: typo in README",
        )
        assert "Typo in README" in out
        assert "OAuth2" not in out and "CSV" not in out
        assert "2 line(s) the model wrote matched no commit" in out

    def test_nothing_matching_is_not_published_as_an_entry(self):
        out = self._changelog("### Added\n- OAuth2 login support")
        assert "No Changelog Entry Produced" in out


class TestRedisUrlNeverLogged:
    def test_only_the_host_is_logged(self):
        from app.core import redis_client

        assert redis_client._redis_host("rediss://default:s3cr3tpassw0rd@cache.example:6380") == (
            "cache.example:6380"
        )
        assert "s3cr3t" not in redis_client._redis_host("redis://u:s3cr3t@h")


def test_no_redis_url_prefix_is_logged_anywhere():
    """get_redis() was fixed to log the host only; get_redis_blocking() still
    logged redis_url[:30] — most of the password for rediss://default:…@host."""
    import pathlib
    import re

    src = pathlib.Path("app/core/redis_client.py").read_text()
    assert not re.search(r"redis_url\[", src)


class TestSelfEstimatesAreLabelled:
    """A model's confidence in itself was printed as "Confidence: 85%"."""

    def test_the_helper(self):
        from app.ai.validator import self_estimate

        assert self_estimate(0.85).startswith("Model's self-estimate: 85%")
        assert self_estimate("85").startswith("Model's self-estimate: 85%")
        assert self_estimate("high") == "" and self_estimate(None) == "" and self_estimate(7000) == ""

    def test_autofix_and_mcp_use_it(self):
        from app.handlers import autofix
        from app.mcp.handlers import _confidence_line

        body = autofix._build_pr_body({"problem": "p", "fix_description": "f", "explanation": "e",
                                       "confidence": 0.7}, 3, "t")
        assert "Model's self-estimate: 70%" in body and "Confidence: 70%" not in body
        assert _confidence_line(0.7) == "*Model's self-estimate: 70% (its own guess, not a measured probability)*"
