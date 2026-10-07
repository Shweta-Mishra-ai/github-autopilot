"""
The review's evidence: findings checked against the diff, the code around the
change shown to the model, and the prompt sized for the provider it reaches.
"""

import base64
from unittest.mock import MagicMock, patch

import pytest

from app.ai import router as router_mod
from app.ai.routing_policy import MAX_USER_CHARS, prompt_chars_for
from app.core.confidence import ConfidenceGate
from app.handlers.pull_request import review
from app.handlers.pull_request.context import fetch_file, surrounding_code
from app.handlers.pull_request.grounding import (
    GROUNDED,
    MISQUOTED,
    REMOVED,
    UNQUOTED,
    ground_finding,
)

PATCH = (
    "@@ -10,3 +10,4 @@ def load():\n"
    " cfg = read()\n"
    "+if cfg is None:\n"
    '+    raise ConfigError("missing")\n'
    "-    return {}\n"
    " ok()"
)


class TestGrounding:
    @pytest.mark.parametrize(
        "issue,expected",
        [
            ({"line": "11", "code": "if cfg is None:"}, (GROUNDED, 11)),
            ({"line": "40", "code": 'raise ConfigError("missing")'}, (GROUNDED, 12)),
            ({"line": "1", "code": '    12 + raise ConfigError("missing")'}, (GROUNDED, 12)),
            ({"line": "11", "code": "return {}"}, (REMOVED, None)),
            ({"line": "11", "code": "db.execute(user_input)"}, (MISQUOTED, None)),
            ({"line": "11"}, (GROUNDED, 11)),
            ({"line": "99"}, (UNQUOTED, None)),
            ({"line": "13", "code": "ok()"}, (GROUNDED, 13)),
            ({"line": "11", "code": "ok()"}, (UNQUOTED, None)),
        ],
    )
    def test_each_outcome(self, issue, expected):
        assert ground_finding(issue, PATCH) == expected

    def test_a_negative_number_in_code_is_not_mangled(self):
        p = "@@ -1,0 +1,1 @@\n+offset = -1 * step_size"
        assert ground_finding({"line": "9", "code": "offset = -1 * step_size"}, p) == (GROUNDED, 1)


SOURCE = (
    "import os\n"  # 1
    "\n"  # 2
    "def unrelated():\n"  # 3
    "    return 1\n"  # 4
    "\n"  # 5
    "def load(path):\n"  # 6
    "    try:\n"  # 7
    "        cfg = read(path)\n"  # 8
    "    except OSError:\n"  # 9
    "        return {}\n"  # 10
    "    if cfg is None:\n"  # 11
    "        raise ConfigError('missing')\n"  # 12
    "    return cfg\n"  # 13
)
SOURCE_PATCH = "@@ -11,1 +11,2 @@\n+    if cfg is None:\n+        raise ConfigError('missing')\n     return cfg"


class TestSurroundingCode:
    def test_python_shows_the_whole_enclosing_function_and_nothing_else(self):
        out = surrounding_code(SOURCE, "app/cfg.py", SOURCE_PATCH, 5000)
        assert "except OSError:" in out, "the handler above the hunk is what the model lacked"
        assert "def load(path):" in out
        assert "def unrelated" not in out

    def test_python_includes_the_imports(self):
        """'X is not defined' is the commonest false positive without them."""
        out = surrounding_code(SOURCE, "app/cfg.py", SOURCE_PATCH, 5000)
        assert out.splitlines()[0].endswith("import os")

    def test_lines_carry_their_real_numbers(self):
        out = surrounding_code(SOURCE, "app/cfg.py", SOURCE_PATCH, 5000)
        assert "     9       except OSError:" in out.splitlines()

    def test_other_languages_get_a_window(self):
        js = "\n".join(f"line{n}();" for n in range(1, 61))
        p = "@@ -30,1 +30,1 @@\n+line30();"
        out = surrounding_code(js, "app/x.js", p, 5000)
        assert "line15();" in out and "line45();" in out and "line50();" not in out

    def test_respects_the_budget_on_a_line_boundary(self):
        out = surrounding_code(SOURCE, "app/cfg.py", SOURCE_PATCH, 60)
        assert len(out) <= 60
        assert all(line in surrounding_code(SOURCE, "app/cfg.py", SOURCE_PATCH, 5000) for line in out.splitlines())

    def test_unparseable_python_falls_back_to_a_window(self):
        broken = SOURCE + "def (:\n"  # a syntax error after the change
        out = surrounding_code(broken, "app/cfg.py", SOURCE_PATCH, 5000)
        assert "if cfg is None:" in out, "a parse failure must still give a window"
        assert "import os" in out, "the window reaches 15 lines either side"

    def test_fetch_never_raises(self):
        def boom(path, token):
            raise RuntimeError("network down")

        assert fetch_file("o/r", "a.py", "sha", "t", boom) == ""

    def test_fetch_skips_oversized_files(self):
        big = {"encoding": "base64", "size": 10_000_000, "content": ""}
        assert fetch_file("o/r", "a.py", "sha", "t", lambda p, t: big) == ""


def _cfg():
    c = MagicMock()
    c.footer = ""
    c.get.side_effect = lambda *a, default=None: default
    return c


def _capture(files, provider_key="groq_70b", contents=None):
    """The prompt and task the review actually sends, for a given provider."""
    seen = {}
    provider = MagicMock()
    provider.provider_key = provider_key

    def ask(system, user, *a, **k):
        seen["user"] = user
        raise RuntimeError("captured")

    provider.ask.side_effect = ask

    def select(task, *a, **k):
        seen.setdefault("tasks", []).append(task)
        return provider

    def gh(path, token):
        name = path.split("/contents/")[1].split("?")[0]
        src = (contents or {}).get(name)
        if src is None:
            raise RuntimeError("no such file")
        return {"encoding": "base64", "size": len(src), "content": base64.b64encode(src.encode()).decode()}

    with (
        patch.object(router_mod.router, "_select_provider", side_effect=select),
        patch.object(review, "gh_get", side_effect=gh),
    ):
        try:
            review._review_code(
                {"head": {"sha": "s"}}, "o/r", 1, files, "t", _cfg(),
                ConfidenceGate(None), "", MagicMock(),
            )
        except RuntimeError:
            pass
    return seen


class TestPromptCarriesEvidence:
    def test_the_surrounding_code_reaches_the_model(self):
        files = [{"filename": "app/cfg.py", "patch": SOURCE_PATCH, "status": "modified"}]
        seen = _capture(files, contents={"app/cfg.py": SOURCE})
        assert "SURROUNDING CODE (unchanged)" in seen["user"] and "<CONTEXT>" in seen["user"]
        assert "except OSError:" in seen["user"]
        assert len(seen["user"]) <= MAX_USER_CHARS

    def test_the_prompt_asks_for_the_quoted_line(self):
        files = [{"filename": "app/cfg.py", "patch": SOURCE_PATCH, "status": "modified"}]
        seen = _capture(files)
        assert '"code": "the exact text of that line' in seen["user"]

    def test_a_failed_fetch_costs_nothing_but_the_context(self):
        files = [{"filename": "app/cfg.py", "patch": SOURCE_PATCH, "status": "modified"}]
        seen = _capture(files, contents={})
        assert "<CONTEXT>" not in seen["user"]
        assert '"files": [' in seen["user"]


def _big_patch(n):
    return "@@ -1,0 +1,%d @@\n" % n + "\n".join(
        f"+    value_{i} = compute_something(argument_{i}, other_{i})" for i in range(n)
    )


class TestGeminiGetsTheBiggerPrompt:
    def test_gemini_limit_is_larger(self):
        assert prompt_chars_for("gemini") > prompt_chars_for("groq_70b") == MAX_USER_CHARS

    def test_with_gemini_the_review_is_a_long_task_and_sees_more_code(self):
        files = [{"filename": f"app/m{i}.py", "patch": _big_patch(150)} for i in range(4)]
        groq = _capture(files, provider_key="groq_70b")
        gem = _capture(files, provider_key="gemini")
        assert "large_pr_review" in gem["tasks"]
        assert len(gem["user"]) > 2 * len(groq["user"])
        assert len(gem["user"]) <= prompt_chars_for("gemini")
        assert gem["user"].count("<DIFF>") == gem["user"].count("</DIFF>")

    def test_without_gemini_nothing_changes(self):
        files = [{"filename": "app/a.py", "patch": _big_patch(10)}]
        seen = _capture(files, provider_key="groq_70b")
        assert seen["tasks"][-1] == "code_review"

    def test_a_fallback_to_a_smaller_provider_is_refitted(self):
        big = "x" * 20_000 + " SCHEMA"
        small = MagicMock()
        small.provider_key = "groq_8b"
        sent = {}

        def ask(system, user, *a, **k):
            sent["user"] = user
            return {"ok": 1}, MagicMock(error=None)

        small.ask.side_effect = ask
        with (
            patch.object(router_mod.router, "_fallback_candidates", return_value=[small]),
            patch("app.ai.router.get_breaker") as gb,
        ):
            gb.return_value.is_available.return_value = True
            router_mod.router._try_fallback("s", big, 100, 0.2, 10, "gemini")
        assert len(sent["user"]) <= MAX_USER_CHARS and sent["user"].endswith("SCHEMA")


class TestIgnorePreferencesReachTheReview:
    """`/ignore` promised reviews would follow it; the review never read memory."""

    def _prompt_with_memory(self, monkeypatch, allow="1"):
        from app.intelligence import memory

        monkeypatch.setenv("MEMORY_ALLOW_CLOUD", allow)
        repo = "o/prefs-" + allow
        memory.clear(repo)
        memory.remember(repo, "Ignored rule set by @lead: line-length nitpicks on test files", kind="preference")
        memory.remember(repo, "Issue #4 triaged as bug/high", kind="pattern")
        files = [{"filename": "app/cfg.py", "patch": SOURCE_PATCH, "status": "modified"}]
        seen = {}
        provider = MagicMock()
        provider.provider_key = "groq_70b"

        def ask(system, user, *a, **k):
            seen["user"] = user
            raise RuntimeError("captured")

        provider.ask.side_effect = ask
        with (
            patch.object(router_mod.router, "_select_provider", return_value=provider),
            patch.object(review, "gh_get", side_effect=RuntimeError("no network")),
        ):
            try:
                review._review_code(
                    {"head": {"sha": "s"}}, repo, 1, files, "t", _cfg(),
                    ConfidenceGate(None), "", MagicMock(),
                )
            except RuntimeError:
                pass
        return seen["user"]

    def test_a_stored_preference_is_in_the_review_prompt(self, monkeypatch):
        user = self._prompt_with_memory(monkeypatch)
        assert "line-length nitpicks on test files" in user
        assert "<PREFERENCES>" in user
        assert "triaged as bug" not in user, "only preferences, not every memory"

    def test_the_privacy_switch_keeps_them_out(self, monkeypatch):
        user = self._prompt_with_memory(monkeypatch, allow="0")
        assert "line-length nitpicks" not in user
