"""
tests/test_pr_description_autofill.py

`pull_requests.auto_fill_description` shipped as three disconnected pieces:
the prompt asked the model for a structured PR body, validate_pr_analysis()
sanitised it to 5000 characters, check_pr_description_update() decided whether
it was allowed — and no code path ever wrote it. The config key was documented
("Fills empty PR descriptions"), defaulted to true, and was *read*, so the
prelaunch "every config key is read" audit passed it. Reading a key inside a
function nobody calls is not wiring.

These tests pin the write itself, plus the two payload shapes that used to
raise inside the same function.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.handlers.pull_request import analysis as A


class Cfg:
    def __init__(self, polish_title=True, fill_desc=True):
        self._polish, self._fill = polish_title, fill_desc

    def get(self, *keys, default=None):
        return {
            ("pull_requests", "auto_polish_title"): self._polish,
            ("pull_requests", "auto_fill_description"): self._fill,
        }.get(keys, default)


def _pr(**over):
    pr = {
        "title": "update stuff",
        "body": "",
        "base": {"ref": "main"},
        "head": {"ref": "feature/x", "sha": "abc123"},
        "user": {"login": "someone"},
    }
    pr.update(over)
    return pr


def _analysis(**over):
    r = {
        "suggested_title": "feat: add widget",
        "description": "## Summary\n\nAdds a widget.\n\n## Changes\n\n- widget\n\n## Testing\n\n- unit",
        "risk_level": "low",
        "risk_reason": "small",
        "review_focus": ["widget"],
        "confidence": 0.9,
    }
    r.update(over)
    return r


def _run(pr, analysis, config, auto_apply=True):
    """Drive _analyze_pr with the LLM and GitHub stubbed. Returns the gh_put mock."""
    gate = MagicMock()
    gate.evaluate.return_value = {"auto_apply": auto_apply, "confidence_note": ""}
    log = MagicMock()

    with (
        patch.object(A.router, "ask", return_value=({}, {})),
        patch.object(A, "validate_pr_analysis", return_value=analysis),
        patch.object(A, "gh_put") as put,
        patch.object(A, "notify_high_risk_pr"),
    ):
        A._analyze_pr(pr, "o/r", 7, [], "tok", config, gate, "", log)
    return put


class TestDescriptionIsActuallyWritten:
    def test_empty_description_is_filled(self):
        put = _run(_pr(body=""), _analysis(), Cfg())
        assert put.call_count == 1
        body = put.call_args[0][2]
        assert body["body"].startswith("## Summary")

    def test_title_and_body_go_in_one_request(self):
        """GitHub emits a `pull_request.edited` webhook per PATCH and this bot
        listens to those, so two writes would mean two events for one decision."""
        put = _run(_pr(), _analysis(), Cfg())
        assert put.call_count == 1
        assert set(put.call_args[0][2]) == {"title", "body"}

    def test_an_existing_description_is_never_overwritten(self):
        """check_pr_description_update() refuses at >=50 characters. Clobbering
        a human-written PR body would be the worst possible failure here."""
        human = "x" * 60
        put = _run(_pr(body=human), _analysis(), Cfg())
        assert "body" not in (put.call_args[0][2] if put.call_count else {})

    def test_config_false_opts_out_of_the_description_only(self):
        put = _run(_pr(), _analysis(), Cfg(fill_desc=False))
        assert set(put.call_args[0][2]) == {"title"}

    def test_config_false_opts_out_of_the_title_only(self):
        put = _run(_pr(), _analysis(), Cfg(polish_title=False))
        assert set(put.call_args[0][2]) == {"body"}

    def test_both_disabled_makes_no_request_at_all(self):
        put = _run(_pr(), _analysis(), Cfg(polish_title=False, fill_desc=False))
        put.assert_not_called()

    def test_the_confidence_gate_still_blocks_both(self):
        put = _run(_pr(), _analysis(), Cfg(), auto_apply=False)
        put.assert_not_called()

    def test_an_empty_description_from_the_model_is_not_written(self):
        """A blank body would erase nothing here (the PR body is already empty)
        but it would still be a pointless PATCH and a spurious webhook."""
        put = _run(_pr(), _analysis(description=""), Cfg())
        assert set(put.call_args[0][2]) == {"title"}


class TestPayloadShapesThatUsedToRaise:
    @pytest.mark.parametrize(
        "field",
        ["user", "head", "base"],
        ids=["deleted-account", "deleted-fork", "missing-base"],
    )
    def test_a_null_top_level_object_does_not_crash_the_analysis(self, field):
        """GitHub sends an explicit null for a deleted fork's `head` and a
        deleted account's `user`. Bare subscripting raised inside the function
        that decides the PR's risk level, so the whole review was lost."""
        put = _run(_pr(**{field: None}), _analysis(), Cfg())
        assert put.call_count == 1  # analysis completed rather than raising

    def test_the_prompt_still_names_the_author_when_the_account_is_gone(self):
        gate = MagicMock()
        gate.evaluate.return_value = {"auto_apply": False, "confidence_note": ""}

        with (
            patch.object(A.router, "ask", return_value=({}, {})) as ask,
            patch.object(A, "validate_pr_analysis", return_value=_analysis()),
            patch.object(A, "notify_high_risk_pr"),
        ):
            A._analyze_pr(_pr(user=None), "o/r", 7, [], "tok", Cfg(), gate, "", MagicMock())

        assert "Author: unknown" in ask.call_args[0][1]
