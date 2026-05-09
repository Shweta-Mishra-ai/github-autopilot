"""
tests/test_analytics.py
Sprint 6: Tests for app/core/analytics.py and app/core/cache.py
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

    def test_record_bot_action_no_crash(self):
        from app.core.analytics import record_bot_action
        with patch("app.core.analytics._incr"):
            record_bot_action("test/repo", "comment_posted")

    def test_record_pr_merged_no_crash(self):
        from app.core.analytics import record_pr_merged
        with patch("app.core.analytics._incr"), patch("app.core.analytics._lpush"):
            record_pr_merged("test/repo", 42, 24.5)

    def test_record_issue_closed_no_crash(self):
        from app.core.analytics import record_issue_closed
        with patch("app.core.analytics._incr"), patch("app.core.analytics._lpush"):
            record_issue_closed("test/repo", 7, 48.0)

    def test_record_review_score_no_crash(self):
        from app.core.analytics import record_review_score
        with patch("app.core.analytics._lpush"):
            record_review_score("test/repo", 8.5)


class TestAnalyticsHelpers:

    def test_avg_empty_list(self):
        from app.core.analytics import _avg
        assert _avg([]) == 0.0

    def test_avg_values(self):
        from app.core.analytics import _avg
        assert _avg([10.0, 20.0, 30.0]) == 20.0

    def test_score_to_grade_A(self):
        from app.core.analytics import _score_to_grade
        assert _score_to_grade(9.5) == "A"

    def test_score_to_grade_B(self):
        from app.core.analytics import _score_to_grade
        assert _score_to_grade(8.0) == "B"

    def test_score_to_grade_C(self):
        from app.core.analytics import _score_to_grade
        assert _score_to_grade(7.0) == "C"

    def test_score_to_grade_D(self):
        from app.core.analytics import _score_to_grade
        assert _score_to_grade(5.0) == "D"

    def test_score_to_grade_F(self):
        from app.core.analytics import _score_to_grade
        assert _score_to_grade(3.0) == "F"

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
        assert "code_quality" in report
        assert "bot_usage" in report

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
        assert report["prs"]["merged_today"] == 0
        assert report["prs"]["avg_merge_hours"] == 0.0

    def test_format_report_comment_contains_repo(self):
        from app.core.analytics import format_report_comment
        mock_r = self._mock_redis_empty()
        with patch("app.core.redis_client.get_redis", return_value=mock_r):
            comment = format_report_comment("test/repo")
        assert "test/repo" in comment
        assert "##" in comment

    def test_format_report_comment_has_grade(self):
        from app.core.analytics import format_report_comment
        mock_r = self._mock_redis_empty()
        with patch("app.core.redis_client.get_redis", return_value=mock_r):
            comment = format_report_comment("test/repo")
        assert "Grade" in comment

    def test_redis_failure_returns_defaults(self):
        from app.core.analytics import get_weekly_report
        with patch("app.core.redis_client.get_redis", side_effect=Exception("Redis down")):
            report = get_weekly_report("test/repo")
        assert report["prs"]["merged_today"] == 0


class TestCache:

    def test_cache_hit_returns_data(self):
        from app.core.cache import _get
        mock_r = MagicMock()
        mock_r.get.return_value = b'{"key": "value"}'
        mock_r.incr = MagicMock()
        mock_r.expire = MagicMock()
        with patch("app.core.redis_client.get_redis", return_value=mock_r):
            result = _get("test_key")
        assert result == {"key": "value"}

    def test_cache_miss_returns_none(self):
        from app.core.cache import _get
        mock_r = MagicMock()
        mock_r.get.return_value = None
        with patch("app.core.redis_client.get_redis", return_value=mock_r):
            result = _get("missing_key")
        assert result is None

    def test_make_key_is_deterministic(self):
        from app.core.cache import _make_key
        k1 = _make_key("/repos/test/repo", "token123")
        k2 = _make_key("/repos/test/repo", "token123")
        assert k1 == k2

    def test_make_key_different_paths(self):
        from app.core.cache import _make_key
        k1 = _make_key("/repos/a/b", "token")
        k2 = _make_key("/repos/c/d", "token")
        assert k1 != k2

    def test_get_ttl_pulls(self):
        from app.core.cache import _get_ttl
        assert _get_ttl("/repos/x/pulls/1/files") == 300

    def test_get_ttl_commits(self):
        from app.core.cache import _get_ttl
        # /repos/ pattern matches first in TTL_MAP for this path
        assert _get_ttl("/repos/x/commits/abc") == 1800

    def test_get_ttl_default(self):
        from app.core.cache import _get_ttl
        # Path with no known pattern uses default
        assert _get_ttl("/unknown/endpoint/xyz") == 180

    def test_get_stats_redis_failure(self):
        from app.core.cache import get_stats
        with patch("app.core.redis_client.get_redis", side_effect=Exception("down")):
            stats = get_stats()
        assert stats["hits"] == 0
        assert stats["misses"] == 0

    def test_cached_gh_get_uses_cache(self):
        from app.core.cache import cached_gh_get
        mock_r = MagicMock()
        # Return valid JSON bytes so cache hit is triggered
        mock_r.get.return_value = b'[{"id": 1}]'
        mock_r.incr = MagicMock()
        mock_r.expire = MagicMock()
        with patch("app.core.redis_client.get_redis", return_value=mock_r):
            result = cached_gh_get("/repos/test/pulls/1/files", "token")
        assert result == [{"id": 1}]
