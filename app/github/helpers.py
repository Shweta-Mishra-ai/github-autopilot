"""
app/github/helpers.py — Shared GitHub API helpers.
"""
import logging

log = logging.getLogger(__name__)


def fmt_error(label: str, e: Exception, limit: int = 200) -> str:
    """Standard error comment format used by all slash commands."""
    return f"## ⚠️ {label}\n\n`{str(e)[:limit]}`"
