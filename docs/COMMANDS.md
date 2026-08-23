# Command reference

Every slash command GitHub Autopilot understands, what it does, where it
works, and who may run it.

Commands are typed as a comment on any issue or pull request in a repository
the App is installed on. The command must be the **first thing in the
comment**; anything after it on the same line is passed to the command as its
argument.

```
/rollback 3 confirm
└──┬────┘ └───┬───┘
   command    argument
```

The bot replies in the same thread. It never acts silently: every command
produces a comment, including the ones that fail.

---

## Contents

- [Access model](#access-model)
- [AI analysis](#ai-analysis) — `/fix` `/explain` `/improve` `/test` `/docs` `/refactor` `/perf` `/gaps` `/arch`
- [Repository insight](#repository-insight) — `/health` `/version` `/summarize` `/ci` `/impact` `/changelog` `/report` `/budget`
- [Security](#security) — `/security` `/secfull`
- [Repository actions](#repository-actions) — `/merge` `/autofix` `/apply` `/rollback` `/release` `/runtests` `/notify` `/ignore`
- [Commands that take arguments](#commands-that-take-arguments)
- [When a command does not respond](#when-a-command-does-not-respond)

---

## Access model

Two levels, and only two:

| Level | Who | Commands |
|---|---|---|
| **Open** | Anyone who can comment | Everything not listed below |
| **Maintainer** | `write`, `maintain` or `admin` on the repository | `/merge` `/autofix` `/apply` `/rollback` `/release` `/secfull` `/ignore` `/runtests` `/notify` |

Permission is checked against GitHub's collaborator API on every invocation —
never cached from a previous answer, and never inferred from who opened the
issue.

**The check fails closed.** If GitHub cannot be reached, or answers with an
error, the command refuses to run rather than assuming permission. A refusal
caused by a transport failure says so explicitly and names the App
installation as the suspect, because that is the usual cause — it does not
tell you that *you* lack access when the bot could not determine whether you
do.

Two entries deserve their reasoning stated:

- **`/secfull`** is maintainer-only because its report names unpatched
  vulnerabilities and their locations. That is a roadmap for anyone who should
  not have it.
- **`/ignore`** writes to persistent repository memory, which is injected into
  every later AI prompt. Ungated on a public repository, any commenter could
  poison the context every subsequent command sees — a stored prompt injection
  that outlives the comment that created it.
- **`/runtests` and `/notify`** spend the maintainer's resources on a
  stranger's say-so: CI minutes, and messages into the team's Slack and
  Discord. Neither reads or writes code, so the risk is not disclosure — it is
  that the cost lands on someone who never agreed to it.

---

## AI analysis

These read the issue or PR they are called from and answer with generated
analysis. They change nothing.

| Command | Scope | Access | What it returns |
|---|---|---|---|
| `/fix` | Issue or PR | Open | A precise bug fix: root cause, the code change, and a test that would have caught it |
| `/explain` | Issue or PR | Open | The issue or code explained in plain English, for someone seeing it for the first time |
| `/improve` | Issue or PR | Open | Concrete, actionable improvements — not a style review |
| `/test` | Issue or PR | Open | Runnable pytest cases, including the edge cases the happy path misses |
| `/docs` | Issue or PR | Open | A docstring, a usage example, and a README section for the code in context |
| `/refactor` | Issue or PR | Open | Targeted refactoring with before/after, scoped to what is actually worth changing |
| `/perf` | Issue or PR | Open | Performance analysis — algorithmic complexity, memory, N+1 queries |
| `/gaps` | Issue or PR | Open | Test coverage gaps, each with a risk rating so the list is orderable |
| `/arch` | Issue or PR | Open | Architecture review: layer violations, coupling, god classes. Uses the PR diff when called on a PR, the repository structure otherwise |

---

## Repository insight

Read-only reporting. No AI generation in most of these — they report facts
from the GitHub API and the bot's own metrics.

| Command | Scope | Access | What it returns |
|---|---|---|---|
| `/health` | Anywhere | Open | A repository health grade: open issues and PRs, licence, description, activity |
| `/version` | Anywhere | Open | Latest tag, latest release, and recent commits |
| `/summarize` | Issue or PR | Open | The discussion thread condensed — for a long thread you are joining late |
| `/ci` | Anywhere | Open | CI failure analysis, from a log pasted after the command or from the latest failed run |
| `/impact` | **PR only** | Open | Blast radius: what else in the codebase this PR's changes reach |
| `/changelog` | Anywhere | Open | A Keep-a-Changelog entry generated from commits since the last tag |
| `/report` | Anywhere | Open | Weekly analytics for this repository |
| `/budget` | Anywhere | Open | Today's AI token usage and cost |

`/impact` answers `/impact only works on Pull Requests` when called on an
issue, rather than failing.

---

## Security

| Command | Scope | Access | What it returns |
|---|---|---|---|
| `/security` | PR (best) | Open | Scans the PR's changed files for secrets and vulnerable dependencies |
| `/secfull` | Anywhere | **Maintainer** | Full repository scan: secrets, dependencies, licence compliance, and CodeQL alerts where enabled |

Both scanners are tuned so that a finding is worth reading. Placeholder
values, lockfile integrity hashes and documented example credentials are
suppressed; a value carrying real key material is reported even when it also
contains a word like `example`. The licence scanner reports only packages it
actually reached, and omits the rest rather than listing them as `unknown`.

`/secfull` degrades rather than failing when an optional permission is
missing: with code scanning disabled, that section is absent and the rest of
the report is unaffected.

---

## Repository actions

These change something. All are maintainer-only, and each one reports what it
did rather than assuming it worked.

| Command | Scope | Access | Effect |
|---|---|---|---|
| `/merge` | **PR only** | Maintainer | Merges the PR after guardrail checks pass — checks green, no conflicts, policy satisfied |
| `/autofix` | Issue or PR | Maintainer | Generates a fix, commits it to a new branch, and posts the diff. **Opens no PR** — a human confirms with `/apply` |
| `/apply` | Issue or PR | Maintainer | Opens a PR from an autofix branch. With no argument, lists the branches available |
| `/rollback` | Anywhere | Maintainer | Lists, previews, or executes a rollback to a snapshot. Requires explicit confirmation |
| `/release` | Anywhere | Maintainer | Drafts a GitHub release from commits since the last tag |
| `/runtests` | Anywhere | Maintainer | Triggers the CI workflow |
| `/notify` | Issue or PR | Maintainer | Sends a Discord and/or Slack alert about this issue or PR |
| `/ignore` | Anywhere | Maintainer | Teaches the bot to stop flagging a pattern in this repository |

**`/autofix` never opens a PR by itself.** It writes a branch and shows you
the diff; nothing reaches a pull request until a maintainer runs `/apply`.
That separation is deliberate — the model proposes, a human disposes.

**`/rollback` undoes newest-first.** Reverting the oldest action before the
newest is not an undo. It also takes a safety snapshot before executing, and
aborts if that snapshot cannot be written — a rollback with no way back is not
one worth performing.

**`/notify` reports per channel.** It tells you what was delivered against
what is configured, including "configured but unreachable". It does not report
success for a channel it never contacted.

---

## Commands that take arguments

Six commands accept an argument. The rest ignore anything after them.

| Usage | Meaning |
|---|---|
| `/rollback` | List available snapshots (max 10, expiring after 7 days) |
| `/rollback 3` | Preview snapshot 3 — what would be undone |
| `/rollback 3 confirm` | Execute the rollback |
| `/apply` | List autofix branches available to open a PR from |
| `/apply <branch>` | Open a PR from that branch |
| `/autofix` | Let the model choose the file to fix |
| `/autofix path/to/file.py` | Fix that file specifically — preferred over the model's own choice |
| `/ci` | Analyse the latest failed run |
| `/ci <pasted log>` | Analyse the log you pasted after the command |
| `/notify` | Send the default alert for this issue or PR |
| `/notify <message>` | Include your own message in the alert |
| `/ignore <rule>` | Record the pattern to stop flagging, in this repository's memory |

A malformed argument is answered with the usage, not a stack trace:
`/rollback abc` replies with what the command expects.

---

## When a command does not respond

Work through these in order:

1. **Was the command first in the comment?** Text before it means the comment
   is not read as a command.
2. **Does the command exist?** An unrecognised command is ignored silently by
   design — otherwise every mention of a Unix path in a comment would draw a reply.
3. **Do you have the access it needs?** Maintainer commands require `write`,
   `maintain` or `admin`. The reply will say so.
4. **Ask the deployment.** The doctor probes each capability with a real call
   and reports which commands cannot work and why:

   ```bash
   curl -H "Authorization: Bearer $METRICS_AUTH_TOKEN" \
     "https://<your-deployment>/setup/doctor?repo=owner/name&installation_id=<id>"
   ```

   A missing App permission is the usual cause, and it is invisible from
   GitHub's UI until something asks. The doctor also reports the deployment
   settings that fail quietly when unset, and needs no arguments to do that:

   ```bash
   curl -H "Authorization: Bearer $METRICS_AUTH_TOKEN" \
     "https://<your-deployment>/setup/doctor"
   ```

Added permissions do **not** apply to an existing installation until they are
accepted. If the doctor still reports a capability as missing after you have
granted it, accept the change on the installation itself.

---

*This reference is checked against the command registry by
`tests/test_commands_doc.py` — a command added to the code and not documented
here fails the build.*
