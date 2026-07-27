"""
app/handlers/comments/
V5 — Split from 1603-line comments.py into focused modules.

Package structure:
  constants.py   — SKIP_AUTHORS, ALL_COMMANDS, rate-limit config
  dispatcher.py  — command extraction, rate limit, provider-down handling
  generator.py   — AI content: /fix /explain /improve /test /docs /refactor /gaps /perf /arch
  reviewer.py    — Read-only: /health /version /summarize /ci /budget /report /impact /changelog
  publisher.py   — GitHub writes: /merge /apply /rollback /release /runtests /notify /security
  service.py     — Main entry point: handle_comment_event()

Public API (used by server.py):
"""

# ── Additional shims for test patching (defined first to prevent circular imports) ──
# Tests patch these at the package level (e.g. app.handlers.comments.gh_get).
from app.github.auth import get_installation_token
from app.github.client import gh_get, gh_post, gh_put, gh_delete
from app.core.config import load_config
from app.ai.router import router

# ── Main Entry Point ─────────────────────────────────────────────────────────
from .service import handle_comment_event

# ── Backward-compat shims ─────────────────────────────────────────────────────
# Existing tests and app/mcp/mcp_server.py import these using the old
# underscore-prefixed names from the monolith.  Expose them here so that
# `from app.handlers.comments import _cmd_fix` keeps working.

from .dispatcher import extract_command as _extract_command
from .generator import (
    cmd_fix as _cmd_fix,
    cmd_explain as _cmd_explain,
    cmd_improve as _cmd_improve,
    cmd_test as _cmd_test,
    cmd_docs as _cmd_docs,
    cmd_refactor as _cmd_refactor,
    cmd_gaps as _cmd_gaps,
    cmd_perf as _cmd_perf,
    cmd_arch as _cmd_arch,
)
from .reviewer import (
    cmd_health as _cmd_health,
    cmd_version as _cmd_version,
    cmd_summarize as _cmd_summarize,
    cmd_ci as _cmd_ci,
    cmd_budget as _cmd_budget,
    cmd_report as _cmd_report,
    cmd_impact as _cmd_impact,
    cmd_changelog as _cmd_changelog,
)
from .publisher import (
    cmd_merge as _cmd_merge,
    cmd_apply as _cmd_apply,
    cmd_rollback as _cmd_rollback,
    cmd_release as _cmd_release,
    cmd_runtests as _cmd_runtests,
    cmd_notify as _cmd_notify,
)
from .security import (
    cmd_security as _cmd_security,
    cmd_secfull as _cmd_secfull,
)

# server.py imports 'handle' from this package
handle = handle_comment_event

__all__ = [
    "handle_comment_event",
    "handle",
    "_extract_command",
    "_cmd_fix",
    "_cmd_explain",
    "_cmd_improve",
    "_cmd_test",
    "_cmd_docs",
    "_cmd_refactor",
    "_cmd_gaps",
    "_cmd_perf",
    "_cmd_arch",
    "_cmd_health",
    "_cmd_version",
    "_cmd_summarize",
    "_cmd_ci",
    "_cmd_budget",
    "_cmd_report",
    "_cmd_impact",
    "_cmd_changelog",
    "_cmd_merge",
    "_cmd_apply",
    "_cmd_rollback",
    "_cmd_release",
    "_cmd_runtests",
    "_cmd_notify",
    "_cmd_security",
    "_cmd_secfull",
    "get_installation_token",
    "gh_get",
    "gh_post",
    "gh_put",
    "gh_delete",
    "load_config",
    "router",
]
