"""
The eval gate must not blame the prompts for a provider outage.

The first scheduled run of the suite returned a 0.0 pass rate and filed an
issue reading "review quality has regressed". The real cause was a 404 from
Groq for a retired model id: every request failed, so every case scored zero
on "no code fence" because there was no output at all. Eleven failing cases
described a prompt problem that did not exist, and the actual fix was one
environment variable.

Same defect class as reporting a missing API key as a quality drop — a
diagnosis that points at the wrong half of the system costs more time than
no diagnosis.
"""

from unittest.mock import MagicMock, patch

import pytest

from evals.run import check_configured_models, configured_models


def _default_ids():
    """
    The ids the router actually asks for.

    Hardcoding them here meant this fixture claimed "all models present" while
    listing models the router no longer requests — the check would pass for a
    configuration that cannot work, which is the exact failure the preflight
    exists to catch.
    """
    from app.ai.router import DEFAULT_FALLBACK_MODEL, DEFAULT_PRIMARY_MODEL

    return (DEFAULT_PRIMARY_MODEL, DEFAULT_FALLBACK_MODEL)


def _models_response(status=200, ids=None):
    ids = _default_ids() if ids is None else ids
    r = MagicMock()
    r.status_code = status
    r.json.return_value = {"data": [{"id": i} for i in ids]}
    return r


class TestModelCheck:
    def test_all_models_present_is_ok(self, monkeypatch):
        monkeypatch.delenv("LLM_PRIMARY_MODEL", raising=False)
        monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)
        with patch("requests.get", return_value=_models_response()):
            ok, detail = check_configured_models("real-key")
        assert ok is True
        assert "all available" in detail

    def test_every_model_missing_fails_with_the_real_cause(self, monkeypatch):
        monkeypatch.setenv("LLM_PRIMARY_MODEL", "retired-70b")
        monkeypatch.setenv("LLM_FALLBACK_MODEL", "retired-8b")
        with patch("requests.get", return_value=_models_response(ids=("current-model",))):
            ok, detail = check_configured_models("real-key")
        assert ok is False
        assert "not a quality regression" in detail
        assert "retired-70b" in detail and "retired-8b" in detail
        # The operator needs to know what to set it TO, not just that it is wrong.
        assert "current-model" in detail
        assert "LLM_PRIMARY_MODEL" in detail

    def test_the_report_says_production_is_affected_too(self, monkeypatch):
        monkeypatch.setenv("LLM_PRIMARY_MODEL", "gone")
        monkeypatch.setenv("LLM_FALLBACK_MODEL", "gone-too")
        with patch("requests.get", return_value=_models_response(ids=("other",))):
            _, detail = check_configured_models("real-key")
        assert "live bot" in detail, (
            "A 404 for the configured model breaks every AI command in production, "
            "not just this suite. Reporting it as a CI problem understates it."
        )

    def test_one_missing_model_warns_but_still_runs(self, monkeypatch):
        monkeypatch.setenv("LLM_PRIMARY_MODEL", "llama-3.3-70b-versatile")
        monkeypatch.setenv("LLM_FALLBACK_MODEL", "retired-8b")
        with patch("requests.get", return_value=_models_response(ids=("llama-3.3-70b-versatile",))):
            ok, detail = check_configured_models("real-key")
        assert ok is True, "one working model is still worth measuring"
        assert "WARNING" in detail
        assert "retired-8b" in detail

    def test_a_rejected_key_is_reported_as_a_key_problem(self):
        with patch("requests.get", return_value=_models_response(status=401)):
            ok, detail = check_configured_models("bad-key")
        assert ok is False
        assert "401" in detail and "regenerate" in detail.lower()

    @pytest.mark.parametrize("status", [429, 500, 503])
    def test_an_inconclusive_check_does_not_block_the_suite(self, status):
        # The suite itself is the real measurement; refusing to run it over a
        # probe that could not answer would be the worse failure.
        with patch("requests.get", return_value=_models_response(status=status)):
            ok, detail = check_configured_models("real-key")
        assert ok is True
        assert "NOTE" in detail

    def test_a_network_error_does_not_raise(self):
        with patch("requests.get", side_effect=OSError("dns")):
            ok, detail = check_configured_models("real-key")
        assert ok is True
        assert "could not check" in detail

    def test_unparseable_body_does_not_raise(self):
        r = MagicMock()
        r.status_code = 200
        r.json.side_effect = ValueError("not json")
        with patch("requests.get", return_value=r):
            ok, detail = check_configured_models("real-key")
        assert ok is True
        assert "NOTE" in detail

    def test_configured_models_follow_the_environment(self, monkeypatch):
        monkeypatch.setenv("LLM_PRIMARY_MODEL", "a")
        monkeypatch.setenv("LLM_FALLBACK_MODEL", "b")
        assert configured_models() == ["a", "b"]

    def test_defaults_come_from_the_router_not_a_copy(self, monkeypatch):
        # If these drift apart the check validates models the bot never calls,
        # reporting success for a configuration that cannot work.
        monkeypatch.delenv("LLM_PRIMARY_MODEL", raising=False)
        monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)
        from app.ai.router import DEFAULT_FALLBACK_MODEL, DEFAULT_PRIMARY_MODEL

        assert configured_models() == [DEFAULT_PRIMARY_MODEL, DEFAULT_FALLBACK_MODEL]


class TestExitCodeContract:
    def test_the_runner_documents_a_distinct_code_for_a_missing_model(self):
        import evals.run as run

        assert "3  the configured model does not exist" in run.__doc__, (
            "The workflow branches on exit code 3 to decide whether to report a "
            "quality regression or an infrastructure failure."
        )

    def test_the_workflow_branches_on_that_code(self):
        from pathlib import Path

        wf = Path(__file__).resolve().parent.parent / ".github/workflows/evals.yml"
        text = wf.read_text(encoding="utf-8")
        assert "exit_code" in text, "the run step must publish its exit code"
        assert 'outputs.exit_code }}\" = \"3\"' in text, (
            "the report step must branch on exit code 3, or a retired model is "
            "reported as a quality regression again"
        )


class TestTheNightlyReportReusesOneIssue:
    """
    The report step opened a new issue every night.

    It looked up the existing report with `gh issue list --label evals`, and
    that label had never been created: `gh issue create --label evals` fails
    with "not found" and falls back to the unlabelled create, so no issue ever
    carried it. A filter on a label nothing has matches nothing, so the lookup
    was always empty. Four nights produced four identical issues — #86, #88,
    #89, #90 — which is exactly what reusing one issue was written to prevent.

    A comment in that same step claimed the reuse worked. Nothing checked it.
    """

    @staticmethod
    def _report_step() -> str:
        from pathlib import Path

        import yaml

        wf = Path(__file__).resolve().parent.parent / ".github/workflows/evals.yml"
        spec = yaml.safe_load(wf.read_text(encoding="utf-8"))
        steps = spec["jobs"]["evals"]["steps"]
        report = [s for s in steps if str(s.get("name", "")).startswith("Report")]
        assert report, "the evals workflow must still have a report step"
        return report[0]["run"]

    def test_the_lookup_does_not_filter_on_a_label(self):
        run = self._report_step()
        assert "--label evals --json" not in run, (
            "filtering the lookup by a label makes the reuse depend on that "
            "label existing — it did not, and every night opened a new issue"
        )

    def test_the_lookup_matches_on_the_title_instead(self):
        run = self._report_step()
        assert 'startswith("Nightly AI evals")' in run, (
            "titles are ours and all three causes share a prefix, so matching "
            "on them needs no label and no search index"
        )

    def test_every_title_the_step_can_set_shares_that_prefix(self):
        # If a new cause is added with a different title, the reuse silently
        # breaks again and nothing else would notice.
        import re

        run = self._report_step()
        titles = re.findall(r'TITLE="([^"]+)"', run)
        assert len(titles) >= 3, f"expected the three failure causes, found {titles}"
        for title in titles:
            assert title.startswith("Nightly AI evals"), (
                f"{title!r} does not share the prefix the lookup matches on, so a "
                f"run failing this way would open a second issue alongside the first"
            )

    def test_label_creation_cannot_break_the_step(self):
        run = self._report_step()
        assert "gh label create evals" in run, "the label should still be created"
        # The command spans a line continuation, so join them before looking
        # for the guard — checking a single line would pass a broken step.
        joined = run.replace("\\\n", " ")
        statement = next(ln for ln in joined.splitlines() if "gh label create" in ln)
        assert "|| true" in statement, (
            f"label creation must be best-effort — it fails when the label "
            f"already exists, and `bash -e` would abort the whole step. Got: "
            f"{statement.strip()!r}"
        )

    def test_the_existing_issue_is_retitled_to_the_current_cause(self):
        run = self._report_step()
        assert "gh issue edit" in run, (
            "the cause can change between nights; an issue still titled 'are "
            "failing' when the run now fails for a missing model points the "
            "reader at the wrong thing"
        )


class TestTheFreshnessGateChecksTheResult:
    """
    The freshness job reported "✅ eval gate is running" while the evals had
    failed four nights running.

    It read only the last run's timestamp. A gate that runs and fails measures
    exactly as much as one that never runs, and the green tick actively argued
    against looking — the job exists to notice when quality stops being
    measured, and this is one of the two ways that happens.
    """

    @staticmethod
    def _step() -> str:
        from pathlib import Path

        import yaml

        wf = Path(__file__).resolve().parent.parent / ".github/workflows/ci.yml"
        spec = yaml.safe_load(wf.read_text(encoding="utf-8"))
        steps = spec["jobs"]["eval-freshness"]["steps"]
        return steps[0]["run"]

    def test_it_reads_the_conclusion_not_only_the_timestamp(self):
        run = self._step()
        assert "conclusion" in run, (
            "reading only created_at cannot tell a passing nightly run from a "
            "failing one, and it reported the failing case as healthy"
        )

    def test_a_failing_run_is_not_reported_with_a_tick(self):
        run = self._step()
        assert 'RESULT" != "success"' in run, (
            "there must be a branch for 'ran recently but did not pass'"
        )
        # Only lines that actually emit — a comment quoting the old wording
        # is documentation, not behaviour, and matching it would fail here for
        # a reason that has nothing to do with what the job does.
        emitted = [
            ln
            for ln in run.splitlines()
            if "GITHUB_STEP_SUMMARY" in ln
            and "eval gate is running" in ln
            and not ln.lstrip().startswith("#")
        ]
        assert emitted, "the healthy summary line should still exist"
        assert all("passing" in ln for ln in emitted), (
            "'the eval gate is running' was true and useless while the gate was "
            "red every night; the healthy line must claim passing, not running"
        )


class TestTheBadgeCannotAdvertiseARedRun:
    """
    The badge job pipes pytest into tee and greps the pass count out. Without
    pipefail a failing pytest exits 0 through the pipe, and the grep reads
    "2090 passed" straight out of "3 failed, 2090 passed" — publishing a green
    badge for a red run. The job re-runs the suite itself, so its run can fail
    independently of the test job it depends on.
    """

    @staticmethod
    def _step() -> str:
        from pathlib import Path

        import yaml

        wf = Path(__file__).resolve().parent.parent / ".github/workflows/ci.yml"
        spec = yaml.safe_load(wf.read_text(encoding="utf-8"))
        steps = spec["jobs"]["badge"]["steps"]
        counting = [s for s in steps if "Count passing tests" in str(s.get("name", ""))]
        assert counting, "the badge job must still count tests"
        return counting[0]["run"]

    def test_pipefail_is_set(self):
        run = self._step()
        assert "pipefail" in run, (
            "without pipefail the pytest exit code is swallowed by tee and a "
            "failing run publishes a passing count"
        )

    def test_a_run_with_failures_is_refused(self):
        run = self._step()
        assert "failed|error" in run, (
            "the count must be refused when the output reports failures, not "
            "merely when the pass count is missing"
        )


class TestAThrottledRunIsNotAQualityResult:
    """
    The 2026-08-29 run passed all six /fix cases at 1.0, then tripped the
    provider's rate limit. Both circuit breakers opened, the five review cases
    received empty output, and every one was scored as a QUALITY failure:
    `pass_rate=0.545` on a run that never scored a single review case.

    Same defect as reporting a retired model id as a regression — an
    infrastructure fault written into a number people read as a statement
    about the prompts.
    """

    @pytest.mark.parametrize(
        "output",
        [
            "",
            "   ",
            "x" * 39,
            "Providers are busy, try again shortly.",
            "## ⚠️ Provider error",
            "AI providers down — try again",
        ],
    )
    def test_an_unanswered_case_is_detected(self, output):
        from evals.run import looks_blocked

        assert looks_blocked(output)

    @pytest.mark.parametrize(
        "output",
        [
            "x" * 200,
            "## Fix\n\nThe slice is off by one: `size - 1` drops the final element. "
            "Use `start + size`, and add a boundary test so it cannot regress again.",
        ],
    )
    def test_a_real_answer_is_not_treated_as_blocked(self, output):
        from evals.run import looks_blocked

        assert not looks_blocked(output), (
            "marking a genuine answer as blocked would hide a real quality drop, "
            "which is the opposite failure and the more dangerous one"
        )

    def test_the_runner_documents_a_distinct_code_for_a_throttled_run(self):
        import evals.run as run

        assert "4  the provider throttled us" in run.__doc__

    def test_the_workflow_branches_on_that_code(self):
        from pathlib import Path

        wf = Path(__file__).resolve().parent.parent / ".github/workflows/evals.yml"
        text = wf.read_text(encoding="utf-8")
        assert 'outputs.exit_code }}" = "4"' in text, (
            "without its own branch a throttled run files 'quality regressed'"
        )

    def test_the_suite_paces_itself_between_cases(self):
        from evals.run import CASE_DELAY_SECONDS

        assert CASE_DELAY_SECONDS > 0, (
            "firing every case back to back is what tripped the rate limit; "
            "pacing prevents the condition rather than only reporting it"
        )

    def test_a_case_that_raises_is_printed_not_only_counted(self):
        # Both handlers used to `continue` before _print_case, so a raising
        # case was counted in the summary and invisible in the output: two
        # FAIL lines above a summary listing five failed cases.
        import inspect

        from evals.run import _attempt

        source = inspect.getsource(_attempt)
        assert "_print_case(result)" in source, (
            "an exception case must reach the printer, or the visible output "
            "disagrees with the summary and hides the real cause"
        )

    def test_a_blocked_case_is_reported_separately_from_failures(self):
        import inspect

        import evals.run as run

        source = inspect.getsource(run.main)
        assert "return 4" in source
        assert "NOT a quality result" in source, (
            "the message must say plainly that these cases were never scored"
        )
