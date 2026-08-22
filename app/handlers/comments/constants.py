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
