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
