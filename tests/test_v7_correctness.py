"""
tests/test_v7_correctness.py — V7 Phase 1.

These tests assert on RENDERED OUTPUT, not on validator return values. That is
the discipline the existing suite lacked: 908 tests passed while the bot was
publishing "Score: 7/10 — no issues found" for reviews that never ran.
"""

from unittest.mock import MagicMock, patch

from app.ai.validator import (
    is_unusable,
    validate_code_review,
    validate_issue_triage,
    validate_pr_analysis,
)


class TestUnusableGuard:
    def test_raw_key_is_unusable(self):
        assert is_unusable({"raw": "Sorry, I cannot help with that."}) is True

    def test_error_key_is_unusable(self):
        assert is_unusable({"error": "timeout"}) is True

    def test_non_dict_is_unusable(self):
        assert is_unusable("not a dict") is True

    def test_good_payload_is_usable(self):
        assert is_unusable({"score": 8, "issues": []}) is False

    def test_code_review_marks_raw_as_degraded(self):
        out = validate_code_review({"raw": "Sorry, I cannot help."})
        assert out["_degraded"] is True

    def test_code_review_does_not_invent_a_passing_score(self):
        """The 7.0 default must never reach a renderer for unparseable output."""
        out = validate_code_review({"raw": "Sorry, I cannot help."})
        assert out["score"] is None
        assert out["issues"] == []

    def test_triage_marks_raw_as_degraded(self):
        assert validate_issue_triage({"raw": "..."})["_degraded"] is True

    def test_pr_analysis_marks_raw_as_degraded(self):
        assert validate_pr_analysis({"raw": "..."})["_degraded"] is True

    def test_good_payload_is_not_degraded(self):
        out = validate_code_review({"score": 9, "issues": [], "summary": "fine"})
        assert out.get("_degraded", False) is False


class TestTriageVocabulary:
    def test_critical_priority_survives(self):
        """Live evidence: issue #76 (a security vuln) was labelled 'priority: medium'."""
        out = validate_issue_triage(
            {
                "type": "security",
                "priority": "critical",
                "complexity": "epic",
                "time_estimate": "1-3 days",
                "welcome": "thanks",
                "labels": ["security"],
            }
        )
        assert out["priority"] == "critical"

    def test_refactor_type_survives(self):
        out = validate_issue_triage({"type": "refactor", "priority": "low", "welcome": "hi"})
        assert out["type"] == "refactor"

    def test_epic_complexity_survives(self):
        out = validate_issue_triage({"type": "bug", "complexity": "epic", "welcome": "hi"})
        assert out["complexity"] == "epic"

    def test_time_estimate_passes_through(self):
        out = validate_issue_triage(
            {"type": "bug", "time_estimate": "1-4 hours", "welcome": "hi"}
        )
        assert out["time_estimate"] == "1-4 hours"

    def test_bogus_time_estimate_is_dropped(self):
        out = validate_issue_triage(
            {"type": "bug", "time_estimate": "about a fortnight", "welcome": "hi"}
        )
        assert out["time_estimate"] == ""

    def test_unknown_priority_still_falls_back(self):
        out = validate_issue_triage({"type": "bug", "priority": "urgent-ish", "welcome": "hi"})
        assert out["priority"] == "medium"


class TestDegradedTriage:
    def test_degraded_triage_posts_no_fabricated_table(self):
        from app.handlers import issues as issues_mod

        posted = {}
        payload = {
            "action": "opened",
            "issue": {
                "number": 1,
                "title": "t",
                "body": "b",
                "user": {"login": "dev"},
            },
            "repository": {"full_name": "o/r"},
            "installation": {"id": 1},
        }
        cfg = MagicMock()
        cfg.footer = ""
        cfg.issues_enabled.return_value = True
        cfg.get.side_effect = lambda *a, **kw: kw.get("default", True)

        with (
            patch.object(issues_mod, "get_installation_token", return_value="tok"),
            patch.object(issues_mod, "load_config", return_value=cfg),
            patch.object(issues_mod, "gh_get", return_value={"language": "Python"}),
            patch.object(
                issues_mod, "gh_post", side_effect=lambda p, t, d: posted.update(body=d["body"])
            ),
            patch.object(
                issues_mod.router, "ask", return_value=({"raw": "I cannot help"}, MagicMock())
            ),
        ):
            issues_mod.handle(payload)

        body = posted.get("body", "")
        assert "**Priority**" not in body
        assert "**Complexity**" not in body


def _review_cfg():
    cfg = MagicMock()
    cfg.footer = ""
    cfg.get.return_value = 4
    return cfg


class TestReviewRendering:
    def test_verdict_is_accepted_as_input_and_normalised_to_summary(self):
        """The original bug was a name mismatch: the model answered under
        `verdict`, the renderer read `summary`, and every review shipped blank.
        The fix was to accept both as INPUT — not to emit both. The duplicate
        output field carried a comment claiming app/mcp/handlers.py and evals/
        read it; neither ever did, so it was removed in v7.2.0 and this pins
        the half that actually mattered."""
        from_summary = validate_code_review({"score": 8, "issues": [], "summary": "Looks solid."})
        from_verdict = validate_code_review({"score": 8, "issues": [], "verdict": "Looks solid."})

        assert from_summary["summary"] == "Looks solid."
        assert from_verdict["summary"] == "Looks solid."
        assert "verdict" not in from_verdict

    def test_rendered_review_contains_model_summary(self):
        """Regression: renderer read 'summary', validator only returned 'verdict'."""
        from app.handlers import pull_request as pr_mod

        files = [
            {
                "filename": "app/x.py",
                "patch": "@@ -1,1 +1,1 @@\n-x = 0\n+x = 1\n",
                "additions": 1,
                "deletions": 1,
            }
        ]
        llm = (
            {
                "files": [
                    {
                        "file": "app/x.py",
                        "score": 8,
                        "issues": [],
                        "summary": "Change is well scoped.",
                    }
                ]
            },
            MagicMock(),
        )

        with patch.object(pr_mod.router, "ask", return_value=llm):
            md, _inline = pr_mod._review_code(
                {"head": {"sha": "abc"}},
                "o/r",
                1,
                files,
                "t",
                _review_cfg(),
                MagicMock(),
                "",
                MagicMock(),
            )

        assert "Change is well scoped." in md

    def test_degraded_file_is_skipped_not_rendered_as_clean(self):
        from app.handlers import pull_request as pr_mod

        files = [{"filename": "app/x.py", "patch": "@@ -1,1 +1,1 @@\n-x = 0\n+x = 1\n"}]
        llm = ({"raw": "Sorry, I cannot help with that."}, MagicMock())

        with patch.object(pr_mod.router, "ask", return_value=llm):
            md, inline = pr_mod._review_code(
                {"head": {"sha": "abc"}},
                "o/r",
                1,
                files,
                "t",
                _review_cfg(),
                MagicMock(),
                "",
                MagicMock(),
            )

        assert md == ""
        assert inline == []
        assert "Score: 7" not in md
        assert "No issues found" not in md
        assert "Score: None" not in md
