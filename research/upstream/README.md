# Upstream issue drafts — NOT POSTED

These are drafts for the authors to review, edit and, if they choose, file.
Nothing in this directory has been posted to any project. Each draft states
only what was reproduced with the named release binary; where a claim rests
on a static count of a rule table rather than on a run, the draft says so.

| Draft | Project | Verified against |
|---|---|---|
| [gitleaks-terminator.md](gitleaks-terminator.md) | gitleaks/gitleaks | 8.21.2, 8.30.1 (latest at drafting) |
| [betterleaks-terminator.md](betterleaks-terminator.md) | betterleaks/betterleaks | 1.9.0 (latest at drafting) |
| [kingfisher-inherited-terminator.md](kingfisher-inherited-terminator.md) | mongodb/kingfisher | 2.11.1, 2.14.0 (latest at drafting) |
| [trufflehog-trailing-word-boundary.md](trufflehog-trailing-word-boundary.md) | trufflesecurity/trufflehog | 3.82.13, 3.99.3 (latest at drafting) |

No draft is proposed for Nosey Parker or detect-secrets: the battery found no
boundary defect in either that a repair would address (see the paper).

Re-run the reproducers: `python reproduce_matrix.py OUTDIR NAME=KIND:BINARY[:CONFIG] ...`
(values are seeded random draws; sequential placeholders are filtered by
these tools and would test the filter, not the boundary).

Before filing:
- check each project's tracker for an existing report of the same defect;
- re-run `reproduce_matrix.py` against the then-current release;
- decide how to describe the research context.
