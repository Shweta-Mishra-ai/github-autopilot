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


def score_output(
    text: str,
    case: dict,
    pass_threshold: float = 0.7,
    structured: dict | None = None,
) -> CaseResult:
    """Score one model output against one golden case.

    `structured` is the model's parsed response, when the task has one. Only
    `expect_gaps` reads it; every other check works on the rendered text, so
    passing it is optional and existing callers are unaffected.
    """
    checks: list[tuple[bool, str]] = []

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
        hit = re.search(pattern, text, re.IGNORECASE | re.DOTALL) is not None
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

    if not checks:
        return CaseResult(case.get("id", "?"), 0.0, False, ["case defines no checks"])

    passed_count = sum(1 for ok, _ in checks if ok)
    score = passed_count / len(checks)
    failures = [msg for ok, msg in checks if not ok]
    return CaseResult(case.get("id", "?"), round(score, 3), score >= pass_threshold, failures)


def summarize(results: list[CaseResult]) -> dict:
    """Aggregate stats for a run."""
    if not results:
        return {"cases": 0, "mean_score": 0.0, "pass_rate": 0.0}
    return {
        "cases": len(results),
        "mean_score": round(sum(r.score for r in results) / len(results), 3),
        "pass_rate": round(sum(1 for r in results if r.passed) / len(results), 3),
        "failed_cases": [r.case_id for r in results if not r.passed],
    }
