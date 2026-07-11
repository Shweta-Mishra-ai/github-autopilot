# AI output evals

The unit suite (826+ tests) proves the *plumbing* works. This directory proves
the *product* works: golden issues and PR diffs with planted bugs, pushed
through the **real** production code paths (`cmd_fix`, `_review_code`), scored
deterministically.

## Run

```bash
export GROQ_API_KEY=...      # real key — evals spend provider quota
python -m evals.run                     # everything
python -m evals.run --task review       # just PR-review cases
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
