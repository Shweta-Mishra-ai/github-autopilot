"""
app/handlers/comments/integrations.py
Commands that reach OUTSIDE GitHub: /runtests (dispatches a CI workflow) and
/notify (posts to Slack / Discord).

Split from publisher.py, which crossed the package's 600-line ceiling. The seam
is real rather than arbitrary: everything left in publisher.py mutates GitHub
state (merge, apply, rollback, release), while these two hand work to a system
GitHub does not own — so their failure modes, their credentials and their
"did it actually arrive?" questions are all different.

GitHub access goes through the same delegating wrappers used elsewhere in this
package, so `patch("app.handlers.comments.gh_get")` reaches these functions too.
"""

from __future__ import annotations

import logging
import os

from app.github.client import GitHubError
from ._client import gh_get, gh_post  # noqa: F401  (re-exported: tests patch these names)


log = logging.getLogger(__name__)


_TEST_WORDS = frozenset({"test", "tests", "ci", "pytest", "check", "checks", "lint", "unit"})
# A workflow named for any of these is never "the tests", whatever else its
# name says: "Release: build & publish to PyPI" matched "build" as a substring
# and /runtests published a release.
_NEVER_WORDS = frozenset({"release", "deploy", "publish", "pages", "production", "prod"})


def _words(text: str) -> set:
    import re

    return set(re.split(r"[^a-z0-9]+", (text or "").lower())) - {""}


def _is_test_workflow(wf: dict) -> bool:
    words = _words(wf.get("name", "")) | _words(wf.get("path", "").rsplit("/", 1)[-1])
    return bool(words & _TEST_WORDS) and not (words & _NEVER_WORDS)


def cmd_runtests(
    repo: str, token_or_issue_number: str | int, token: str | None = None, issue: dict | None = None
) -> str:
    """Trigger CI workflow via workflow_dispatch."""
    actual_token = token if token is not None else str(token_or_issue_number)
    try:
        repo_data = gh_get(f"/repos/{repo}", actual_token)
        default_branch = repo_data.get("default_branch", "main")
        workflows_data = gh_get(f"/repos/{repo}/actions/workflows", actual_token)
        all_workflows = (
            workflows_data.get("workflows", []) if isinstance(workflows_data, dict) else []
        )

        test_workflow = next((wf for wf in all_workflows if _is_test_workflow(wf)), None)

        # On a pull request, test the PR. This always dispatched the default
        # branch, so the "tests triggered" reply described a run of code the
        # PR does not contain. A fork's branch cannot be dispatched here, and
        # the reply says so instead of implying otherwise.
        ref, ref_note = default_branch, ""
        if issue and "pull_request" in issue and token is not None:
            try:
                pr = gh_get(f"/repos/{repo}/pulls/{int(token_or_issue_number)}", actual_token)
                head = pr.get("head") or {}
                if (head.get("repo") or {}).get("full_name") == repo and head.get("ref"):
                    ref = head["ref"]
                else:
                    ref_note = (
                        "\n\n> ℹ️ This PR comes from a fork, whose branch cannot be "
                        f"dispatched from here — this runs `{default_branch}`, not the PR."
                    )
            except Exception as e:
                log.debug(f"cmd_runtests.pr_lookup_failed: {e}")
                ref_note = f"\n\n> ℹ️ Could not read the PR's branch — this runs `{default_branch}`."

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
                {"ref": ref},
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
            f"**Branch:** `{ref}`\n\n"
            f"[View runs]({wf_url}){ref_note}"
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
        from app.github.notifications import send_rich_discord, send_rich_slack

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

        # Send to each CONFIGURED channel, and report what each one actually
        # did. Previously only Discord was ever contacted, while the success
        # message listed every configured channel — so a Slack-only setup was
        # told "Alert posted to: Slack" having sent nothing, and a both-channels
        # setup was told Slack succeeded whenever Discord did.
        headline = f"🔔 {kind} #{issue_number} — {title[:80]}"
        description = "\n".join(desc_parts)
        fields = [
            {"name": "Type", "value": kind, "inline": True},
            {"name": "Number", "value": f"#{issue_number}", "inline": True},
            {"name": "Repo", "value": repo, "inline": False},
        ]

        results: list[tuple[str, bool, str]] = []
        if discord_url:
            ok, msg = send_rich_discord(
                title=headline,
                description=description,
                color=color,
                fields=fields,
                url=url,
            )
            results.append(("Discord", ok, msg))
        if slack_url:
            ok, msg = send_rich_slack(
                title=headline,
                description=description,
                color=f"#{color:06X}",
                fields=fields,
                url=url,
            )
            results.append(("Slack", ok, msg))

        delivered = [c for c, ok, _ in results if ok]
        failed = [(c, m) for c, ok, m in results if not ok]

        lines = []
        if delivered:
            lines.append(
                f"## 🔔 Notification Sent\n\n"
                f"Delivered to: **{', '.join(delivered)}**\n\n"
                f"**{kind} #{issue_number}:** {title[:80]}"
            )
        else:
            lines.append("## ⚠️ Notification Failed\n")
        if failed:
            lines.append(
                "\n**Not delivered:**\n" + "\n".join(f"- {c}: `{m[:120]}`" for c, m in failed)
            )
        return "\n".join(lines)

    except Exception as exc:
        log.error(f"cmd_notify error: {exc}")
        return f"## ⚠️ Notify error: `{str(exc)[:200]}`"
