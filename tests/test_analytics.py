"""
tests/test_analytics.py
Sprint 6: Tests for app/core/analytics.py
"""
import sys
import os
from unittest.mock import patch, MagicMock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestAnalyticsRecording:

    def test_record_command_used_no_crash(self):
        from app.core.analytics import record_command_used
        with patch("app.core.analytics._incr"):
            record_command_used("test/repo", "/fix")

    def test_record_pr_merged_no_crash(self):
        from app.core.analytics import record_pr_merged
        with patch("app.core.analytics._incr"), patch("app.core.analytics._lpush"):
            record_pr_merged("test/repo", 42, 24.5)

    def test_record_issue_closed_no_crash(self):
        from app.core.analytics import record_issue_closed
        with patch("app.core.analytics._incr"), patch("app.core.analytics._lpush"):
            record_issue_closed("test/repo", 7, 48.0)

    def test_nothing_unrecorded_is_reported(self):
        """record_bot_action and record_review_score had no callers, so the
        report showed "Total actions: 0" and graded nothing. Both are gone."""
        from app.core import analytics
        assert not hasattr(analytics, "record_bot_action")
        assert not hasattr(analytics, "record_review_score")


class TestAnalyticsHelpers:

    def test_avg_empty_list(self):
        from app.core.analytics import _avg
        assert _avg([]) == 0.0

    def test_avg_values(self):
        from app.core.analytics import _avg
        assert _avg([10.0, 20.0, 30.0]) == 20.0

    def test_today_format(self):
        from app.core.analytics import _today
        today = _today()
        assert len(today) == 10
        assert today.count("-") == 2

    def test_week_format(self):
        from app.core.analytics import _week
        week = _week()
        assert "W" in week
        assert "-" in week


class TestWeeklyReport:

    def _mock_redis_empty(self):
        r = MagicMock()
        r.get.return_value = None
        r.lrange.return_value = []
        r.keys.return_value = []
        return r

    def test_get_weekly_report_structure(self):
        from app.core.analytics import get_weekly_report
        mock_r = self._mock_redis_empty()
        with patch("app.core.redis_client.get_redis", return_value=mock_r):
            report = get_weekly_report("test/repo")
        assert "repo" in report
        assert "prs" in report
        assert "issues" in report
        assert "bot_usage" in report
        assert "code_quality" not in report, "graded from scores nothing records"

    def test_report_repo_name_correct(self):
        from app.core.analytics import get_weekly_report
        mock_r = self._mock_redis_empty()
        with patch("app.core.redis_client.get_redis", return_value=mock_r):
            report = get_weekly_report("myorg/myrepo")
        assert report["repo"] == "myorg/myrepo"

    def test_report_defaults_to_zero(self):
        from app.core.analytics import get_weekly_report
        mock_r = self._mock_redis_empty()
        with patch("app.core.redis_client.get_redis", return_value=mock_r):
            report = get_weekly_report("test/repo")
        assert report["prs"]["merged"] == 0
        assert report["prs"]["avg_merge_hours"] == 0.0

    def test_an_empty_store_renders_no_report(self):
        """With nothing recorded the reply is "No Data Yet" (cmd_report), not
        a table of zeros under a failing grade."""
        from app.core.analytics import format_report_comment
        mock_r = self._mock_redis_empty()
        with patch("app.core.redis_client.get_redis", return_value=mock_r):
            assert format_report_comment("test/repo") == ""

    def test_a_report_with_data_names_the_repo_and_has_no_grade(self):
        from app.core import analytics
        with patch.object(analytics, "_get_top_commands", return_value={"fix": 3}), \
             patch.object(analytics, "_get_int", return_value=0), \
             patch.object(analytics, "_get_list", return_value=[]), \
             patch("app.core.redis_client.get_redis", return_value=self._mock_redis_empty()):
            comment = analytics.format_report_comment("test/repo")
        assert "test/repo" in comment and "##" in comment
        assert "Grade" not in comment, "reviews no longer produce a score to grade"

    def test_a_redis_failure_is_raised_not_reported_as_zero(self):
        """It returned zeros, and /report said "No activity recorded"."""
        import pytest
        from app.core.analytics import AnalyticsUnavailable, get_weekly_report
        with patch("app.core.redis_client.get_redis", side_effect=Exception("Redis down")), \
             pytest.raises(AnalyticsUnavailable, match="Redis down"):
            get_weekly_report("test/repo")

    def test_the_report_command_says_it_could_not_read(self):
        from app.handlers.comments.reviewer import cmd_report
        with patch("app.core.redis_client.get_redis", side_effect=ConnectionError("refused")):
            out = cmd_report("o/r")
        assert out.startswith("## ⚠️ Report Unavailable")
        assert "not a report of zero activity" in out


class TestEveryCommandIsCounted:
    def test_commands_outside_the_old_hard_coded_list_are_shown(self, fake_redis):
        """It read fifteen names; a repo using only these was told No Data Yet."""
        from app.core import analytics
        with patch("app.core.redis_client.get_redis", return_value=fake_redis):
            for c in ("merge", "summarize", "impact", "apply", "merge"):
                analytics.record_command_used("o/r", c)
            report = analytics.get_weekly_report("o/r")
        assert report["bot_usage"]["top_commands"] == {"merge": 2, "summarize": 1, "impact": 1, "apply": 1}

    def test_the_week_is_summed_not_just_today(self, fake_redis):
        from app.core import analytics
        days = analytics._week_days()
        with patch("app.core.redis_client.get_redis", return_value=fake_redis):
            for d in days:
                fake_redis.set(f"analytics:o/r:prs_merged:{d}", "2")
            report = analytics.get_weekly_report("o/r")
        assert report["prs"]["merged"] == 2 * len(days)
