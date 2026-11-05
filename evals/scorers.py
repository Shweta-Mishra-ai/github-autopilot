"""
evals/scorers.py — deterministic scoring for AI outputs.

No LLM judge: every check is a regex/substring assertion an engineer can read
and dispute. This keeps evals free, fast, reproducible, and honest — the
trade-off is that scorers check "did it mention the planted bug / produce the
required structure", not "was the prose beautiful".

Case schema (JSON):
  {
    "id": "unique-case-id",
    ...task-specific inputs...,
    "must_mention": ["regexA", "regexB"],   # each hit = weight toward score
    "must_not_mention": ["regexC"],          # any hit = hard fail for that check
    "require_code_block": true,              # output must contain ``` fence
    "expect_gaps": false                     # gap analysis: the verdict itself
  }

`expect_gaps` exists because for gap analysis the CORRECT answer is often no
output at all, and every other check here rewards saying something. A reviewer
that reports gaps on a fully tested change is worse than one that stays quiet:
it sends someone looking for tests that already exist, and after the second
time nobody reads the section. That is not a hypothetical — it shipped, on
this repository's own PR #103, which listed four gaps against a diff
containing a direct test for each of them.

Scoring silence needs the structured verdict, not the rendered text, because
"no gaps" and "the provider never answered" both render as an empty string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class CaseResult:
    case_id: str
    score: float           # 0.0 – 1.0
    passed: bool
    failures: list[str] = field(default_factory=list)
    # "detect": a planted bug or a real gap must be found. "restraint": a
    # clean change must not be flagged. Gated separately — see summarize().
    kind: str = ""


def score_output(
    text: str,
    case: dict,
    pass_threshold: float = 0.7,
    structured: dict | None = None,
    findings: str | None = None,
) -> CaseResult:
    """Score one model output against one golden case.

    `structured` is the model's parsed response, when the task has one. Only
    `expect_gaps` reads it.

    `findings` is the text of the findings alone, for a review. When given,
    `must_mention` is matched against it rather than the whole output, and a
    case with `must_mention` fails outright when nothing was reported: a
    planted bug is caught by a finding that names it, not by a summary that
    happens to share a word with the pattern.
    """
    checks: list[tuple[bool, str]] = []
    mention_text = text if findings is None else findings

    if findings is not None and case.get("must_mention"):
        checks.append((bool(findings.strip()), "no finding reported for a planted bug"))

    if "expect_gaps" in case:
        expected = bool(case["expect_gaps"])
        actual = bool((structured or {}).get("has_gaps"))
        checks.append(
            (
                actual == expected,
                f"expect_gaps={expected} but the model answered has_gaps={actual}"
                + (
                    " — it invented gaps in a change these tests already cover"
                    if actual and not expected
                    else ""
                ),
            )
        )

    for pattern in case.get("must_mention", []):
        hit = re.search(pattern, mention_text, re.IGNORECASE | re.DOTALL) is not None
        checks.append((hit, f"must_mention failed: /{pattern}/"))

    for pattern in case.get("must_not_mention", []):
        hit = re.search(pattern, text, re.IGNORECASE) is not None
        checks.append((not hit, f"must_not_mention violated: /{pattern}/"))

    if case.get("require_code_block"):
        checks.append(("```" in text, "require_code_block failed: no ``` fence"))

    # A case whose right answer is silence must not be marked down for being
    # silent. Defaulting to 80 here would make every correct "no gaps" result
    # fail a length check, which is how a suite ends up rewarding noise.
    default_min = 0 if case.get("expect_gaps") is False else 80
    min_len = case.get("min_length", default_min)
    checks.append((len(text) >= min_len, f"output too short (<{min_len} chars)"))

    kind = case_kind(case)
    if not checks:
        return CaseResult(case.get("id", "?"), 0.0, False, ["case defines no checks"], kind)

    passed_count = sum(1 for ok, _ in checks if ok)
    score = passed_count / len(checks)
    failures = [msg for ok, msg in checks if not ok]
    return CaseResult(case.get("id", "?"), round(score, 3), score >= pass_threshold, failures, kind)


def case_kind(case: dict) -> str:
    """Whether the case tests finding something or staying quiet."""
    return "detect" if case.get("must_mention") or case.get("expect_gaps") is True else "restraint"


def summarize(results: list[CaseResult]) -> dict:
    """
    Aggregate stats for a run.

    `by_kind` splits the pass rate into detection and restraint. One overall
    rate let a model that reports nothing pass: the clean cases and the
    "stay quiet" gap cases reward silence, and there are enough of them that
    a stub answering "no issues" to everything scored 0.72 against a 0.7
    gate. Each kind has to clear the threshold on its own.
    """
    if not results:
        return {"cases": 0, "mean_score": 0.0, "pass_rate": 0.0, "by_kind": {}}
    by_kind = {}
    for kind in sorted({r.kind for r in results}):
        group = [r for r in results if r.kind == kind]
        by_kind[kind] = round(sum(1 for r in group if r.passed) / len(group), 3)
    return {
        "cases": len(results),
        "mean_score": round(sum(r.score for r in results) / len(results), 3),
        "pass_rate": round(sum(1 for r in results if r.passed) / len(results), 3),
        "by_kind": by_kind,
        "failed_cases": [r.case_id for r in results if not r.passed],
    }


def gate(stats: dict, threshold: float) -> tuple[bool, str]:
    """Pass only when the overall rate and every kind's rate clear the bar."""
    rates = {"overall": stats.get("pass_rate", 0.0), **stats.get("by_kind", {})}
    low = {k: v for k, v in rates.items() if v < threshold}
    if low:
        return False, ", ".join(f"{k}={v}" for k, v in low.items()) + f" below {threshold}"
    return True, ", ".join(f"{k}={v}" for k, v in rates.items())
