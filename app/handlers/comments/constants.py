"""
app/handlers/comments/constants.py
Shared constants for the comments handler package.
"""

SKIP_AUTHORS = frozenset(
    {
        "dependabot[bot]",
        "renovate[bot]",
        "github-actions[bot]",
        "ai-repo-manager[bot]",
        "github-autopilot[bot]",
    }
)

# Deduplicated, sorted command registry
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

# Per-user rate limiting
USER_CMD_LIMIT: int = 10  # commands per user per hour per repo
USER_CMD_WINDOW: int = 3600  # seconds
