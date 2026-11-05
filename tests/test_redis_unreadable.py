"""
tests/test_redis_unreadable.py

"Redis could not be read" was reported as "there is nothing there". These
are the places where the difference changes what a person is told, or what
the bot does next.
"""

from __future__ import annotations

from unittest.mock import patch


def _down(*_a, **_k):
    raise ConnectionError("Error 111 connecting to redis:6379. Connection refused.")


class TestRollback:
    def test_listing_says_unavailable_not_nothing_recorded(self):
        from app.handlers.comments.publisher import cmd_rollback

        with patch("app.core.redis_client.get_redis", side_effect=_down):
            out = cmd_rollback("o/r", 1, "t", "", "maint")
        assert out.startswith("## ⚠️ Snapshots Unavailable")
        assert "Nothing has been recorded" not in out

    def test_a_numbered_rollback_says_unavailable_not_not_found(self):
        from app.handlers.comments.publisher import cmd_rollback

        with patch("app.core.redis_client.get_redis", side_effect=_down):
            out = cmd_rollback("o/r", 1, "t", "2 confirm", "maint")
        assert out.startswith("## ⚠️ Snapshots Unavailable")
        assert "nothing was rolled back" in out

    def test_one_malformed_snapshot_does_not_hide_the_others(self, fake_redis):
        from app.core import snapshot

        with patch("app.core.redis_client.get_redis", return_value=fake_redis):
            fake_redis.lpush("snapshot_index:o/r", "bad", "good")
            fake_redis.set("snapshot:o/r:good", '{"trigger": "activity", "timestamp": "2026-10-14T10:00"}')
            fake_redis.set("snapshot:o/r:bad", "{not json")
            listed = snapshot.list_snapshots("o/r")
        assert [s["id"] for s in listed] == ["good"]


class TestInstallations:
    def test_a_read_error_does_not_forget_a_quiet_repository(self, fake_redis):
        """One timeout during the maintenance pass dropped quiet repositories
        from every later scheduled security sweep until their next webhook."""
        import app.core.installations as inst

        with patch("app.core.redis_client.get_redis", return_value=fake_redis):
            inst.remember_installation("quiet/repo", 42)
            inst.remember_installation("busy/repo", 43)
            real_get = fake_redis.get

            def flaky(key):
                if key.endswith("quiet/repo"):
                    raise TimeoutError("Timeout reading from socket")
                return real_get(key)

            fake_redis.get = flaky
            first = inst.known_installations()
            fake_redis.get = real_get
            second = inst.known_installations()
        assert first == {"busy/repo": 43}
        assert second == {"quiet/repo": 42, "busy/repo": 43}

    def test_an_expired_entry_is_still_pruned(self, fake_redis):
        import app.core.installations as inst

        with patch("app.core.redis_client.get_redis", return_value=fake_redis):
            inst.remember_installation("gone/repo", 7)
            fake_redis.delete(inst._entry_key("gone/repo"))
            assert inst.known_installations() == {}
            assert "gone/repo" not in {
                (m.decode() if isinstance(m, bytes) else m)
                for m in fake_redis.smembers(inst._INDEX_KEY) or set()
            }


class TestSummarizeReadsTheEnd:
    """It read the first 50 comments, oldest first: on a longer thread the
    resolution was never fetched and the summary described the opening."""

    def _run(self, total):
        from app.handlers.comments import reviewer

        def get(path, token):
            if path.endswith("/issues/9"):
                return {"title": "t", "comments": total}
            page = int(path.rsplit("page=", 1)[1])
            start = (page - 1) * 100
            return [{"user": {"login": "u"}, "body": f"c{i}"} for i in range(start, min(total, start + 100))]

        seen = {}

        def summarize(comments, issue, omitted=0):
            seen.update(n=len(comments), omitted=omitted, last=comments[-1]["body"])
            return "summary"

        with patch.object(reviewer, "gh_get", side_effect=get), \
             patch("app.intelligence.summarizer.summarize_issue_thread", side_effect=summarize):
            out = reviewer.cmd_summarize("o/r", 9, "t")
        return out, seen

    def test_a_long_thread_includes_its_latest_comment(self):
        out, seen = self._run(1000)
        assert seen["last"] == "c999"
        assert seen["omitted"] == 1000 - seen["n"]
        assert f"Read {seen['n']} of 1000 comments" in out

    def test_a_short_thread_is_read_whole_and_says_nothing_extra(self):
        out, seen = self._run(40)
        assert seen == {"n": 40, "omitted": 0, "last": "c39"}
        assert "Read " not in out
