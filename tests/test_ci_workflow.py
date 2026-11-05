"""
The CI workflow's own promises (round-3 audit F13).

A release ran without waiting for the integration and codegraph jobs, and the
"map is stale" check ran on pull requests only, while the comment above it
said main committed a refresh that no step performed.
"""

from pathlib import Path

import yaml

WORKFLOW = yaml.safe_load((Path(__file__).parent.parent / ".github/workflows/ci.yml").read_text())
JOBS = WORKFLOW["jobs"]


def test_release_waits_for_every_gate_that_runs_on_a_tag():
    # A job with no `if:` runs on a tag push, so it is a gate the release
    # can be held by. eval-freshness and badge are main-only by design.
    gates = {name for name, job in JOBS.items() if name != "release" and "if" not in job}
    assert gates <= set(JOBS["release"]["needs"])
    assert {"integration", "codegraph"} <= gates


def test_the_stale_map_check_runs_on_every_event():
    step = next(s for s in JOBS["codegraph"]["steps"] if "stale" in s.get("name", "").lower())
    assert "if" not in step


def test_no_step_claims_to_commit_a_refresh():
    for step in JOBS["codegraph"]["steps"]:
        assert "git push" not in step.get("run", "") and "git commit" not in step.get("run", "")


def test_an_integration_run_with_any_skip_fails():
    """It failed only when the summary BEGAN with "N skipped" (every test
    skipped); a partly skipped run went green."""
    import re

    run = "\n".join(s.get("run", "") for s in JOBS["integration"]["steps"])
    check = re.search(r'grep -qE "([^"]+)" out\.txt', run)
    assert check, "the integration job no longer checks what ran"
    pattern = check.group(1)
    assert re.search(pattern, "30 passed, 6 skipped in 4.2s")
    assert re.search(pattern, "no tests ran in 0.1s")
    assert not re.search(pattern, "36 passed in 4.2s")
