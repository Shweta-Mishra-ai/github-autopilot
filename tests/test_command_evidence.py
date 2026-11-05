"""
tests/test_command_evidence.py

Round-4 accuracy audit: command answers were published without being checked
against what the model was shown — "Coverage is 42%", a gap in
"OrderExporter.to_csv" for a diff without it, "~50x faster", a billing file
that does not exist, a migration for a PR with no migration file.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.handlers.comments import evidence
from app.handlers.comments import generator as G
from app.handlers.comments.dispatcher import DIFF_MARKER

CODE = (
    f"PR #7: Add order lookup\n{DIFF_MARKER}\n--- app/orders.py\n"
    "+def get_order(order_id):\n+    for o in orders:\n+        if o.id == order_id:\n"
    "+            return o\n"
)


def _ask(answer):
    return patch.object(G, "guarded_ask", return_value=(answer, MagicMock()))


class TestEvidenceHelpers:
    def test_names_inside_a_path_are_reported_once(self):
        assert evidence.code_names("tests/test_billing.py::test_refund fails") == [
            "tests/test_billing.py",
            "test_refund",
        ]

    def test_missing_from(self):
        assert evidence.missing_from("get_order loops; OrderExporter.to_csv too", CODE) == [
            "OrderExporter.to_csv",
            "to_csv",
            "OrderExporter",
        ]
        assert evidence.missing_from("plain prose with no code in it", CODE) == []

    def test_unmeasured_numbers(self):
        for claim in ("Coverage is 42%.", "~50x faster", "3x faster on big inputs", "adds 200 ms"):
            assert evidence.has_unmeasured(claim), claim
        for fine in ("O(n²) per lookup", "Python 3.12", "line 14"):
            assert not evidence.has_unmeasured(fine), fine

    def test_changelog_entry_keeps_only_what_commits_back(self):
        entry, dropped = evidence.changelog_entry(
            "## [X.Y.Z] - YYYY-MM-DD\n### Added\n- OAuth2 login\n### Fixed\n- Typo in the README",
            ["fix: typo in README"],
            "2026-10-21",
        )
        assert entry == "## [Unreleased] - 2026-10-21\n### Fixed\n- Typo in the README"
        assert dropped == 1


class TestGaps:
    def test_an_unmeasured_coverage_figure_and_an_absent_function_are_not_shown(self):
        answer = {
            "coverage_assessment": "Coverage is 42%. The lookup has no error-path test.",
            "gaps": [
                {"area": "get_order when no order matches", "risk": "high", "suggested_test": "a"},
                {"area": "OrderExporter.to_csv", "risk": "medium", "suggested_test": "b"},
            ],
        }
        with _ask(answer):
            out = G.cmd_gaps(CODE)
        assert "42%" not in out
        assert "The lookup has no error-path test." in out
        assert "get_order when no order matches" in out
        assert "OrderExporter" not in out
        assert "1 reported gap(s) named code that is not in the diff" in out

    def test_without_code_nothing_is_hidden(self):
        """On an issue there is no diff to check against; ground_answer says so."""
        answer = {"coverage_assessment": "", "gaps": [{"area": "OrderExporter.to_csv"}]}
        with _ask(answer):
            out = G.cmd_gaps("Issue: exports break")
        assert "OrderExporter.to_csv" in out


class TestPerf:
    def test_speedups_and_absent_locations_are_not_shown(self):
        answer = {
            "overall_rating": "slow",
            "complexity_issues": [
                {"location": "get_order", "current_complexity": "O(n)", "issue": "Linear scan",
                 "fix": "index by id", "improvement": "~50x faster on 10k orders"},
                {"location": "OrderCache.warm", "current_complexity": "O(n²)",
                 "issue": "Nested loop", "fix": "x", "improvement": "big"},
            ],
            "quick_wins": ["Cache the lookup (3x faster)", "Index orders by id"],
            "summary": "",
        }
        with _ask(answer):
            out = G.cmd_perf(CODE)
        assert "Linear scan" in out
        assert "50x" not in out and "3x" not in out
        assert "Index orders by id" in out
        assert "OrderCache" not in out
        assert "1 reported issue(s) named code that is not in the diff" in out
        assert "**Summary:**" not in out, "an empty summary was rendered as a label"


class TestImpact:
    def _impact(self, answer, files=({"filename": "app/orders.py"},), tree=("app/orders.py",)):
        from app.handlers.comments import impact as im

        def gh(path, tok, *a, **k):
            if "/git/trees/" in path:
                return {"tree": [{"path": p, "type": "blob"} for p in tree], "truncated": False}
            return list(files)

        with (
            patch.object(im, "gh_get", side_effect=gh),
            patch("app.ai.guarded.guarded_ask", return_value=(answer, MagicMock())),
        ):
            return im.cmd_impact("o/r", 7, {"pull_request": {}}, "t")

    BASE = {"summary": "Changes the order lookup.", "affected_systems": ["orders"],
            "breaking_change_risk": "low", "requires_migration": False,
            "review_priority": "low", "notes": ""}

    def test_a_migration_guess_without_a_migration_file_is_shown_as_a_guess(self):
        out = self._impact({**self.BASE, "requires_migration": True,
                            "notes": "Requires a DB migration of the orders table. Check callers."})
        assert "No migration files in this PR (the model guessed one is needed)" in out
        assert "Requires a DB migration" not in out
        assert "Check callers." in out

    def test_priority_follows_a_higher_breaking_change_risk(self):
        out = self._impact({**self.BASE, "breaking_change_risk": "high"})
        assert "`high (breaking-change risk is high)`" in out

    def test_a_file_that_does_not_exist_is_flagged(self):
        out = self._impact({**self.BASE, "summary": "Touches app/billing/worker.py."})
        assert "Not in this repository: `app/billing/worker.py`" in out


class TestNobodyIsMentionedByAModel:
    """Round-4 audit M8: "I've assigned @bob" and "cc @security-team" in model
    output notified those people; a planted mention in an issue was echoed."""

    def test_quiet_mentions_leaves_code_alone(self):
        from app.core.sanitizer import quiet_mentions

        text = "Ping @bob and @org/team.\n```python\n@app.route('/')\n```\nUse `@cache` here."
        assert quiet_mentions(text) == (
            "Ping `@bob` and `@org/team`.\n```python\n@app.route('/')\n```\nUse `@cache` here."
        )
        assert quiet_mentions("mail me at a@b.com") == "mail me at a@b.com"

    def test_a_command_reply_mentions_only_the_requester(self):
        from app.handlers.comments import service as svc

        posted = []
        with (
            patch.object(svc, "get_installation_token", return_value="t"),
            patch.object(svc, "load_config", return_value=MagicMock(footer="")),
            patch.object(svc, "check_user_rate_limit", return_value=True),
            patch.object(svc, "check_command_permission", return_value=(True, "")),
            patch.object(svc, "post_comment", side_effect=lambda *a, **k: posted.append(a[4])),
            patch.object(svc, "_dispatch", return_value="Fixed — cc @bob for review."),
        ):
            svc.handle_comment_event({
                "action": "created", "comment": {"body": "/explain"},
                "issue": {"number": 3, "title": "t", "body": "", "user": {"login": "amy"}},
                "repository": {"full_name": "o/r"}, "installation": {"id": 1},
                "sender": {"login": "maint"},
            })
        assert posted, "nothing was posted"
        assert "`@bob`" in posted[-1]
        assert "requested by @maint" in posted[-1]

    def test_the_triage_welcome_claims_nothing_it_cannot_know(self):
        from app.ai.validator import validate_issue_triage

        r = validate_issue_triage({
            "type": "bug", "priority": "high",
            "welcome": "Thanks! This is a duplicate of #42, which was fixed in v2.3.1. "
                       "I've assigned @bob. Could you share the stack trace?",
        })
        assert r["welcome"] == "Thanks! Could you share the stack trace?"


def test_a_kind_label_contradicting_the_type_is_not_applied():
    """Round-4 audit L3: an issue typed "feature" was labelled "bug"."""
    from app.handlers.issues import triage_labels

    assert triage_labels(["bug", "help wanted"], None, "feature") == ["help wanted 🙏"]
    assert triage_labels(["bug", "enhancement"], None, "bug") == ["bug 🐛"]
    assert triage_labels([], "priority: high 🔥", "security") == ["priority: high 🔥"]
    assert triage_labels(["bug"], None, None) == ["bug 🐛"]


def test_severity_words_models_use_are_mapped_not_demoted():
    """Round-4 audit L2: "P0", "fatal", "security", "bug" became minor."""
    from app.ai.validator import validate_code_review

    def sev(word):
        r = validate_code_review({"summary": "s", "issues": [{"severity": word, "issue": "x"}]})
        return r["issues"][0]["severity"]

    assert [sev(w) for w in ("P0", "fatal", "urgent")] == ["critical"] * 3
    assert [sev(w) for w in ("p1", "security", "bug", "serious")] == ["major"] * 4
    assert [sev(w) for w in ("nitpick", "trivial", "optional")] == ["nit"] * 3
