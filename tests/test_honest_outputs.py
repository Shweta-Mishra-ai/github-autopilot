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

    def test_a_given_confidence_is_printed(self):
        out = self._ci({"root_cause": "x", "fix": "y", "prevention": "z", "confidence": 0.4})
        assert "Confidence: 40%" in out

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
    def test_the_prompt_carries_todays_date(self):
        import datetime

        seen = {}

        def ask_text(system, user, **kw):
            seen["user"] = user
            return "## [1.0.1]", MagicMock()

        commits = [{"sha": "a", "commit": {"message": "fix: x"}}]
        with (
            patch.object(rv, "gh_get", side_effect=[[], commits]),
            patch.object(rv.router, "ask_text", side_effect=ask_text),
        ):
            rv.cmd_changelog("o/r", "t")
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        assert today in seen["user"]


class TestRedisUrlNeverLogged:
    def test_only_the_host_is_logged(self):
        from app.core import redis_client

        assert redis_client._redis_host("rediss://default:s3cr3tpassw0rd@cache.example:6380") == (
            "cache.example:6380"
        )
        assert "s3cr3t" not in redis_client._redis_host("redis://u:s3cr3t@h")
