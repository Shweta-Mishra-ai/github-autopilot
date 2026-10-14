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
