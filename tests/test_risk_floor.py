"""The recorded PR risk gates auto-merge; it must not rest on a model's guess.
Audit round 3: a 16-file PR granting a workflow write-all and replacing
verify(token) with pass was recorded "low" — the model had been shown no diff."""

from unittest.mock import MagicMock, patch

import pytest

from app.handlers.pull_request import analysis
from app.handlers.pull_request.risk import higher, risk_floor


def _f(name, patch_="@@ -1 +1 @@\n-a\n+b\n", **kw):
    return {"filename": name, "patch": patch_, "additions": 1, "deletions": 1, "status": "modified", **kw}


class TestRiskFloor:
    @pytest.mark.parametrize(
        "path",
        [
            ".github/workflows/ci.yml",
            "Dockerfile",
            "db/migrations/0042_drop_users.sql",
            "app/auth/jwt.py",
            "app/core/authorization.py",
            "services/session_store.py",
            "CODEOWNERS",
        ],
    )
    def test_sensitive_paths_are_high(self, path):
        assert risk_floor([_f(path)])[0] == "high"

    @pytest.mark.parametrize("path", ["requirements.txt", "package-lock.json", "pyproject.toml"])
    def test_dependency_changes_are_at_least_medium(self, path):
        assert risk_floor([_f(path)])[0] == "medium"

    def test_a_file_without_a_diff_is_at_least_medium(self):
        assert risk_floor([_f("assets/blob.bin", patch_=None)])[0] == "medium"

    def test_many_files_is_at_least_medium(self):
        assert risk_floor([_f(f"docs/p{i}.md") for i in range(25)])[0] == "medium"

    def test_tests_of_auth_code_are_not_auth_code(self):
        assert risk_floor([_f("tests/test_auth.py")])[0] == "low"

    def test_an_ordinary_change_is_low(self):
        assert risk_floor([_f("app/utils/format.py"), _f("README.md")]) == ("low", [])

    def test_higher(self):
        assert higher("low", "high") == "high" and higher("high", "low") == "high"


class TestExcerptCoversEveryFile:
    @pytest.mark.parametrize("n", [4, 12, 16, 30])
    def test_every_file_is_shown_or_named(self, n):
        files = [_f(f"app/m{i}.py", "@@ -1,3 +1,3 @@\n" + "+x = 1\n" * 40) for i in range(n)]
        text, shown, total = analysis.diff_excerpt_coverage(files, 3000)
        assert total == n
        for f in files:
            assert f["filename"] in text
        assert len(text) <= 3000
        if shown < total:
            assert "diff not shown" in text


def _run_analysis(files, model_risk="low", provider_key="groq_70b", auto_apply=True):
    payload = {
        "suggested_title": "fix: x",
        "description": "## Summary\nx",
        "risk_level": model_risk,
        "risk_reason": "small change",
        "review_focus": [],
        "confidence": 0.9,
    }
    meta = MagicMock(provider_key=provider_key)
    gate = MagicMock()
    gate.evaluate.return_value = {"auto_apply": auto_apply, "confidence_note": ""}
    recorded = {}
    with (
        patch.object(analysis.router, "ask", return_value=(payload, meta)),
        patch("app.core.guardrails.record_pr_risk", side_effect=lambda *a: recorded.update(risk=a[3])),
        patch.object(analysis, "notify_high_risk_pr"),
        patch.object(analysis, "gh_patch"),
    ):
        out = analysis._analyze_pr(
            {"title": "t", "head": {"sha": "s"}}, "o/r", 1, files, "tok", MagicMock(), gate, "", MagicMock(),
            apply_metadata=False,
        )
    return out, recorded.get("risk")


class TestRecordedRisk:
    def test_a_workflow_change_is_recorded_high_whatever_the_model_says(self):
        out, risk = _run_analysis([_f(".github/workflows/ci.yml")])
        assert risk == "high"
        assert "Raised from `LOW`" in out

    def test_low_from_a_basic_tier_model_is_not_trusted(self):
        _out, risk = _run_analysis([_f("app/x.py")], provider_key="groq_8b")
        assert risk == "medium"

    def test_low_that_failed_the_confidence_check_is_not_trusted(self):
        _out, risk = _run_analysis([_f("app/x.py")], auto_apply=False)
        assert risk == "medium"

    def test_an_ordinary_low_stays_low(self):
        _out, risk = _run_analysis([_f("app/x.py")])
        assert risk == "low"

    def test_the_model_can_still_raise_the_level(self):
        _out, risk = _run_analysis([_f("app/x.py")], model_risk="high")
        assert risk == "high"


class TestGroundedDescription:
    FILES = [
        {
            "filename": "app/billing.py",
            "additions": 1,
            "deletions": 1,
            "patch": "@@ -1,2 +1,2 @@\n def charge(amount):\n-    return gw.charge(amount)\n+    return gw.charge(round(amount, 2))\n",
        }
    ]
    DESC = (
        "## Summary\nRounds amounts in `charge` before calling the gateway. "
        "Also updates `retry_with_backoff()` in app/gateway.py.\n"
        "## Testing\n- Added 12 unit tests in tests/test_billing.py; all pass."
    )

    def test_testing_and_changes_come_from_the_files_not_the_model(self):
        out = analysis.grounded_description(self.DESC, self.FILES)
        assert "12 unit tests" not in out
        assert "No test files are changed in this PR." in out
        assert "`app/billing.py` (+1 −1)" in out

    def test_sentences_naming_code_or_files_not_in_the_diff_are_dropped(self):
        out = analysis.grounded_description(self.DESC, self.FILES)
        assert "Rounds amounts in `charge`" in out
        assert "retry_with_backoff" not in out and "app/gateway.py" not in out

    def test_it_is_marked_as_drafted(self):
        out = analysis.grounded_description(self.DESC, self.FILES)
        assert out.startswith(analysis.DESCRIPTION_MARKER) and "Drafted by GitHub Autopilot" in out

    def test_nothing_grounded_writes_nothing(self):
        assert analysis.grounded_description("## Summary\nRewrites `everything()`.", self.FILES) == ""
