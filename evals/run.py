"""
evals/run.py — run the AI-output eval suite against real providers.

    python -m evals.run                 # all tasks
    python -m evals.run --task fix      # just /fix cases
    python -m evals.run --task review   # just PR-review cases
    python -m evals.run --min-pass-rate 0.8

Requires a real GROQ_API_KEY (and optionally GEMINI_API_KEY etc.) — this
deliberately spends provider quota, which is why it is NOT part of pytest.
Exit code 0 when pass-rate >= --min-pass-rate, 1 otherwise, 2 when unrunnable.

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
            with patch("app.handlers.pull_request.gh_post", side_effect=_capture):
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
