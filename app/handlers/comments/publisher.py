"""
app/handlers/comments/publisher.py
Commands that write to GitHub: /merge, /apply, /rollback, /release,
/runtests, /notify, /security, /secfull.
"""

from __future__ import annotations

import logging
import os
import re

import contextlib
from app.github.client import GitHubError
from app.github.helpers import fmt_error
import app.handlers.comments as hc


def gh_get(*a, **kw):
    return hc.gh_get(*a, **kw)


def gh_post(*a, **kw):
    return hc.gh_post(*a, **kw)


def gh_put(*a, **kw):
    return hc.gh_put(*a, **kw)


def gh_delete(*a, **kw):
    return hc.gh_delete(*a, **kw)


class RouterProxy:
    def __getattr__(self, name):
        return getattr(hc.router, name)


router = RouterProxy()

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

    # Take safety snapshot first — abort if it fails
    try:
        take_snapshot(repo, token, trigger=f"pre_rollback_by_{author}")
    except Exception as exc:
        log.error(f"cmd_rollback safety snapshot failed: {exc}")
        return f"## ⚠️ Rollback Aborted\n\nCould not create a safety snapshot before rolling back.\n\nError: `{str(exc)[:200]}`\n\nRollback was **not** performed."

    restored: list[str] = []
    failed: list[str] = []

    for action in reversed(bot_actions):
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
            else:
                log.warning(f"cmd_rollback: unknown action type {action_type!r}, skipping")

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


def cmd_runtests(repo: str, token_or_issue_number: str | int, token: str | None = None) -> str:
    """Trigger CI workflow via workflow_dispatch."""
    actual_token = token if token is not None else str(token_or_issue_number)
    try:
        repo_data = gh_get(f"/repos/{repo}", actual_token)
        default_branch = repo_data.get("default_branch", "main")
        workflows_data = gh_get(f"/repos/{repo}/actions/workflows", actual_token)
        all_workflows = (
            workflows_data.get("workflows", []) if isinstance(workflows_data, dict) else []
        )

        TEST_NAMES = ("test", "ci", "pytest", "check", "lint", "build")
        test_workflow = next(
            (
                wf
                for wf in all_workflows
                if any(
                    n in wf.get("path", "").lower() or n in wf.get("name", "").lower()
                    for n in TEST_NAMES
                )
            ),
            None,
        )

        if not test_workflow:
            wf_names = [w.get("name", w.get("path", "?")) for w in all_workflows[:5]]
            existing = (
                f"\nExisting workflows: {', '.join(f'`{n}`' for n in wf_names)}" if wf_names else ""
            )
            return (
                f"## ⚠️ No Test Workflow Found{existing}\n\n"
                "Create a workflow (e.g. test.yml or ci.yml) with `workflow_dispatch` trigger to enable `/runtests`."
            )

        wf_id = test_workflow["id"]
        wf_name = test_workflow.get("name", "Test workflow")
        wf_file = test_workflow.get("path", "").split("/")[-1]
        wf_url = f"https://github.com/{repo}/actions/workflows/{wf_file}"

        try:
            gh_post(
                f"/repos/{repo}/actions/workflows/{wf_id}/dispatches",
                actual_token,
                {"ref": default_branch},
            )
        except GitHubError as exc:
            if exc.status_code == 422:
                return (
                    f"## ⚠️ Workflow Cannot Be Dispatched\n\n"
                    f"Add `workflow_dispatch:` trigger to `{wf_file}`."
                )
            if exc.status_code == 403:
                return "## ⚠️ Permission Denied\n\nGitHub App needs `actions: write` permission."
            raise

        return (
            f"## 🧪 Tests Triggered\n\n"
            f"**Workflow:** `{wf_name}`\n"
            f"**Branch:** `{default_branch}`\n\n"
            f"[View runs]({wf_url})"
        )

    except GitHubError as exc:
        return f"## ⚠️ Could not trigger tests: `{str(exc)[:200]}`"
    except Exception as exc:
        log.error(f"cmd_runtests error: {exc}")
        return f"## ⚠️ Could not trigger tests: `{str(exc)[:200]}`"


def cmd_notify(
    repo: str,
    issue_number: int,
    issue: dict,
    token: str,
    cmd_args: str,
) -> str:
    """Send Discord/Slack notification about this issue or PR."""
    discord_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    slack_url = os.environ.get("SLACK_WEBHOOK_URL", "")

    if not discord_url and not slack_url:
        return (
            "## ⚠️ Notifications Not Configured\n\n"
            "Add `DISCORD_WEBHOOK_URL` or `SLACK_WEBHOOK_URL` to your Render env vars."
        )

    try:
        from app.github.notifications import send_rich_discord

        title = issue.get("title", f"Issue #{issue_number}")
        is_pr = "pull_request" in issue
        labels = [lb.get("name", "") for lb in issue.get("labels", [])]
        kind = "PR" if is_pr else "Issue"
        url = issue.get("html_url", f"https://github.com/{repo}/issues/{issue_number}")
        custom_msg = (cmd_args or "").strip()

        color = 0x5865F2  # Discord blurple
        for lb in labels:
            lb_l = lb.lower()
            if any(w in lb_l for w in ("bug", "security", "critical")):
                color = 0xE74C3C
                break
            if any(w in lb_l for w in ("feature", "enhancement")):
                color = 0x2ECC71
                break

        desc_parts = [f"**Repo:** `{repo}`", f"**Labels:** {', '.join(labels) or 'none'}"]
        if custom_msg:
            desc_parts.append(f"**Note:** {custom_msg[:200]}")

        success, msg = send_rich_discord(
            title=f"🔔 {kind} #{issue_number} — {title[:80]}",
            description="\n".join(desc_parts),
            color=color,
            fields=[
                {"name": "Type", "value": kind, "inline": True},
                {"name": "Number", "value": f"#{issue_number}", "inline": True},
                {"name": "Repo", "value": repo, "inline": False},
            ],
            url=url,
        )

        channels = [c for c, u in [("Discord", discord_url), ("Slack", slack_url)] if u]
        if success:
            return (
                f"## 🔔 Notification Sent\n\n"
                f"Alert posted to: **{', '.join(channels)}**\n\n"
                f"**{kind} #{issue_number}:** {title[:80]}"
            )
        return f"## ⚠️ Notification Failed\n\nWebhook error: `{msg[:200]}`"

    except Exception as exc:
        log.error(f"cmd_notify error: {exc}")
        return f"## ⚠️ Notify error: `{str(exc)[:200]}`"


def cmd_security(repo: str, issue_number: int, issue: dict, token: str) -> str:
    """Scan PR files for secrets and vulnerable dependencies."""
    if "pull_request" not in issue:
        return "## ℹ️ `/security` works best on Pull Requests."

    try:
        from app.security.enhanced_secrets import format_findings as fmt_secrets, scan_diff
        from app.security.dependencies import scan_requirements_txt, format_dep_findings

        pr_files = gh_get(f"/repos/{repo}/pulls/{issue_number}/files", token)
        all_findings = []
        for f in pr_files[:10]:
            patch = f.get("patch", "")
            if patch:
                all_findings.extend(scan_diff(patch, file_path=f.get("filename", "")))

        dep_findings = []
        for f in pr_files:
            if f["filename"] == "requirements.txt":
                import base64

                raw = gh_get(f"/repos/{repo}/contents/{f['filename']}", token)
                content = base64.b64decode(raw["content"]).decode()
                dep_findings.extend(scan_requirements_txt(content))

        lines = ["## 🔒 Security Scan Results\n"]
        lines.append(
            fmt_secrets(all_findings, repo)
            if all_findings
            else "✅ **No secrets detected** in changed files.\n"
        )
        lines.append(
            format_dep_findings(dep_findings)
            if dep_findings
            else "✅ **No vulnerable dependencies** found.\n"
        )
        return "\n\n".join(lines)

    except Exception as exc:
        return fmt_error("Security scan failed", exc)


def cmd_secfull(repo: str, token: str) -> str:
    """Full repository security scan."""
    try:
        from app.security.scanner import run_security_scan

        report = run_security_scan(repo, token)
        return report.to_markdown(include_low=True)
    except Exception as exc:
        return fmt_error("Security scan failed", exc)
