"""
REPO_DAILY_AI_LIMIT counts AI calls, not events.

It was charged once per pull-request or issue event — a PR event makes three
to five AI calls — and comment, push and CI events were never charged.
"""

from unittest.mock import MagicMock, patch

from app.ai import router as router_mod
from app.core import guardrails
from app.core.redis_client import get_redis


def _count(repo):
    import datetime

    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    return int(get_redis().get(f"limit:{repo}:ai_calls:{today}") or 0)


def _provider():
    meta = MagicMock(error=None, text='{"ok": true}', provider="x", total_tokens=0)
    p = MagicMock()
    p.ask.return_value = ({"ok": True}, meta)
    p.ask_text.return_value = ("ok", meta)
    return p


class TestEveryCallIsCharged:
    def test_each_call_inside_a_metered_event_counts(self):
        repo = "o/metered-calls"
        before = _count(repo)
        with (
            patch.object(router_mod.router, "_select_provider", return_value=_provider()),
            patch.object(router_mod.router, "_log_and_track"),
            guardrails.metered(repo),
        ):
            router_mod.router.ask("s", "u")
            router_mod.router.ask("s", "u")
            router_mod.router.ask_text("s", "u")
        assert _count(repo) - before == 3

    def test_calls_outside_any_event_are_not_charged_to_a_repo(self):
        repo = "o/unmetered"
        before = _count(repo)
        with (
            patch.object(router_mod.router, "_select_provider", return_value=_provider()),
            patch.object(router_mod.router, "_log_and_track"),
        ):
            router_mod.router.ask("s", "u")
        assert _count(repo) == before

    def test_the_scope_does_not_leak_after_the_event(self):
        with guardrails.metered("o/scoped"):
            assert guardrails._metered_repo.get() == "o/scoped"
        assert guardrails._metered_repo.get() == ""


class TestServerMetersEveryHandler:
    def test_a_comment_event_is_metered(self):
        import server

        seen = {}

        def handle(payload):
            seen["repo"] = guardrails._metered_repo.get()

        with patch("app.handlers.comments.handle", side_effect=handle):
            server._run_handler("issue_comment", {}, "o/comments")
        assert seen["repo"] == "o/comments"


class TestNoDoubleCharge:
    def test_the_pr_handler_no_longer_adds_a_flat_charge_per_event(self):
        import ast
        import inspect
        import textwrap

        from app.handlers import issues, pull_request

        for mod in (pull_request, issues):
            tree = ast.parse(textwrap.dedent(inspect.getsource(mod.handle)))
            calls = {
                getattr(n.func, "id", getattr(n.func, "attr", ""))
                for n in ast.walk(tree)
                if isinstance(n, ast.Call)
            }
            assert "increment_repo_usage" not in calls, f"{mod.__name__} double-charges"
