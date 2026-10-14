"""
A test gap in code the PR's tests already call is published only with a fact
the diff can prove — the model proposes a line, it does not get to decide.

Asking the model whether its own claim was covered was measured on the live
evals: it said "not covered" for tests that plainly cover the code, every
time. So the proof is checked in code, and costs no extra AI call.
"""

from unittest.mock import MagicMock, patch

from app.handlers.pull_request import gaps as gaps_mod

SRC = {
    "filename": "app/net/retry.py",
    "status": "modified",
    "patch": "@@ -1,2 +1,8 @@\n def call_with_retry(fn, attempts=3, on_giveup=None):\n"
    "-    return fn()\n+    last = None\n+    for attempt in range(attempts):\n"
    "+        try:\n+            return fn()\n+        except TimeoutError as exc:\n"
    "+            last = exc\n+    if on_giveup is not None:\n+        on_giveup(last)\n"
    "+    raise last\n",
}
TESTS_NO_FAILURE_ASSERTED = {
    "filename": "tests/test_retry.py",
    "status": "modified",
    "patch": "@@ -1,2 +1,4 @@\n from app.net.retry import call_with_retry\n"
    "+def test_returns_result():\n+    assert call_with_retry(lambda: 42) == 42\n",
}
TESTS_ASSERT_FAILURE = {
    "filename": "tests/test_retry.py",
    "status": "modified",
    "patch": "@@ -1,2 +1,6 @@\n from app.net.retry import call_with_retry\n"
    "+def test_returns_result():\n+    assert call_with_retry(lambda: 42) == 42\n"
    "+def test_gives_up():\n+    with pytest.raises(TimeoutError):\n"
    "+        call_with_retry(boom, on_giveup=print)\n",
}


def _gap(line, function="call_with_retry"):
    return {
        "file": "app/net/retry.py",
        "function": function,
        "risk": "high",
        "untested_line": line,
        "suggested_test": "exhaust the attempts",
    }


def _run(gap, files):
    claim = {"has_gaps": True, "summary": "s", "gaps": [gap]}
    calls = []

    def ask(system, user, **kw):
        calls.append(user)
        return claim, MagicMock()

    with patch.object(gaps_mod.router, "ask", side_effect=ask):
        out = gaps_mod._detect_test_gaps({}, "o/r", 1, list(files), "t", MagicMock(), MagicMock())
    return out, calls


class TestAGapInATestedFunctionNeedsProof:
    def test_a_raise_is_a_gap_when_no_test_asserts_failure(self):
        out, calls = _run(_gap("raise last"), [SRC, TESTS_NO_FAILURE_ASSERTED])
        assert "Gaps Found" in out
        assert "no test in this PR asserts that anything raises" in out
        assert len(calls) == 1, "proof is checked in code, not by asking the model again"

    def test_a_raise_is_not_a_gap_when_a_test_asserts_failure(self):
        out, _ = _run(_gap("raise last"), [SRC, TESTS_ASSERT_FAILURE])
        assert out == ""

    def test_an_optional_parameter_no_test_names_is_a_gap(self):
        out, _ = _run(_gap("on_giveup(last)"), [SRC, TESTS_NO_FAILURE_ASSERTED])
        assert "Gaps Found" in out and "`on_giveup`" in out

    def test_an_optional_parameter_a_test_sets_is_not_a_gap(self):
        out, _ = _run(_gap("on_giveup(last)"), [SRC, TESTS_ASSERT_FAILURE])
        assert out == ""

    def test_a_line_that_proves_nothing_is_not_published(self):
        """'Add another input' is a claim no diff can confirm."""
        out, _ = _run(_gap("last = exc"), [SRC, TESTS_NO_FAILURE_ASSERTED])
        assert out == ""

    def test_a_line_that_is_not_in_the_diff_is_not_published(self):
        out, _ = _run(_gap("raise ImaginaryError()"), [SRC, TESTS_NO_FAILURE_ASSERTED])
        assert out == ""

    def test_no_line_means_no_gap(self):
        out, _ = _run(_gap(""), [SRC, TESTS_NO_FAILURE_ASSERTED])
        assert out == ""

    def test_the_function_field_may_be_decorated(self):
        out, _ = _run(_gap("raise last", "call_with_retry() — give-up branch"), [SRC, TESTS_NO_FAILURE_ASSERTED])
        assert "Gaps Found" in out


class TestAnUntestedFunctionNeedsNoLine:
    def test_no_test_mentions_it(self):
        out, _ = _run(_gap(""), [SRC])
        assert "Gaps Found" in out

    def test_a_removed_test_is_not_a_reference(self):
        deleted = {
            "filename": "tests/test_retry.py",
            "status": "modified",
            "patch": "@@ -1,3 +1,1 @@\n-def test_old():\n-    assert call_with_retry(f) == 1\n+x = 1\n",
        }
        out, _ = _run(_gap(""), [SRC, deleted])
        assert "Gaps Found" in out


class TestOptionalParams:
    def test_defaults_only_and_not_self_or_varargs(self):
        patch_ = "@@ -0,0 +1,2 @@\n+def f(self, a, b=1, *args, c: int = 2, **kw):\n+    pass\n"
        assert gaps_mod._optional_params(patch_, "f") == ["b", "c"]

    def test_defaults_containing_commas_and_calls(self):
        patch_ = "@@ -0,0 +1,2 @@\n+def f(a, opts=dict(x=1, y=2), flag=False):\n+    pass\n"
        assert gaps_mod._optional_params(patch_, "f") == ["opts", "flag"]

    def test_a_signature_spread_over_lines(self):
        patch_ = "@@ -0,0 +1,4 @@\n+def f(\n+    a,\n+    retries=3,\n+):\n+    pass\n"
        assert gaps_mod._optional_params(patch_, "f") == ["retries"]

    def test_javascript_has_no_python_defaults_and_does_not_crash(self):
        patch_ = "@@ -0,0 +1,2 @@\n+function f(a, b) {\n+  return a;\n+}\n"
        assert gaps_mod._optional_params(patch_, "f") == []
