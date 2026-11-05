"""
app/handlers/comments/constants.py
Shared constants for the comments handler package.
"""

from app.core.commands import ALL_COMMANDS as _ALL_COMMANDS

SKIP_AUTHORS = frozenset(
    {
        "dependabot[bot]",
        "renovate[bot]",
        "github-actions[bot]",
        "ai-repo-manager[bot]",
        "github-autopilot[bot]",
    }
)

# Re-exported from app.core.commands, which is the single source of truth.
# It lives there rather than here because importing this module executes
# app/handlers/comments/__init__.py, which pulls in the GitHub + JWT stack —
# too heavy for callers that only need to know which commands exist.
ALL_COMMANDS = _ALL_COMMANDS

# Per-user rate limiting
USER_CMD_LIMIT: int = 10  # commands per user per hour per repo
USER_CMD_WINDOW: int = 3600  # seconds

# Commands that analyse code. On a pull request they are given the start of
# its diff; given only the title and description they analysed prose.
DIFF_CONTEXT_COMMANDS = frozenset(
    {"/fix", "/explain", "/improve", "/test", "/docs", "/refactor", "/gaps", "/perf"}
)
PR_DIFF_CONTEXT_CHARS = 3500

# How much context a generator command sends. Was 2,000-2,500, which on a PR
# context left room for the title and description and none of the diff.
COMMAND_CONTEXT_CHARS = 5500
