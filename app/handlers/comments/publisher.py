"""
app/handlers/comments/publisher.py
Commands that write to GitHub: /merge, /apply, /rollback, /release,
/runtests, /notify, /security, /secfull.
"""

from __future__ import annotations

import logging
import re

import contextlib
from app.github.client import GitHubError
from app.github.helpers import fmt_error
from ._client import gh_get, gh_post, gh_put, gh_delete, router  # noqa: F401  (re-exported: tests patch these names)


log = logging.getLogger(__name__)


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
        reviews = gh_get(f"/repos/{repo}/pulls/{issue_number}/reviews", token)
        commit_sha = pr["head"]["sha"]
        check_runs = gh_get(f"/repos/{repo}/commits/{commit_sha}/check-runs", token)

        from app.core.guardrails import check_pr_auto_merge

        guard = check_pr_auto_merge(pr, check_runs.get("check_runs", []), reviews, config)
        if not guard.passed:
            return f"## 🚫 Cannot Merge\n\n**Reason:** {guard.reason}"

        head_branch = pr["head"]["ref"]
        base_branch = pr["base"]["ref"]
        result = gh_put(
            f"/repos/{repo}/pulls/{issue_number}/merge",
            token,
            {
                "commit_title": f"feat: merge {head_branch} via /merge by @{author}",
                "merge_method": "merge",
            },
        )

        if result.get("merged"):
            # Audit log — /merge is irreversible
            try:
                import json as _j
                import time as _t
                from app.core.redis_client import get_redis as _r

                _r().lpush(
                    "audit:merge",
                    _j.dumps(
                        {
                            "repo": repo,
                            "pr": issue_number,
                            "by": author,
                            "at": int(_t.time()),
                            "sha": result.get("sha", "")[:12],
                        }
                    ),
                )
                _r().ltrim("audit:merge", 0, 999)
            except Exception:
                pass  # audit failure must not block the merge

            with contextlib.suppress(Exception):
                gh_delete(f"/repos/{repo}/git/refs/heads/{head_branch}", token)

            # Learning loop: merging a bot-authored autofix branch is the
            # strongest acceptance signal we get.
            if head_branch.startswith("fix/bot-issue-"):
                with contextlib.suppress(Exception):
                    from app.core.learning import record_autofix_merged

                    m = re.search(r"issue-(\d+)", head_branch)
                    record_autofix_merged(repo, issue_number, int(m.group(1)) if m else 0)

                # Write it to the brain too. Nothing in the application called
                # remember() before V7, so the memory store only ever held what
                # a backup restored into it.
                with contextlib.suppress(Exception):
                    from app.intelligence.memory import remember

                    remember(
                        repo,
                        f"Accepted fix merged for #{issue_number}: {issue.get('title', '')}",
                        kind="fix",
                        meta={"pr": issue_number, "by": author},
                    )

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
            branches = gh_get(f"/repos/{repo}/branches?per_page=100", token)
            fix_branches = [
                b["name"]
                for b in (branches if isinstance(branches, list) else [])
                if b.get("name", "").startswith("fix/bot-issue-")
            ]
            if not fix_branches:
                return "## ℹ️ No Autofix Branches Found\n\nNo `fix/bot-issue-*` branches exist yet.\n\nRun `/autofix` on an issue first, then `/apply <branch>`."
            branch_list = "\n".join(f"- `{b}`" for b in fix_branches[:10])
            return f"## 🌱 Available Autofix Branches\n\n{branch_list}\n\nReply with `/apply <branch-name>` to open a PR."

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
        m = re.search(r"issue-(\d+)", branch)
        if m:
            issue_ref = f"\n\nCloses #{m.group(1)}"

        pr = gh_post(
            f"/repos/{repo}/pulls",
            token,
            {
                "title": f"fix: autofix for issue #{issue_number}",
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

        # Learning loop: a maintainer choosing to open a PR from a bot fix IS
        # the acceptance signal. Feeds get_pattern_summary() → future /fix prompts.
        with contextlib.suppress(Exception):
            from app.core.learning import record_fix_accepted

            record_fix_accepted(repo, issue_number, "autofix")

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
        get_snapshot_by_number,
        format_snapshot_list,
        format_rollback_result,
        take_snapshot,
    )

    args = (cmd_args or "").strip()

    if not args:
        return format_snapshot_list(repo)

    parts = args.split()
    n_str = parts[0]
    confirm = len(parts) > 1 and parts[1].lower() == "confirm"

    try:
        n = int(n_str)
    except ValueError:
        return f"## ⚠️ Invalid Snapshot Number\n\n`{n_str}` is not a number.\n\nUsage: `/rollback` → list, `/rollback 3` → preview, `/rollback 3 confirm` → execute"

    snap = get_snapshot_by_number(repo, n)
    if not snap:
        return f"## ⚠️ Snapshot #{n} Not Found\n\nUse `/rollback` to see available snapshots (max 10, expire after 7 days)."

    bot_actions = snap.get("bot_actions", [])
    snap_ts = snap.get("timestamp", "")[:16].replace("T", " ")
    action_lines = (
        "\n".join(
            f"- `{a.get('type', 'unknown')}` on #{a.get('number', '?')}" for a in bot_actions[:5]
        )
        or "- No recorded actions"
    )

    if not confirm:
        return f"## ⚠️ Confirm Rollback\n\n**Snapshot #{n}** — `{snap_ts}` trigger: `{snap.get('trigger', 'unknown')}`\n\n**Actions to undo:**\n{action_lines}\n\n{'*(and more...)*' if len(bot_actions) > 5 else ''}\n\n**Proceed:** `/rollback {n} confirm`\n**Cancel:** ignore this message"

    # Take a safety snapshot first — abort if it fails.
    #
    # take_snapshot() catches its own exceptions and returns None, so the
    # try/except that used to guard this could never fire: a failed safety
    # snapshot was swallowed and the rollback proceeded anyway, with nothing
    # to undo it. The return value is what has to be checked.
    try:
        safety_id = take_snapshot(repo, token, trigger=f"pre_rollback_by_{author}")
    except Exception as exc:  # defensive — take_snapshot should not raise
        log.error(f"cmd_rollback safety snapshot raised: {exc}")
        safety_id = None

    if not safety_id:
        log.error(f"cmd_rollback.aborted repo={repo} — safety snapshot failed")
        return (
            "## ⚠️ Rollback Aborted\n\n"
            "Could not create a safety snapshot before rolling back, so there "
            "would be no way to undo this. Rollback was **not** performed.\n\n"
            "This usually means Redis or the GitHub API is unavailable. "
            "Check `/health` and try again."
        )

    restored: list[str] = []
    failed: list[str] = []

    # Newest action first. bot_actions arrives newest-first already, so the
    # previous `reversed()` undid oldest-first — and undoing is LIFO. With two
    # recorded title edits on one PR (X->Y then Y->Z), oldest-first restores X
    # and then Y, leaving the intermediate title; newest-first restores Y then
    # X, which is the original.
    for action in bot_actions:
        action_type = action.get("type", "")
        num = action.get("number")
        try:
            if action_type == "create_issue" and num:
                gh_put(f"/repos/{repo}/issues/{num}", token, {"state": "closed"})
                restored.append(f"Closed issue #{num}: {action.get('title', '')[:50]}")

            elif action_type == "edit_pr_title" and num:
                old_title = action.get("old_title", "")
                if old_title:
                    gh_put(f"/repos/{repo}/pulls/{num}", token, {"title": old_title})
                    restored.append(f"Reverted PR #{num} title → `{old_title[:50]}`")
                else:
                    failed.append(f"edit_pr_title #{num}: no old_title recorded")

            elif action_type == "add_labels" and num:
                label_errors = []
                for lbl in action.get("labels", []):
                    try:
                        gh_delete(f"/repos/{repo}/issues/{num}/labels/{lbl}", token)
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

    if not bot_actions:
        restored.append("No automated actions were recorded in this snapshot")

    return format_rollback_result(repo, snap, restored, failed)


def cmd_release(repo: str, token: str, author: str) -> str:
    """Draft a GitHub release from commits since last tag."""
    try:
        tags = gh_get(f"/repos/{repo}/tags?per_page=10", token)
        commits = gh_get(f"/repos/{repo}/commits?per_page=20", token)

        if not commits:
            return "## ⚠️ No Commits Found\n\nThis repository has no commits yet."

        existing_tags = [t["name"] for t in (tags if isinstance(tags, list) else [])]
        latest_tag = existing_tags[0] if existing_tags else "v0.0.0"
        commit_list = "\n".join(
            f"- {c['commit']['message'].split(chr(10))[0][:120]}" for c in commits[:15]
        )

        from .reviewer import _bump_version
        from app.handlers.comments import router

        r, _ = router.ask(
            "Technical writer. Generate a GitHub release. JSON only.",
            f"""Generate release notes for the next version after {latest_tag}.
Existing tags (DO NOT reuse): {", ".join(existing_tags[:10]) or "none"}
Commits:
{commit_list}

Return JSON:
{{
  "version": "next semver e.g. v1.2.3",
  "title": "short release title",
  "highlights": ["key change 1"],
  "breaking_changes": [],
  "release_notes": "full markdown notes"
}}""",
            task="changelog",
        )

        version = r.get("version", "").strip()
        if not version or not re.match(r"^v\d+\.\d+\.\d+", version):
            version = _bump_version(latest_tag)
            log.warning(f"cmd_release: bad version from LLM, using {version}")
        if version in existing_tags:
            version = _bump_version(version)

        try:
            release = gh_post(
                f"/repos/{repo}/releases",
                token,
                {
                    "tag_name": version,
                    "name": r.get("title", version),
                    "body": r.get("release_notes", f"Release {version}"),
                    "draft": True,
                },
            )
        except GitHubError as exc:
            if exc.status_code == 422:
                return (
                    f"## ⚠️ Tag Already Exists\n\n"
                    f"`{version}` already exists. Try again — bot will auto-bump."
                )
            raise

        highlights_md = (
            "\n".join(f"- {h}" for h in r.get("highlights", [])[:5])
            or "_No highlights identified._"
        )
        breaking = r.get("breaking_changes", [])
        breaking_md = "\n".join(f"- ⚠️ {b}" for b in breaking[:3]) if breaking else ""

        out = (
            f"## 🚀 Draft Release Created\n\n"
            f"**Version:** `{version}` | **Status:** Draft\n\n"
            f"### Highlights\n{highlights_md}\n"
        )
        if breaking_md:
            out += f"\n### ⚠️ Breaking Changes\n{breaking_md}\n"
        out += (
            f"\n[View & Edit Draft]({release.get('html_url', '')})\n\n"
            f"> Review and publish when ready."
        )
        return out

    except GitHubError as exc:
        return f"## ⚠️ Release failed (GitHub API): `{str(exc)[:200]}`"
    except Exception as exc:
        log.error(f"cmd_release error: {exc}")
        return f"## ⚠️ Release failed: `{str(exc)[:200]}`"


# Moved to integrations.py when this module crossed the package's line ceiling.
# Re-exported because `from .publisher import cmd_runtests` is how the package
# __init__ and the test suite reach them.
from .integrations import cmd_notify, cmd_runtests  # noqa: E402,F401
