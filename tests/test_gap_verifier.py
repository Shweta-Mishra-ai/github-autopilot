"""
A claimed test gap on code the PR's tests already call is checked once more,
and dropped only on proof: a named test and a quoted line that really exist.
"""

from unittest.mock import MagicMock, patch

from app.handlers.pull_request import gaps as gaps_mod

SRC = {
    "filename": "app/billing/discount.py",
    "status": "added",
    "patch": "@@ -0,0 +1,4 @@\n+def apply_discount(total, percent):\n"
    "+    if percent < 0 or percent > 100:\n+        raise ValueError('bad')\n"
    "+    return round(total * (1 - percent / 100), 2)\n",
}
TESTS = {
    "filename": "tests/test_discount.py",
    "status": "added",
    "patch": "@@ -0,0 +1,6 @@\n+def test_applies_percentage():\n"
    "+    assert apply_discount(200.0, 10) == 180.0\n+\n"
    "+def test_rejects_out_of_range_percent():\n"
    "+    with pytest.raises(ValueError):\n+        apply_discount(10.0, 101)\n",
}
CLAIM = {
    "has_gaps": True,
    "summary": "s",
    "gaps": [
        {
            "file": "app/billing/discount.py",
            "function": "apply_discount",
            "risk": "high",
            "untested_line": "raise ValueError('bad')",
            "suggested_test": "test that an out-of-range percent raises ValueError",
        }
    ],
}


def _run(verdict, files=(SRC, TESTS)):
    calls = []

    def ask(system, user, **kw):
        calls.append(user)
        return (CLAIM if len(calls) == 1 else verdict), MagicMock()

    with patch.object(gaps_mod.router, "ask", side_effect=ask):
        out = gaps_mod._detect_test_gaps({}, "o/r", 1, list(files), "t", MagicMock(), MagicMock())
    return out, calls


class TestRefutedOnlyOnProof:
    def test_a_claim_the_tests_cover_is_dropped(self):
        out, calls = _run(
            {
                "covered": True,
                "test": "test_rejects_out_of_range_percent",
                "line": "apply_discount(10.0, 101)",
            }
        )
        assert out == "", "a gap the PR's own tests cover must not be published"
        assert len(calls) == 2 and "test_rejects_out_of_range_percent" in calls[1]

    def test_an_invented_test_name_is_not_believed(self):
        out, _ = _run({"covered": True, "test": "test_does_not_exist", "line": "apply_discount(10.0, 101)"})
        assert "Gaps Found" in out

    def test_a_quote_not_in_the_tests_is_not_believed(self):
        out, _ = _run(
            {"covered": True, "test": "test_rejects_out_of_range_percent", "line": "assert raises_for(-1)"}
        )
        assert "Gaps Found" in out

    def test_not_covered_keeps_the_gap(self):
        out, _ = _run({"covered": False, "test": "", "line": ""})
        assert "Gaps Found" in out

    def test_an_unreferenced_function_is_not_rechecked(self):
        out, calls = _run({"covered": True}, files=(SRC,))
        assert "Gaps Found" in out and len(calls) == 1

    def test_a_failed_check_keeps_the_gap(self):
        def ask(system, user, **kw):
            if "claims this behaviour has no test" in user:
                raise RuntimeError("provider down")
            return CLAIM, MagicMock()

        with patch.object(gaps_mod.router, "ask", side_effect=ask):
            out = gaps_mod._detect_test_gaps({}, "o/r", 1, [SRC, TESTS], "t", MagicMock(), MagicMock())
        assert "Gaps Found" in out


class TestRealWorldAnswerShapes:
    """The formats models actually use, which an exact-string check refused."""

    def test_a_parametrised_test_name_and_a_diff_prefixed_quote_still_prove_coverage(self):
        out, _ = _run(
            {
                "covered": "true",
                "test": "tests/test_discount.py::test_rejects_out_of_range_percent(bad)",
                "line": "+        apply_discount(10.0, 101)",
            }
        )
        assert out == ""

    def test_a_function_field_with_decoration_is_still_rechecked(self):
        claim = {
            **CLAIM,
            "gaps": [{**CLAIM["gaps"][0], "function": "apply_discount() — error branch"}],
        }
        calls = []

        def ask(system, user, **kw):
            calls.append(user)
            if len(calls) == 1:
                return claim, MagicMock()
            return (
                {
                    "covered": True,
                    "test": "test_rejects_out_of_range_percent",
                    "line": "apply_discount(10.0, 101)",
                },
                MagicMock(),
            )

        with patch.object(gaps_mod.router, "ask", side_effect=ask):
            out = gaps_mod._detect_test_gaps({}, "o/r", 1, [SRC, TESTS], "t", MagicMock(), MagicMock())
        assert len(calls) == 2, "the decorated name must still trigger the check"
        assert out == ""


class TestATestedFunctionNeedsAnUntestedLine:
    """Asking the model again did not stop its false gaps (it answered "not
    covered" for plainly covered code on every stay-quiet eval case). So a gap
    in a function the PR's tests call must name the changed line no test
    reaches — a claim that cannot is not published."""

    def _claim(self, **gap):
        return {**CLAIM, "gaps": [{**CLAIM["gaps"][0], **gap}]}

    def _run_claim(self, claim, files=(SRC, TESTS)):
        calls = []

        def ask(system, user, **kw):
            calls.append(user)
            return (claim if len(calls) == 1 else {"covered": False}), MagicMock()

        with patch.object(gaps_mod.router, "ask", side_effect=ask):
            out = gaps_mod._detect_test_gaps({}, "o/r", 1, list(files), "t", MagicMock(), MagicMock())
        return out, calls

    def test_no_line_means_no_gap(self):
        out, calls = self._run_claim(self._claim(untested_line=""))
        assert out == "" and len(calls) == 1, "dropped before any extra AI call"

    def test_a_line_that_is_not_in_the_diff_means_no_gap(self):
        out, _ = self._run_claim(self._claim(untested_line="if total is None: return 0"))
        assert out == ""

    def test_a_real_untested_line_is_kept(self):
        out, _ = self._run_claim(self._claim(untested_line="+        raise ValueError('bad')"))
        assert "Gaps Found" in out

    def test_an_untested_function_needs_no_line(self):
        out, _ = self._run_claim(self._claim(untested_line=""), files=(SRC,))
        assert "Gaps Found" in out
