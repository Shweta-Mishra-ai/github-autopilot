# AI output evals

The unit suite proves the *plumbing* works. This directory proves the *product*
works: golden issues and PR diffs with planted bugs, pushed through the **real**
production code paths (`cmd_fix`, `_review_code`, `_detect_test_gaps`), scored
deterministically.

## Run

```bash
export GROQ_API_KEY=...      # real key — evals spend provider quota
python -m evals.run                     # everything
python -m evals.run --task review       # just PR-review cases
python -m evals.run --task gaps         # just test-gap cases
python -m evals.run --min-pass-rate 0.8
```

Exit 0 = pass-rate met, 1 = below threshold, 2 = no real API key.

Also runnable from GitHub Actions: the **Evals** workflow (`workflow_dispatch`)
uses the `GROQ_API_KEY` repo secret.

## Why deterministic scorers (no LLM judge)?

Every check is a regex an engineer can read, dispute, and fix — free, fast,
reproducible. The trade-off is scope: we verify "did the review find the
planted SQL injection", not "was the prose elegant". That is exactly the
regression we care about when swapping prompts or providers.

## When to run

- Before merging any change to prompts, `TASK_MAP`, provider order, or
  sanitization.
- After adding a provider or bumping a model version.
- When a user reports a bad `/fix` or review: reduce it to a case, add it
  here, fix, and it can never silently regress again.

## Adding a case

Append to `cases/fix_cases.json` or `cases/review_cases.json`:

```json
{
  "id": "review-my-planted-bug",
  "filename": "app/x.py",
  "patch": "@@ -1,2 +1,3 @@\n context\n+buggy line\n",
  "planted": "human description of the bug",
  "must_mention": ["regex the output must match"],
  "must_not_mention": ["optional regexes that must NOT appear"],
  "require_code_block": true
}
```

Keep cases *unambiguous* — one clearly-detectable planted issue per case, plus
the `review-clean-change` style negative case to catch over-flagging.

## Gap-analysis cases are scored on the verdict, not the prose

`cases/gaps_cases.json` is different, and deliberately so. For `/fix` and
review, a model that says more scores better. For gap analysis the correct
answer is frequently **nothing at all**, and a report listing gaps a reviewer
can see are already covered is worse than no report: it sends someone looking
for tests that exist, and after the second time nobody reads the section.

That failure shipped. This repository's own PR #103 was told to add tests for
four functions the same diff tested by name, and for a file it had deleted. The
deterministic half of that (deleted files, sending test patches rather than
filenames) is fixed and unit-tested; whether the model then *judges* it
correctly is what these cases measure.

```json
{
  "id": "gaps-fully-tested-change-stays-quiet",
  "filename": "app/billing/discount.py",
  "patch": "@@ ... the source diff ...",
  "test_files": [{"filename": "tests/test_discount.py", "patch": "@@ ..."}],
  "planted": "nothing — both branches are tested in the same PR",
  "expect_gaps": false,
  "must_not_mention": ["Gaps Found", "apply_discount"]
}
```

`expect_gaps` is checked against the model's structured verdict rather than the
rendered markdown, because "no gaps" and "the provider never answered" both
render as an empty string. A case with `expect_gaps: false` also drops the
default 80-character minimum, so silence is not marked down for being short.

**Keep the file balanced.** With only `expect_gaps: true` cases, a model that
always reports gaps scores 100%; with only `false` ones, so does a model that
never does. `tests/test_evals_harness.py` asserts both kinds are present and
that each degenerate model fails the other half.
