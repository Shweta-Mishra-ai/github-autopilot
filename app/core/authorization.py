"""
app/core/authorization.py
─────────────────────────
Enforces permission checks BEFORE any sensitive command executes.

WHY THIS EXISTS:
  Config declares `maintainer_only: [merge, release, rollback]` but
  comments.py never actually checked it. Any GitHub commenter could
  trigger /merge on a public repo. This module closes that gap.

PERMISSION LEVELS (GitHub API):
  admin   → repo owner, org admin
  maintain → maintainers
  write   → collaborators with write access
  read    → collaborators with read only
  none    → not a collaborator

ALLOWED for maintainer-only commands: admin, maintain, write
"""

import logging
import threading
from app.github.client import gh_get, GitHubError

log = logging.getLogger(__name__)

# Cache permission lookups: (repo, user) → permission_level
# TTL = 5 min — same as config cache. Cleared on explicit invalidation.
_perm_cache: dict[tuple, tuple] = {}   # {(repo, user): (perm, timestamp)}
_perm_lock = threading.Lock()
_PERM_TTL = 300  # 5 minutes


MAINTAINER_PERMISSIONS = {"admin", "maintain", "write"}

# Commands that require at least write/maintain/admin access
RESTRICTED_COMMANDS = {
    "/merge", "/rollback", "/release",
    "/autofix", "/apply",        # Auto-mutates repo state
    "/secfull",                  # Sensitive report — internal data
}


def get_user_permission(repo: str, username: str, token: str) -> str:
    """
    Returns the GitHub permission level for `username` on `repo`.
    Values: "admin" | "maintain" | "write" | "read" | "none"

    Caches for 5 minutes to avoid hammering GitHub API.
    Returns "none" on any API error (fail closed).
    """
    import time
    cache_key = (repo, username)
    now = time.time()

    with _perm_lock:
        if cache_key in _perm_cache:
            perm, ts = _perm_cache[cache_key]
            if now - ts < _PERM_TTL:
                return perm

    try:
        data = gh_get(
            f"/repos/{repo}/collaborators/{username}/permission",
            token,
        )
        perm = data.get("permission", "none")
        log.debug(f"auth.permission user={username} repo={repo} level={perm}")
    except GitHubError as e:
        if e.status_code == 404:
            # Not a collaborator at all
            perm = "none"
        else:
            log.warning(f"auth.permission_check_failed user={username}: {e}")
            perm = "none"   # fail closed
    except Exception as e:
        log.error(f"auth.permission_unexpected user={username}: {e}")
        perm = "none"

    with _perm_lock:
        _perm_cache[cache_key] = (perm, now)

    return perm


def is_maintainer(repo: str, username: str, token: str) -> bool:
    """True if user has write/maintain/admin access."""
    return get_user_permission(repo, username, token) in MAINTAINER_PERMISSIONS


def check_command_permission(
    cmd: str,
    repo: str,
    author: str,
    token: str,
    config,
) -> tuple[bool, str]:
    """
    Returns (allowed: bool, denial_reason: str).

    Steps:
    1. If command is not in RESTRICTED_COMMANDS → always allowed.
    2. If config marks it maintainer_only → check GitHub permission API.
    3. Fail closed: if permission check errors, deny.

    Usage in comments.py:
        allowed, reason = check_command_permission(cmd, repo, author, token, config)
        if not allowed:
            return f"## ⛔ Permission Denied\\n\\n{reason}"
    """
    # Normalize: "/merge" or "merge" both work
    cmd_key = cmd.lstrip("/")

    # Check 1: Is this a globally restricted command?
    full_cmd = f"/{cmd_key}"
    if full_cmd not in RESTRICTED_COMMANDS:
        # Also check config-level maintainer_only list
        if not config.is_maintainer_only(cmd_key):
            return True, ""

    # Command requires elevated permissions
    if not is_maintainer(repo, author, token):
        perm = get_user_permission(repo, author, token)
        log.warning(
            f"auth.denied cmd={cmd} user={author} repo={repo} perm={perm}"
        )
        return False, (
            f"`{cmd}` requires **write/maintain/admin** access on this repository.\n\n"
            f"Your current access level: `{perm or 'none'}`\n\n"
            f"Contact a repository maintainer if you need this action performed."
        )

    log.info(f"auth.allowed cmd={cmd} user={author} repo={repo}")
    return True, ""


def invalidate_permission_cache(repo: str = None, user: str = None):
    """Force-clear permission cache. Call when team membership changes."""
    with _perm_lock:
        if repo and user:
            _perm_cache.pop((repo, user), None)
        elif repo:
            keys = [k for k in _perm_cache if k[0] == repo]
            for k in keys:
                del _perm_cache[k]
        else:
            _perm_cache.clear()
