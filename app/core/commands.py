"""
app/core/commands.py
The slash-command registry and its access policy — the single source of truth.

Pure data. No imports beyond the standard library, and deliberately no
dependency on the handler packages, so anything that needs to know *what
commands exist* can ask without dragging in an HTTP client, a JWT signer, and
the whole GitHub stack.

That is not hypothetical: the registry previously lived in
app/handlers/comments/constants.py, which is itself pure, but importing it
executes app/handlers/comments/__init__.py first — and that pulls in
app.github.auth, hence `jwt` and `cryptography`. The README region renderers
and the CI job that checks them need the command list and nothing else, and
that job installs no third-party dependencies.

app/handlers/comments/constants.py and app/core/authorization.py both
re-export from here, so existing imports are unchanged.
"""

from __future__ import annotations

# Deduplicated, sorted command registry. This is the list the dispatcher
# matches against; a command absent from here does not exist.
ALL_COMMANDS: list[str] = sorted(
    {
        "/apply",
        "/arch",
        "/autofix",
        "/budget",
        "/changelog",
        "/ci",
        "/docs",
        "/explain",
        "/fix",
        "/gaps",
        "/health",
        "/ignore",
        "/impact",
        "/improve",
        "/merge",
        "/notify",
        "/perf",
        "/refactor",
        "/release",
        "/report",
        "/rollback",
        "/runtests",
        "/secfull",
        "/security",
        "/summarize",
        "/test",
        "/version",
    }
)

# Commands that require write/maintain/admin access on the repository.
# Everything else is open to any commenter.
RESTRICTED_COMMANDS: frozenset[str] = frozenset(
    {
        "/merge",
        "/rollback",
        "/release",
        "/autofix",
        "/apply",  # Auto-mutates repo state
        "/secfull",  # Sensitive report — internal data
        # Writes to persistent repo memory, which is injected into every
        # subsequent AI prompt. Ungated, any commenter on a public repo could
        # poison the context every later command sees — a stored prompt-injection
        # vector that outlives the comment. Its own docstring always said
        # "maintainer preference"; this makes that true.
        "/ignore",
        # Spends the maintainer's resources on a stranger's say-so. Both were
        # DOCUMENTED as maintainer-only — the README has listed them that way
        # since before they had a gate — and neither actually had one, so the
        # documentation was a promise the code did not keep. On a public repo
        # that means any commenter can dispatch CI runs against the owner's
        # Actions minutes, or push messages into the team's Slack and Discord,
        # as often as they care to comment.
        #
        # Neither reads or writes code, so the risk is not disclosure; it is
        # that the cost lands on someone who never agreed to it. Gating them
        # makes the behaviour match what every user was already told.
        "/runtests",
        "/notify",
    }
)
