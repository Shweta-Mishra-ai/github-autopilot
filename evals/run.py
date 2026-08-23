"""
evals/run.py — run the AI-output eval suite against real providers.

    python -m evals.run                 # all tasks
    python -m evals.run --task fix      # just /fix cases
    python -m evals.run --task review   # just PR-review cases
    python -m evals.run --min-pass-rate 0.8

Requires a real GROQ_API_KEY (and optionally GEMINI_API_KEY etc.) — this
deliberately spends provider quota, which is why it is NOT part of pytest.

Exit codes, and they are distinct on purpose:

    0  pass-rate >= --min-pass-rate
    1  the bot answered, and answered worse than the threshold allows
    2  unrunnable — no provider key
    3  the configured model does not exist at the provider

3 is separate from 1 because they send a maintainer to opposite ends of the
codebase. The first scheduled run of this suite returned a 0.0 pass rate and
filed an issue saying review quality had regressed; the actual cause was a
404 from Groq for a retired model id, so every case scored zero on "no code
fence" because there was no output at all. Eleven failing cases described a
prompt problem that did not exist.

Design: cases are pushed through the REAL production code paths (cmd_fix,
_review_code with gh_post captured), never through copies of the prompts —
so what we measure is what users get, and prompt edits are automatically
covered. Scoring is deterministic (see scorers.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

CASES_DIR = Path(__file__).parent / "cases"


# Model ids are configuration, and providers retire them. Checking before the
# suite runs costs one cheap request and turns "eleven cases failed, review
# quality has regressed" into one line naming the model that no longer exists.
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"


def configured_models() -> list[str]:
    """
    The Groq model ids this deployment will actually ask for.

    Defaults are imported from the router rather than copied: a preflight that
    validates a different model than the one in use is worse than no preflight,
    because it reports success for a configuration that cannot work.
    """
    from app.ai.router import DEFAULT_FALLBACK_MODEL, DEFAULT_PRIMARY_MODEL

    return [
        os.environ.get("LLM_PRIMARY_MODEL", DEFAULT_PRIMARY_MODEL),
        os.environ.get("LLM_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL),
    ]


def check_configured_models(key: str) -> tuple[bool, str]:
    """
    (ok, human-readable detail) — does the provider still serve our models?

    Never raises: a diagnostic that fails on its own network call would be
    reporting itself rather than the thing it was asked about. A check that
    cannot be completed returns ok=True with a note, because refusing to run
    the suite over an inconclusive probe would be the worse failure — the
    suite itself is the real measurement.
    """
    import requests

    wanted = configured_models()
    try:
        r = requests.get(
            GROQ_MODELS_URL, headers={"Authorization": f"Bearer {key}"}, timeout=15
        )
    except Exception as exc:
        return True, f"NOTE: could not check model ids ({type(exc).__name__}); running anyway."

    if r.status_code == 401:
        return False, (
            "FAIL: the provider rejected GROQ_API_KEY (401). The key is set but not "
            "valid — regenerate it and update the repository secret."
        )
    if r.status_code != 200:
        return True, (
            f"NOTE: model list returned {r.status_code}; could not check ids. Running anyway."
        )

    try:
        available = sorted(m.get("id", "") for m in (r.json().get("data") or []))
    except Exception:
        return True, "NOTE: model list was not readable; running anyway."

    missing = [m for m in wanted if m not in available]
    if not missing:
        return True, f"Model check: {', '.join(wanted)} — all available."

    listing = "\n".join(f"    {m}" for m in available if m)
    if len(missing) == len(wanted):
        return False, (
            f"FAIL: none of the configured models exist at the provider.\n"
            f"  Configured: {', '.join(wanted)}\n"
            f"  Missing:    {', '.join(missing)}\n\n"
            f"This is not a quality regression — every request would 404, so the\n"
            f"suite would score zero on empty output. The live bot is failing the\n"
            f"same way for every AI command right now.\n\n"
            f"Fix: set LLM_PRIMARY_MODEL and LLM_FALLBACK_MODEL to ids the provider\n"
            f"still serves. Currently available:\n{listing}"
        )

    return True, (
        f"WARNING: {', '.join(missing)} no longer exists at the provider, so the\n"
        f"suite is measuring the remaining model only. Update LLM_PRIMARY_MODEL /\n"
        f"LLM_FALLBACK_MODEL. Currently available:\n{listing}"
    )


def _load(name: str) -> list[dict]:
    return json.loads((CASES_DIR / name).read_text(encoding="utf-8"))


def run_fix_cases() -> list:
    """Push each case through the real /fix command path."""
    from evals.scorers import score_output
    from app.handlers.comments.generator import cmd_fix

    results = []
    for case in _load("fix_cases.json"):
        try:
            output = cmd_fix(case["title"], case["context"], repo="")
        except Exception as e:
            from evals.scorers import CaseResult

            results.append(CaseResult(case["id"], 0.0, False, [f"exception: {e}"]))
            continue
        result = score_output(output, case)
        results.append(result)
        _print_case(result)
    return results


def run_review_cases() -> list:
    """Push each case through the real PR-review path, capturing the post."""
    from evals.scorers import score_output, CaseResult
    from app.handlers.pull_request import _review_code

    results = []
    for case in _load("review_cases.json"):
        captured: list[str] = []

        def _capture(path, token, payload, _sink=captured):
            body = payload.get("body", "")
            for c in payload.get("comments", []):
                body += "\n" + c.get("body", "")
            _sink.append(body)
            return {"id": 1}

        cfg = MagicMock()
        cfg.get.side_effect = lambda *a, **kw: kw.get("default", True)
        cfg.footer = ""
        files = [{"filename": case["filename"], "patch": case["patch"]}]
        pr = {"head": {"sha": "eval0000"}}

        try:
            with patch("app.handlers.pull_request.review.gh_post", side_effect=_capture):
                _review_code(pr, "eval/repo", 1, files, "tok", cfg, MagicMock(), "", MagicMock())
        except Exception as e:
            results.append(CaseResult(case["id"], 0.0, False, [f"exception: {e}"]))
            continue

        output = "\n".join(captured)
        result = score_output(output, case)
        results.append(result)
        _print_case(result)
    return results


def _print_case(result) -> None:
    mark = "PASS" if result.passed else "FAIL"
    print(f"  [{mark}] {result.case_id}  score={result.score}")
    for f in result.failures:
        print(f"         - {f}")


def main() -> int:
    parser = argparse.ArgumentParser(description="GitHub Autopilot AI evals")
    parser.add_argument("--task", choices=["all", "fix", "review"], default="all")
    parser.add_argument("--min-pass-rate", type=float, default=0.7)
    args = parser.parse_args()

    key = os.environ.get("GROQ_API_KEY", "")
    if not key or key.startswith("test_"):
        print("SKIP: evals need a real GROQ_API_KEY (they spend provider quota).")
        return 2

    ok, detail = check_configured_models(key)
    print(detail)
    if not ok:
        return 3

    from evals.scorers import summarize

    results = []
    if args.task in ("all", "fix"):
        print("== /fix cases ==")
        results += run_fix_cases()
    if args.task in ("all", "review"):
        print("== PR review cases ==")
        results += run_review_cases()

    stats = summarize(results)
    print("\n== summary ==")
    print(json.dumps(stats, indent=2))

    ok = stats["pass_rate"] >= args.min_pass_rate
    print(f"\n{'PASS' if ok else 'FAIL'}: pass_rate={stats['pass_rate']} "
          f"(threshold {args.min_pass_rate})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
