"""Audit round 3: commands rendered defaults as verdicts the model never gave."""

from unittest.mock import MagicMock, patch

from app.ai.validator import flag, verdict


def test_verdict_accepts_only_allowed_answers():
    assert verdict({"h": "Good"}, "h", {"good"}) == "good"
    assert verdict({"h": "none"}, "h", {"good"}) is None
    assert verdict({}, "h", {"good"}) is None
    assert verdict({"h": 3}, "h", {"good"}) is None


def test_flag_reads_strings_as_their_meaning():
    assert flag({"m": "false"}, "m") is False
    assert flag({"m": "yes"}, "m") is True
    assert flag({"m": None}, "m") is None


def _impact(answer, files):
    from app.handlers.comments import reviewer

    with (
        patch("app.github.helpers.pr_files", return_value=files),
        patch("app.github.helpers.repo_file_context", return_value={}),
        patch("app.ai.guarded.guarded_ask", return_value=(answer, MagicMock())),
    ):
        return reviewer.cmd_impact("o/r", 1, {"pull_request": {}}, "t")


class TestImpact:
    MIGRATION = [{"filename": "db/migrations/0042_drop_users.sql", "additions": 3, "deletions": 0}]

    def test_a_migration_file_is_a_migration_whatever_the_model_says(self):
        out = _impact({"summary": "Drops a table.", "requires_migration": False}, self.MIGRATION)
        assert "⚠️ Yes" in out and "0042_drop_users.sql" in out

    def test_no_assessment_is_not_published_as_low_risk(self):
        out = _impact({"timeout": 30}, [{"filename": "app/x.py"}])
        assert "no assessment" in out and "Low" not in out

    def test_a_missing_field_reads_not_given(self):
        out = _impact({"summary": "Adds an endpoint."}, [{"filename": "app/x.py"}])
        assert "Not given" in out


def test_perf_does_not_default_to_acceptable():
    from app.handlers.comments import generator

    with patch.object(generator, "guarded_ask", return_value=({"timeout": 30}, MagicMock())):
        out = generator.cmd_perf("ctx")
    assert "Acceptable" not in out and "no assessment" in out


class TestArch:
    def _run(self, answer, tree=("app/a.py", "app/b.py")):
        from app.handlers.comments import generator

        files = [{"filename": "app/a.py", "patch": "@@ -1 +1 @@\n-a\n+b\n"}]
        with (
            patch("app.github.helpers.pr_files", return_value=files),
            patch("app.github.helpers.repo_file_context", return_value={"files": list(tree)}),
            patch.object(generator, "guarded_ask", return_value=(answer, MagicMock())) as ask,
        ):
            out = generator.cmd_arch("o/r", 1, {"pull_request": {}}, "t")
        return out, ask

    def test_the_model_is_shown_the_diff_not_only_names(self):
        _out, ask = self._run({"health": "good", "summary": "s"})
        assert "+b" in ask.call_args[0][1]

    def test_a_violation_at_a_location_that_does_not_exist_is_dropped(self):
        out, _ = self._run(
            {
                "health": "needs_work",
                "summary": "s",
                "violations": [
                    {"type": "god_class", "location": "app/ghost.py", "description": "invented"},
                    {"type": "tight_coupling", "location": "app/a.py:12", "description": "real"},
                ],
            }
        )
        assert "invented" not in out and "real" in out
        assert "1 reported issue(s) named a location that does not exist" in out

    def test_no_assessment_is_not_good_health(self):
        out, _ = self._run({"timeout": 30})
        assert "Good" not in out and "no assessment" in out


class TestRelease:
    def test_next_version_follows_the_commits(self):
        from app.handlers.comments.publisher import next_version

        assert next_version("v1.4.2", ["fix typo in README"]) == "v1.4.3"
        assert next_version("v1.4.2", ["feat(api): add x", "fix: y"]) == "v1.5.0"
        assert next_version("v1.4.2", ["feat!: drop v1 API"]) == "v2.0.0"
        assert next_version("v1.4.2", ["refactor: z\n\nBREAKING CHANGE: config keys renamed"]) == "v2.0.0"

    def _run(self, answer, since):
        from app.handlers.comments import _cmd_release

        def gh(path, token):
            if "/tags" in path:
                return [{"name": "v1.4.2"}]
            return {"commits": since, "total_commits": len(since)}

        with (
            patch("app.handlers.comments.router.ask", return_value=(answer, MagicMock())),
            patch("app.handlers.comments.gh_get", side_effect=gh),
            patch("app.handlers.comments.gh_post", return_value={"html_url": "u"}) as post,
        ):
            out = _cmd_release("o/r", "t", "a")
        return out, post.call_args[0][2]

    def test_the_model_cannot_invent_a_major_release_or_breaking_changes(self):
        out, body = self._run(
            {"version": "v3.0.0", "title": "Major platform rewrite", "breaking_changes": ["Removed /v1 API"],
             "release_notes": "Big"},
            [{"commit": {"message": "fix typo in README"}}],
        )
        assert body["tag_name"] == "v1.4.3"
        assert "Removed /v1 API" not in out and "Removed /v1 API" not in body["body"]
        assert "fix typo in README" in body["body"]

    def test_an_unusable_answer_drafts_from_the_commits_only(self):
        out, body = self._run({"raw": "Sorry, I can't"}, [{"commit": {"message": "fix: y"}}])
        assert body["tag_name"] == "v1.4.3"
        assert "fix: y" in body["body"] and "Sorry" not in body["body"]
        assert "commits only" in out
