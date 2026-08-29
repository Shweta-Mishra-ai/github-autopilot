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
    4  the provider throttled us, so quality was never measured

3 and 4 are separate from 1 because they send a maintainer to opposite ends
of the codebase. The first scheduled run of this suite returned a 0.0 pass rate and
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
import time
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
        r = requests.get(GROQ_MODELS_URL, headers={"Authorization": f"Bearer {key}"}, timeout=15)
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


# The suite fires every case back to back. On a small model that fitted under
# the provider's rate limit; on a larger one it does not — the 2026-08-29 run
# passed all six /fix cases at 1.0, then tripped the limit, opened both circuit
# breakers, and the five review cases received empty output and were scored as
# QUALITY failures. pass_rate 0.545 on a run that never measured review quality
# at all.
#
# Pacing prevents it; the retry survives a burst; and a case still empty after
# that is reported as blocked rather than counted against the score.
CASE_DELAY_SECONDS = float(os.environ.get("EVAL_CASE_DELAY_SECONDS", "3"))
THROTTLE_BACKOFF_SECONDS = float(os.environ.get("EVAL_THROTTLE_BACKOFF_SECONDS", "20"))

# Anything shorter than this is not an answer. The providers return an empty
# string when the circuit is open or the request was refused, and the router's
# degraded reply is a short sentence.
_BLOCKED_OUTPUT_CHARS = 40

# The markers below are only evidence of a degraded reply in a SHORT output.
# A real review is long, and one that happens to discuss rate limiting or
# retrying would otherwise be thrown away as "the provider never answered" --
# scoring nothing and reporting infrastructure, which is the exact mistake
# this function exists to prevent. None of the current cases trip it; the
# bound is here so adding a case about retry logic cannot silently blind the
# suite again.
_DEGRADED_REPLY_CHARS = 200
_BLOCKED_MARKERS = (
    "providers down",
    "provider error",
    "rate limit",
    "circuit open",
    "try again",
    "temporarily unavailable",
)


def looks_blocked(output: str) -> bool:
    """
    True when the provider never gave us an answer to score.

    A blocked case says nothing about review quality. Scoring it as a failure
    is the same mistake as reporting a retired model id as a quality
    regression: it puts an infrastructure fault into a number that people read
    as a statement about the prompts.
    """
    text = (output or "").strip()
    if len(text) < _BLOCKED_OUTPUT_CHARS:
        return True
    if len(text) > _DEGRADED_REPLY_CHARS:
        # Long enough to be a real review. The router's degraded reply is a
        # short sentence, so a marker this far in is the reviewer's own words.
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _BLOCKED_MARKERS)


def _pace() -> None:
    if CASE_DELAY_SECONDS > 0:
        time.sleep(CASE_DELAY_SECONDS)


def _load(name: str) -> list[dict]:
    return json.loads((CASES_DIR / name).read_text(encoding="utf-8"))


def run_fix_cases() -> tuple[list, list]:
    """Push each case through the real /fix command path."""
    from evals.scorers import score_output
    from app.handlers.comments.generator import cmd_fix

    results = []
    blocked = []
    for index, case in enumerate(_load("fix_cases.json")):
        if index:
            _pace()

        # Bound explicitly rather than captured: these are called inside the
        # loop today, but a closure over a loop variable silently reads the
        # LAST case the moment anyone defers the call.
        def _run(case=case):
            return cmd_fix(case["title"], case["context"], repo="")

        output = _attempt(_run, case, results)
        if output is None:
            continue
        if looks_blocked(output):
            blocked.append(case["id"])
            _print_blocked(case["id"])
            continue
        result = score_output(output, case)
        results.append(result)
        _print_case(result)
    return results, blocked


def run_review_cases() -> tuple[list, list]:
    """Push each case through the real PR-review path, capturing the post."""
    from evals.scorers import score_output
    from app.handlers.pull_request import _review_code

    results = []
    blocked = []
    for index, case in enumerate(_load("review_cases.json")):
        if index:
            _pace()
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

        def _run(pr=pr, files=files, cfg=cfg, captured=captured):
            # _review_code RETURNS the review; its own docstring says "This
            # function posts nothing itself." The harness captured gh_post and
            # threw the return value away, so `captured` was always empty and
            # every review case scored "".
            #
            # That is why the review half of this suite has never measured
            # anything. It was read as a rate limit, then as a quality
            # regression -- the failure mode this file's own comments warn
            # about, in the file that warns about it.
            # Cleared per attempt: _attempt retries a case that came back
            # empty, and a list shared across attempts would score the second
            # try's review joined to the first try's.
            captured.clear()
            with patch("app.handlers.pull_request.review.gh_post", side_effect=_capture):
                markdown, inline = _review_code(
                    pr, "eval/repo", 1, files, "tok", cfg, MagicMock(), "", MagicMock()
                )
            # Everything the reviewer produced: the body, the line-anchored
            # comments, and anything the fallback path did post.
            parts = [markdown]
            parts += [c.get("body", "") for c in inline or []]
            parts += captured
            return "\n".join(p for p in parts if p)

        output = _attempt(_run, case, results)
        if output is None:
            continue
        if looks_blocked(output):
            blocked.append(case["id"])
            _print_blocked(case["id"])
            continue

        result = score_output(output, case)
        results.append(result)
        _print_case(result)
    return results, blocked


def _attempt(call, case: dict, results: list):
    """
    Run one case, retrying once when the provider gave us nothing.

    Returns the output, or None when the case was recorded as an exception —
    which is now PRINTED. Both handlers used to `continue` before _print_case,
    so a raising case was counted in the summary and invisible in the output:
    the 2026-08-29 run showed two FAIL lines above a summary listing five
    failed cases, and the three silent ones were the actual story.
    """
    from evals.scorers import CaseResult

    for attempt in (1, 2):
        try:
            output = call()
        except Exception as exc:
            if attempt == 1:
                print(f"  [RETRY] {case['id']}  after {type(exc).__name__}")
                time.sleep(THROTTLE_BACKOFF_SECONDS)
                continue
            result = CaseResult(case["id"], 0.0, False, [f"exception: {exc}"])
            results.append(result)
            _print_case(result)
            return None

        if attempt == 1 and looks_blocked(output):
            print(f"  [RETRY] {case['id']}  no answer from the provider")
            time.sleep(THROTTLE_BACKOFF_SECONDS)
            continue
        return output
    return output


def _print_blocked(case_id: str) -> None:
    print(f"  [BLOCKED] {case_id}  provider returned nothing — not scored")


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
    blocked: list[str] = []
    if args.task in ("all", "fix"):
        print("== /fix cases ==")
        fix_results, fix_blocked = run_fix_cases()
        results += fix_results
        blocked += fix_blocked
    if args.task in ("all", "review"):
        print("== PR review cases ==")
        review_results, review_blocked = run_review_cases()
        results += review_results
        blocked += review_blocked

    stats = summarize(results)
    stats["blocked_cases"] = blocked
    print("\n== summary ==")
    print(json.dumps(stats, indent=2))

    # A throttled case is not a quality signal, and averaging it into the
    # pass-rate turns a provider limit into a statement about the prompts.
    # The 2026-08-29 run reported 0.545 having never scored a single review
    # case: all five were empty because both breakers had opened on rate
    # limits, and every one counted as a failure.
    if blocked:
        print(
            f"\nBLOCKED: {len(blocked)} of {len(blocked) + len(results)} cases got no "
            f"answer from the provider, after a retry each.\n"
            f"  {', '.join(blocked)}\n\n"
            f"This is NOT a quality result — those cases were never scored. The\n"
            f"usual cause is the provider's rate limit: the suite runs cases back\n"
            f"to back, and a larger model has a smaller allowance. Raise\n"
            f"EVAL_CASE_DELAY_SECONDS (currently {CASE_DELAY_SECONDS:g}s between cases) and re-run."
        )
        return 4

    ok = stats["pass_rate"] >= args.min_pass_rate
    print(
        f"\n{'PASS' if ok else 'FAIL'}: pass_rate={stats['pass_rate']} "
        f"(threshold {args.min_pass_rate})"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
