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
_perm_cache: dict[tuple, tuple] = {}  # {(repo, user): (perm, timestamp)}
_perm_lock = threading.Lock()
_PERM_TTL = 300  # 5 minutes


MAINTAINER_PERMISSIONS = {"admin", "maintain", "write"}

# Sentinel returned when the permission API could not be consulted at all
# (403 from a missing App permission, 5xx, network failure). It is NOT a
# permission level — it means "unknown". Access is still denied (fail closed),
# but the denial message must not claim the user's level is "none": a repo
# owner reading "your access level: none" has no way to find the real problem.
PERMISSION_UNKNOWN = "unknown"

# Commands that require at least write/maintain/admin access
RESTRICTED_COMMANDS = {
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
}


def get_user_permission(repo: str, username: str, token: str) -> str:
    """
    Returns the GitHub permission level for `username` on `repo`.
    Values: "admin" | "maintain" | "write" | "read" | "none" | "unknown"

    Caches for 5 minutes to avoid hammering GitHub API.

    A 404 means GitHub answered and the user genuinely is not a collaborator →
    "none". Any other failure means the question was never answered →
    PERMISSION_UNKNOWN. Both deny access, but only the first is a fact about
    the user; conflating them made a misconfigured App indistinguishable from
    an unauthorised commenter and left `/autofix`, `/apply`, `/merge`,
    `/rollback`, `/release`, `/secfull` and `/ignore` silently dead with a
    message that told the repo owner they had no access to their own repo.

    "unknown" is deliberately NOT cached: a transient 5xx or a permission that
    an operator has just granted would otherwise stay broken for 5 minutes.
    """
    import time

    from app.core.metrics import metrics

    cache_key = (repo, username)
    now = time.time()

    with _perm_lock:
        if cache_key in _perm_cache:
            perm, ts = _perm_cache[cache_key]
            if now - ts < _PERM_TTL:
                return perm

    cacheable = True
    try:
        data = gh_get(
            f"/repos/{repo}/collaborators/{username}/permission",
            token,
        )
        perm = data.get("permission", "none")
        log.debug(f"auth.permission user={username} repo={repo} level={perm}")
    except GitHubError as e:
        if e.status_code == 404:
            # GitHub answered: not a collaborator.
            perm = "none"
        else:
            # 403 (App lacks the permission), 429, 5xx — question unanswered.
            log.error(
                f"auth.permission_check_failed user={username} repo={repo} "
                f"status={e.status_code} — denying (fail closed). If this is a 403, "
                f"the GitHub App installation is missing read access to repository "
                f"metadata/collaborators: {e}"
            )
            metrics.increment("auth.permission_check_failed")
            perm = PERMISSION_UNKNOWN
            cacheable = False
    except Exception as e:
        log.error(f"auth.permission_unexpected user={username} repo={repo}: {e}")
        metrics.increment("auth.permission_check_failed")
        perm = PERMISSION_UNKNOWN
        cacheable = False

    if cacheable:
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
    if (full_cmd not in RESTRICTED_COMMANDS) and not config.is_maintainer_only(cmd_key):
        return True, ""

    # Command requires elevated permissions. Resolve the level once — calling
    # is_maintainer() and then get_user_permission() issued a second lookup on
    # every denial, and for an uncached "unknown" that meant two live API calls.
    perm = get_user_permission(repo, author, token)

    if perm in MAINTAINER_PERMISSIONS:
        log.info(f"auth.allowed cmd={cmd} user={author} repo={repo}")
        return True, ""

    if perm == PERMISSION_UNKNOWN:
        # Deny (fail closed) but name the real problem — this is an operator
        # misconfiguration, not an access decision about `author`.
        log.warning(f"auth.denied_check_failed cmd={cmd} user={author} repo={repo}")
        return False, (
            f"The permission check for `{cmd}` could not be completed, so it was "
            f"denied as a precaution. **This is not a statement about your access "
            f"level.**\n\n"
            f"This usually means the GitHub App installation cannot read "
            f"collaborator permissions for `{repo}`. A repository admin should "
            f"re-check the App's installation permissions and repository access, "
            f"then retry.\n\n"
            f"Operators: see the `auth.permission_check_failed` metric on "
            f"`/metrics` and the `auth.permission_check_failed` log line for the "
            f"exact HTTP status."
        )

    log.warning(f"auth.denied cmd={cmd} user={author} repo={repo} perm={perm}")
    return False, (
        f"`{cmd}` requires **write/maintain/admin** access on this repository.\n\n"
        f"Your current access level: `{perm or 'none'}`\n\n"
        f"Contact a repository maintainer if you need this action performed."
    )


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
