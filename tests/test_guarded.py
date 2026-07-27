"""tests/test_guarded.py — the single seam where an LLM answer becomes publishable."""

from unittest.mock import MagicMock, patch

from app.ai.guarded import degraded_comment, guarded_ask, is_degraded


class TestGuardedAsk:
    def test_unparseable_payload_is_degraded(self):
        with patch(
            "app.ai.guarded.safe_router_ask",
            return_value=({"raw": "I cannot help"}, MagicMock()),
        ):
            out, _verdict = guarded_ask("s", "u", task="explain", response_type="generic")
        assert out["_degraded"] is True
        assert out["_reason"] == "unparseable"

    def test_blocked_response_is_degraded(self):
        payload = {
            "root_cause": "x",
            "fix": "[insert fix here]",
            "explanation": "[your code]",
        }
        with patch("app.ai.guarded.safe_router_ask", return_value=(payload, MagicMock())):
            out, verdict = guarded_ask("s", "u", task="fix_command", response_type="fix")
        assert out["_degraded"] is True
        assert verdict.should_block is True

    def test_clean_response_passes_through(self):
        payload = {
            "root_cause": "null deref on line 12",
            "fix": "guard the optional before dereferencing it",
            "explanation": "the caller may pass None on the error path",
        }
        with patch("app.ai.guarded.safe_router_ask", return_value=(payload, MagicMock())):
            out, _verdict = guarded_ask("s", "u", task="fix_command", response_type="fix")
        assert out.get("_degraded", False) is False
        assert out["root_cause"] == "null deref on line 12"

    def test_providers_down_is_degraded(self):
        with patch(
            "app.ai.guarded.safe_router_ask",
            return_value=({"_providers_down": True, "_retry_in": 60}, None),
        ):
            out, _ = guarded_ask("s", "u", task="explain", response_type="generic")
        assert out["_degraded"] is True
        assert out["_reason"] == "providers_down"


class TestDegradedComment:
    def test_never_fabricates_a_result(self):
        for reason in ("providers_down", "unparseable", "low_confidence"):
            text = degraded_comment({"_degraded": True, "_reason": reason}, "review")
            assert "⚠️" in text
            assert "Score:" not in text
            assert "No issues found" not in text

    def test_is_degraded_helper(self):
        assert is_degraded({"_degraded": True}) is True
        assert is_degraded({"root_cause": "x"}) is False
        assert is_degraded("not a dict") is False


class TestSeamIsStructural:
    """
    Guard against a future command going around guarded_ask().

    Before V7 the hallucination check was something each command had to
    remember, and 29 of ~30 did not. These tests make forgetting a test
    failure rather than a silently wrong answer in production.
    """

    def test_no_json_command_calls_router_ask_directly(self):
        import inspect

        from app.handlers.ci import handle  # noqa: F401
        from app.handlers.comments import generator, reviewer

        for mod in (generator, reviewer):
            src = inspect.getsource(mod)
            assert "router.ask(" not in src, (
                f"{mod.__name__} calls router.ask() directly — JSON commands must "
                "go through app.ai.guarded.guarded_ask so the hallucination check "
                "cannot be skipped."
            )

    def test_generator_imports_the_seam(self):
        import inspect

        from app.handlers.comments import generator

        assert "guarded_ask" in inspect.getsource(generator)

    def test_ci_handler_guards_its_analysis(self):
        import inspect

        import app.handlers.ci as ci_mod

        src = inspect.getsource(ci_mod)
        assert "guarded_ask" in src
        assert "is_degraded" in src
