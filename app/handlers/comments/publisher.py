"""
app/handlers/comments/publisher.py
Commands that write to GitHub: /merge, /apply, /rollback, /release,
/runtests, /notify, /security, /secfull.
"""

from __future__ import annotations

import logging
import re

import contextlib
from urllib.parse import quote
from app.github.client import GitHubError
from app.github.helpers import fmt_error
from ._client import gh_get, gh_post, gh_put, gh_patch, gh_delete, router  # noqa: F401  (re-exported: tests patch these names)


log = logging.getLogger(__name__)


_CHECK_PAGES = 5


def _all_check_runs(repo: str, sha: str, token: str) -> tuple[list, bool]:
    """
    Every check run on `sha`, and whether that is provably all of them.

    One unpaginated GET returns GitHub's default of 30, so on a repository
    with more checks than that a failing 31st was never seen. Returns
    (runs, False) when the runs fetched fall short of `total_count` — the
    caller must refuse rather than merge on a partial picture.
    """
    runs: list = []
    total = 0
    for page in range(1, _CHECK_PAGES + 1):
        data = gh_get(f"/repos/{repo}/commits/{sha}/check-runs?per_page=100&page={page}", token)
        if not isinstance(data, dict):
            return runs, False
        batch = data.get("check_runs") or []
        total = int(data.get("total_count", len(batch)) or 0)
        runs.extend(batch)
        if len(runs) >= total or len(batch) < 100:
            break
    return runs, len(runs) >= total


_REVIEW_PAGES = 10


def _all_reviews(repo: str, number: int, token: str) -> list:
    """
    Every review on the PR. Reviews come oldest first, so with one page of
    100 a change request made after the 100th review was never seen and the
    "no blocking reviews" guard passed. gh_get raises on failure, so an
    unreadable page refuses the merge rather than reading as "no reviews".
    """
    out: list = []
    for page in range(1, _REVIEW_PAGES + 1):
        batch = gh_get(f"/repos/{repo}/pulls/{number}/reviews?per_page=100&page={page}", token)
        if not isinstance(batch, list):
            raise ValueError("reviews response was not a list")
        out.extend(batch)
        if len(batch) < 100:
            return out
    raise ValueError(f"more than {_REVIEW_PAGES * 100} reviews; cannot read them all")


def cmd_merge(
    repo: str,
    issue_number: int,
    issue: dict,
    token: str,
    author: str,
    config,
) -> str:
    """Merge a PR after guardrail checks pass."""
    if "pull_request" not in issue:
        return "## ℹ️ `/merge` only works on Pull Requests."

    try:
        pr = gh_get(f"/repos/{repo}/pulls/{issue_number}", token)
        # A merged or closed PR fell through to the guard, whose first
        # answer was "GitHub hasn't computed mergeability yet — retry".
        if pr.get("merged"):
            return "## ℹ️ Already Merged\n\nThis pull request has already been merged."
        if pr.get("state") == "closed":
            return "## ℹ️ PR Is Closed\n\nReopen the pull request before merging it."
        # A PR whose source fork was deleted carries a null `head`. This
        # raised inside cmd_merge's try/except, so the user was told "Merge
        # failed" with a TypeError rather than the real reason.
        head = pr.get("head") or {}
        commit_sha = head.get("sha", "")
        if not commit_sha:
            return (
                "## 🚫 Cannot Merge\n\n**Reason:** this PR has no head commit — "
                "its source branch or fork was deleted."
            )
        reviews = _all_reviews(repo, issue_number, token)
        checks, complete = _all_check_runs(repo, commit_sha, token)
        if not complete:
            return (
                "## 🚫 Cannot Merge\n\n**Reason:** could not read every check run on "
                f"`{commit_sha[:8]}`, so it cannot be shown that they all passed."
            )
        status = gh_get(f"/repos/{repo}/commits/{commit_sha}/status?per_page=100", token)
        statuses = status.get("statuses", []) if isinstance(status, dict) else []

        from app.core.guardrails import check_pr_auto_merge

        guard = check_pr_auto_merge(pr, checks, reviews, config, statuses=statuses)
        if not guard.passed:
            return f"## 🚫 Cannot Merge\n\n**Reason:** {guard.reason}"

        head_branch = head.get("ref", "")
        base_branch = (pr.get("base") or {}).get("ref", "")
        result = gh_put(
            f"/repos/{repo}/pulls/{issue_number}/merge",
            token,
            {
                # Not "feat: …": /release reads `feat:` subjects as features,
                # so every /merge of a typo fix bumped the minor version.
                "commit_title": f"Merge pull request #{issue_number} from {head_branch} "
                f"(/merge by {author})",
                "merge_method": "merge",
                # Merge exactly the commit the checks above were read for.
                # Without it, a commit pushed between the check and the merge
                # was merged unchecked; with it, GitHub refuses (409) instead.
                "sha": commit_sha,
            },
        )

        if result.get("merged"):
            # /merge is irreversible: record who did it. Never blocks the merge;
            # an unrecorded entry is logged as an error (app/core/audit.py).
            from app.core.audit import record_merge

            record_merge(repo, issue_number, author, result.get("sha", ""))

            # Only a branch that lives in THIS repository. A fork PR's head
            # branch is in the fork; deleting `heads/<its name>` here deleted
            # whatever same-named branch this repository happened to have.
            #
            # And only a branch this App created (autofix). Any other head
            # branch is the repository's: GitHub deletes it itself when the
            # repo enables "automatically delete head branches", and when it
            # does not, the branch is being kept on purpose — merging
            # develop -> staging deleted develop, and GitHub then closed every
            # PR targeting it. The name is quoted: `fix#12` deleted `fix`.
            head_repo = (head.get("repo") or {}).get("full_name", "")
            if head_branch.startswith("fix/bot-issue-") and head_repo == repo:
                with contextlib.suppress(Exception):
                    gh_delete(f"/repos/{repo}/git/refs/heads/{quote(head_branch, safe='/')}", token)

            # The learning record for an autofix branch is written from the
            # PR's `closed` event (pull_request/outcomes.py), which sees every
            # merge — this button and GitHub's — and every rejection. Writing
            # it here as well counted a /merge twice.

            return (
                f"## ✅ Merged!\n\n"
                f"**`{head_branch}`** → **`{base_branch}`**\n"
                f"SHA: `{result.get('sha', '')[:8]}`"
            )

        return f"## ⚠️ Merge failed: {result.get('message', 'Unknown error')}"

    except Exception as exc:
        return fmt_error("Merge error", exc)


def cmd_apply(
    repo: str,
    issue_number: int,
    token: str,
    cmd_args: str,
) -> str:
    """Create a PR from an autofix branch, or list available branches."""
    branch = cmd_args.strip() if cmd_args else ""

    if branch and (".." in branch or branch.startswith("/") or " " in branch or len(branch) > 200):
        return f"## ⚠️ Invalid Branch Name\n\n`{branch[:80]}` is not a valid branch name.\n\nUsage: `/apply fix/bot-issue-42`"

    try:
        repo_data = gh_get(f"/repos/{repo}", token)
        default_branch = repo_data.get("default_branch", "main")

        if not branch:
            # Only the prefixed refs. This read the first 100 branches and
            # filtered them, so on a larger repository it answered "No Autofix
            # Branches Found" while they existed.
            refs = gh_get(f"/repos/{repo}/git/matching-refs/heads/fix/bot-issue-", token)
            fix_branches = [
                r["ref"].removeprefix("refs/heads/")
                for r in (refs if isinstance(refs, list) else [])
                if isinstance(r, dict) and str(r.get("ref", "")).startswith("refs/heads/")
            ]
            if not fix_branches:
                return "## ℹ️ No Autofix Branches Found\n\nNo `fix/bot-issue-*` branches exist yet.\n\nRun `/autofix` on an issue first, then `/apply <branch>`."
            branch_list = "\n".join(f"- `{b}`" for b in fix_branches[:10])
            more = f"\n- … and {len(fix_branches) - 10} more" if len(fix_branches) > 10 else ""
            return f"## 🌱 Available Autofix Branches\n\n{branch_list}{more}\n\nReply with `/apply <branch-name>` to open a PR."

        # Verify branch exists
        try:
            gh_get(f"/repos/{repo}/branches/{branch}", token)
        except GitHubError as exc:
            if exc.status_code == 404:
                return f"## ⚠️ Branch Not Found\n\n`{branch}` does not exist in `{repo}`.\n\nUse `/apply` (no args) to see available branches."
            raise

        # Check for existing open PR
        owner = repo.split("/")[0] if "/" in repo else repo
        existing = gh_get(f"/repos/{repo}/pulls?head={owner}:{branch}&state=open&per_page=5", token)
        if isinstance(existing, list) and existing:
            pr = existing[0]
            return f"## ℹ️ PR Already Exists\n\n[#{pr['number']} — {pr['title'][:60]}]({pr['html_url']})"

        issue_ref = ""
        # The issue the fix is for is the one its branch names, not the one
        # /apply was typed on: the title said "#50" over "Closes #42".
        fixed = issue_number
        m = re.search(r"issue-(\d+)", branch)
        if m:
            fixed = int(m.group(1))
            issue_ref = f"\n\nCloses #{fixed}"

        pr = gh_post(
            f"/repos/{repo}/pulls",
            token,
            {
                "title": f"fix: autofix for issue #{fixed}",
                "head": branch,
                "base": default_branch,
                "body": (
                    f"## 🤖 Autofix PR\n\n"
                    f"Requested by `/apply` on issue #{issue_number}.\n"
                    f"Branch: `{branch}` → `{default_branch}`"
                    f"{issue_ref}\n\n"
                    "> ⚠️ AI-generated — review all changes before merging."
                ),
                "draft": False,
            },
        )

        # Whether the PR is then merged or rejected is recorded from its
        # `closed` event (pull_request/outcomes.py).
        with contextlib.suppress(Exception):
            from app.intelligence.memory import remember

            remember(
                repo,
                f"Maintainer opened a PR from bot branch {branch} for issue #{issue_number}",
                kind="pattern",
            )

        return f"## ✅ PR Created\n\n**PR #{pr.get('number', '?')}:** [{pr.get('title', '')}]({pr.get('html_url', '')})\n\n**Branch:** `{branch}` → `{default_branch}`\n\n> Review changes carefully before merging."

    except GitHubError as exc:
        if exc.status_code == 422:
            return f"## ⚠️ Cannot Create PR\n\nGitHub 422: `{str(exc)[:200]}`\n\nPossible: branch up to date, or closed PR already exists."
        return f"## ⚠️ Apply Failed\n\n`{str(exc)[:200]}`"
    except Exception as exc:
        log.error(f"cmd_apply unexpected error: {exc}")
        return f"## ⚠️ Apply Failed\n\nUnexpected error: `{str(exc)[:200]}`"


def cmd_rollback(
    repo: str,
    issue_number: int,
    token: str,
    cmd_args: str,
    author: str,
) -> str:
    """Rollback command: preview/list snapshots and execute rollback."""
    from app.core.snapshot import (
        SnapshotsUnavailable,
        get_snapshot_by_number,
        format_snapshot_list,
        format_rollback_result,
    )

    args = (cmd_args or "").strip()
    unavailable = (
        "## ⚠️ Snapshots Unavailable\n\nThe snapshot store (Redis) could not be read: "
        "`{}`. This is not a report that nothing was recorded — nothing was rolled back."
    )

    if not args:
        try:
            return format_snapshot_list(repo)
        except SnapshotsUnavailable as e:
            return unavailable.format(str(e)[:120])

    parts = args.split()
    n_str = parts[0]
    confirm = len(parts) > 1 and parts[1].lower() == "confirm"

    try:
        n = int(n_str)
    except ValueError:
        return f"## ⚠️ Invalid Snapshot Number\n\n`{n_str}` is not a number.\n\nUsage: `/rollback` → list, `/rollback 3` → preview, `/rollback 3 confirm` → execute"

    try:
        snap = get_snapshot_by_number(repo, n)
    except SnapshotsUnavailable as e:
        return unavailable.format(str(e)[:120])
    if not snap:
        return f"## ⚠️ Snapshot #{n} Not Found\n\nUse `/rollback` to see available snapshots (max 10, expire after 7 days)."

    snap_id = str(snap.get("id") or "")
    # Numbers shift when a newer snapshot appears; the preview names the id,
    # and a confirm that carries one must still be looking at that snapshot.
    given_id = parts[2] if confirm and len(parts) > 2 else ""
    if given_id and not snap_id.startswith(given_id):
        return (
            f"## ⚠️ Snapshot #{n} Has Changed\n\nSnapshot #{n} is now `{snap_id[:8]}`, not "
            f"`{given_id}` — a newer snapshot was recorded since the preview. Run "
            f"`/rollback` to see the list again. Nothing was rolled back."
        )

    bot_actions = snap.get("bot_actions", [])
    snap_ts = snap.get("timestamp", "")[:16].replace("T", " ")
    action_lines = (
        "\n".join(
            f"- `{a.get('type', 'unknown')}` on #{a.get('number', '?')}" for a in bot_actions[:5]
        )
        or "- No recorded actions"
    )

    if not confirm:
        return f"## ⚠️ Confirm Rollback\n\n**Snapshot #{n}** (`{snap_id[:8]}`) — `{snap_ts}` trigger: `{snap.get('trigger', 'unknown')}`\n\n**Actions to undo:**\n{action_lines}\n\n{'*(and more...)*' if len(bot_actions) > 5 else ''}\n\nAn action is undone only while it is still the bot's: a title or description someone has edited since is left as it is.\n\n**Proceed:** `/rollback {n} confirm {snap_id[:8]}`\n**Cancel:** ignore this message"

    # Once per snapshot, claimed before any write. Rolling the same snapshot
    # back again re-issued every write — re-closing issues, re-setting titles
    # over later edits — and two concurrent confirms both ran. The "safety
    # snapshot" this replaces recorded no actions, so it could undo nothing,
    # while each one pushed a real snapshot out of the last ten.
    from app.core.redis_client import get_redis

    done_key = f"snapshot_rolled_back:{repo}:{snap_id}"
    try:
        claimed = get_redis().set(done_key, author or "?", nx=True, ex=8 * 86400)
    except Exception as e:
        return unavailable.format(f"{type(e).__name__}: {e}"[:120])
    if not claimed:
        return (
            f"## ℹ️ Already Rolled Back\n\nSnapshot #{n} (`{snap_id[:8]}`) has already been "
            "rolled back. Nothing was done again."
        )

    restored: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []

    # Newest action first. bot_actions arrives newest-first already, so the
    # previous `reversed()` undid oldest-first — and undoing is LIFO. With two
    # recorded title edits on one PR (X->Y then Y->Z), oldest-first restores X
    # and then Y, leaving the intermediate title; newest-first restores Y then
    # X, which is the original.
    # PATCH for both edits below. They were gh_put, and GitHub has no PUT on an
    # issue or a pull request, so /rollback could never close an issue the bot
    # opened or restore a title it changed: each attempt was a 404, recorded
    # under "failed" — the one command meant to undo the bot's mistakes could
    # not undo the two it was written for.
    for action in bot_actions:
        action_type = action.get("type", "")
        num = action.get("number")
        try:
            if action_type == "create_issue" and num:
                current = gh_get(f"/repos/{repo}/issues/{num}", token) or {}
                if current.get("state") == "closed":
                    skipped.append(f"Issue #{num} is already closed")
                    continue
                gh_patch(f"/repos/{repo}/issues/{num}", token, {"state": "closed"})
                restored.append(f"Closed issue #{num}: {action.get('title', '')[:50]}")

            elif action_type == "edit_pr_title" and num:
                # Title and/or description, whichever the bot rewrote — and
                # only where the PR still shows what the bot wrote. A title
                # someone changed after the bot was overwritten with the one
                # before the bot.
                current = gh_get(f"/repos/{repo}/pulls/{num}", token) or {}
                revert, kept = {}, []
                for field, label in (("title", "title"), ("body", "description")):
                    if f"old_{field}" not in action or (
                        field == "title" and not action.get("old_title")
                    ):
                        continue
                    if f"new_{field}" not in action:
                        kept.append(f"{label} (this record cannot show it is still the bot's)")
                    elif (current.get(field) or "") != (action[f"new_{field}"] or ""):
                        kept.append(f"{label} (edited since)")
                    else:
                        revert[field] = action[f"old_{field}"] or ""
                if revert:
                    gh_patch(f"/repos/{repo}/pulls/{num}", token, revert)
                    what = " and ".join(k for k in ("title", "body") if k in revert).replace(
                        "body", "description"
                    )
                    restored.append(f"Reverted PR #{num} {what}")
                if kept:
                    skipped.append(f"PR #{num}: left the " + ", ".join(kept))
                if not revert and not kept:
                    failed.append(f"edit_pr_title #{num}: no old_title recorded")

            elif action_type == "add_labels" and num:
                label_errors = []
                for lbl in action.get("labels", []):
                    try:
                        # Quoted: bot labels contain spaces, colons and emoji,
                        # and a "/" or "#" would address another resource.
                        gh_delete(
                            f"/repos/{repo}/issues/{num}/labels/{quote(str(lbl), safe='')}", token
                        )
                    except GitHubError as le:
                        if le.status_code != 404:
                            label_errors.append(f"{lbl}: {str(le)[:40]}")
                    except Exception as le:
                        label_errors.append(f"{lbl}: {str(le)[:40]}")
                if label_errors:
                    failed.append(f"remove labels from #{num}: {'; '.join(label_errors)}")
                else:
                    restored.append(f"Removed labels from #{num}")
            elif action_type and not num:
                # A known type with no target is unusable, and silently
                # skipping it reported "Rollback Complete" for work not done.
                failed.append(f"{action_type}: no issue/PR number recorded")
            else:
                # Reported, not just logged. This branch means the snapshot
                # holds an action this version cannot undo — the user needs to
                # know it survived the rollback rather than reading "Complete".
                log.warning(f"cmd_rollback: unknown action type {action_type!r}, skipping")
                failed.append(
                    f"`{action_type or 'unrecognised'}` on #{num or '?'}: "
                    f"no undo is implemented for this action type"
                )

        except GitHubError as exc:
            failed.append(f"{action_type} #{num or '?'}: {str(exc)[:80]}")
        except Exception as exc:
            log.error(f"cmd_rollback action {action_type} failed: {exc}")
            failed.append(f"{action_type} #{num or '?'}: {str(exc)[:60]}")

    if not restored and failed:
        # Nothing was undone: let it be tried again once the cause is fixed.
        with contextlib.suppress(Exception):
            get_redis().delete(done_key)
    restored.extend(f"Skipped — {s}" for s in skipped)

    if not bot_actions:
        restored.append("No automated actions were recorded in this snapshot")

    return format_rollback_result(repo, snap, restored, failed)


# Moved to release.py when this module crossed the package's line ceiling.
# Re-exported: the package __init__, reviewer's /version and the tests import
# them from here.
from .release import _semver, cmd_release, next_version  # noqa: E402,F401


# Moved to integrations.py when this module crossed the package's line ceiling.
# Re-exported because `from .publisher import cmd_runtests` is how the package
# __init__ and the test suite reach them.
from .integrations import cmd_notify, cmd_runtests  # noqa: E402,F401
