"""
app/core/context_manager.py
V4 Sprint 5: Conversation context manager.

PROBLEM:
  /fix on issue #24 doesn't know user already ran /explain.
  Every command starts fresh — bot gives generic answers.

SOLUTION:
  Redis-backed context per issue/PR.
  Each command saves its output → next command has full history.
  TTL: 24 hours (per issue).

USAGE:
    from app.core.context_manager import ContextManager

    ctx = ContextManager(repo, issue_number)
    ctx.add("explain", "Here is how auth works...")
    ctx.add("fix", "Fix: add null check on line 42")

    # In next command:
    history = ctx.get_history()
    # → "Previous /explain: Here is how auth works...\nPrevious /fix: ..."
"""

import json
import logging
import time

log = logging.getLogger(__name__)

CONTEXT_TTL = 86400  # 24 hours
MAX_ENTRIES = 10  # Max history entries per issue
MAX_ENTRY_CHARS = 500  # Truncate long entries


class ContextManager:
    """Manages per-issue conversation context in Redis."""

    def __init__(self, repo: str, issue_number: int):
        self.repo = repo
        self.issue_number = issue_number
        self._key = f"ctx:{repo}:{issue_number}"

    def add(self, command: str, response_summary: str, author: str = ""):
        """
        Save a command's output to context.
        response_summary: short summary of what the command returned.
        """
        try:
            from app.core.redis_client import get_redis

            r = get_redis()
            raw = r.get(self._key)
            entries = json.loads(raw) if raw else []

            entries.append(
                {
                    "command": command,
                    "summary": response_summary[:MAX_ENTRY_CHARS],
                    "author": author,
                    "time": int(time.time()),
                }
            )

            # Keep last N entries
            entries = entries[-MAX_ENTRIES:]
            r.set(self._key, json.dumps(entries), ex=CONTEXT_TTL)

        except Exception as e:
            log.debug(f"context_manager.add_failed: {e}")

    def get_history(self, max_entries: int = 3) -> str:
        """
        Returns formatted history string for injection into AI prompts.
        max_entries: how many recent commands to include.
        """
        try:
            from app.core.redis_client import get_redis

            r = get_redis()
            raw = r.get(self._key)
            if not raw:
                return ""

            entries = json.loads(raw)[-max_entries:]
            if not entries:
                return ""

            parts = []
            for e in entries:
                cmd = e.get("command", "command")
                summary = e.get("summary", "")
                parts.append(f"Previous /{cmd}: {summary}")

            return "## Prior Context\n" + "\n".join(parts) + "\n"

        except Exception:
            return ""

    def get_commands_used(self) -> list[str]:
        """Returns list of commands used on this issue."""
        try:
            from app.core.redis_client import get_redis

            r = get_redis()
            raw = r.get(self._key)
            if not raw:
                return []
            return [e.get("command", "") for e in json.loads(raw)]
        except Exception:
            return []

    def clear(self):
        """Clear context for this issue."""
        try:
            from app.core.redis_client import get_redis

            get_redis().delete(self._key)
        except Exception:
            pass
