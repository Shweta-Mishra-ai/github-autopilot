"""app/handlers/comments/service.py — Main orchestration layer for comment handling."""

from __future__ import annotations

import logging

from app.core.authorization import check_command_permission
from app.core.config import load_config
from app.core.logger import EventLogger
from app.github.auth import get_installation_token
from app.github.client import GitHubError
from app.handlers.comments import gh_post
from app.github.helpers import fmt_error

from .constants import SKIP_AUTHORS
from .dispatcher import (
    augment_with_memory,
    check_user_rate_limit,
    extract_command,
    is_providers_down,
    make_degraded_response,
)

log = logging.getLogger(__name__)


def handle_comment_event(payload: dict) -> None:
    """
    Main webhook handler for issue_comment events.

    Called from _run_handler() in server.py inside the thread pool.
    Never raises — all exceptions are caught and logged.
    """
    # ── Payload extraction ────────────────────────────────────────────────
    action = payload.get("action", "")
    if action not in ("created", "edited"):
        return

    comment = payload.get("comment") or {}
    issue = payload.get("issue") or {}
    repo_data = payload.get("repository") or {}
    installation = payload.get("installation") or {}
    sender = payload.get("sender") or {}

    body = comment.get("body", "")
    author = sender.get("login", "")
    repo = repo_data.get("full_name", "")
    issue_number = issue.get("number", 0)
    installation_id = installation.get("id", 0)

    if not all([body, author, repo, issue_number, installation_id]):
        log.warning(f"handle_comment_event: missing required fields repo={repo}")
        return

    # ── Bot-loop prevention ────────────────────────────────────────────────
    if author in SKIP_AUTHORS or author.endswith("[bot]"):
        return

    # ── Command detection ─────────────────────────────────────────────────
    cmd = extract_command(body)
    if not cmd:
        return

    log_ctx = EventLogger("comments", repo=repo, issue=issue_number, cmd=cmd, author=author)
    log_ctx.info("command_received")

    # ── Auth ──────────────────────────────────────────────────────────────
    try:
        token = get_installation_token(installation_id)
    except Exception as exc:
        log_ctx.error("auth_failed", reason=str(exc)[:100])
        return

    config = load_config(repo, token)

    # ── Per-user rate limit ───────────────────────────────────────────────
    if not check_user_rate_limit(repo, author):
        _post_comment(
            repo,
            issue_number,
            token,
            (
                f"## ⏳ Rate Limit Reached\n\n"
                f"@{author} — you've used the command limit (10/hour) for this repo.\n\n"
                "Please try again in an hour."
            ),
            log_ctx,
        )
        return

    # ── Authorization ─────────────────────────────────────────────────────
    allowed, reason = check_command_permission(cmd, repo, author, token, config)
    if not allowed:
        _post_comment(
            repo,
            issue_number,
            token,
            (
                f"## 🚫 Permission Denied\n\n"
                f"@{author} — `{cmd}` requires maintainer access.\n\n"
                f"**Reason:** {reason}"
            ),
            log_ctx,
        )
        return

    idx = body.lower().find(cmd)  # slice ORIGINAL body so args keep their case
    cmd_args = body[idx + len(cmd) :].strip() if idx != -1 else ""

    # ── Context building ──────────────────────────────────────────────────
    context = f"Title: {issue.get('title', '')}\nBody: {(issue.get('body') or '')[:1500]}"

    # ── Repository memory ("the brain") ────────────────────────────────────
    # No-op in default cloud mode (privacy guard); enriches context on local models.
    context = augment_with_memory(context, repo, f"{issue.get('title', '')} {cmd_args}".strip())

    # ── Dispatch ──────────────────────────────────────────────────────────
    # Reset per-thread model record — reused pool threads must not disclose stale models.
    from app.ai.router import last_model_disclosure, reset_last_call

    reset_last_call()
    response = _dispatch(
        cmd=cmd,
        cmd_args=cmd_args,
        context=context,
        repo=repo,
        issue_number=issue_number,
        issue=issue,
        token=token,
        author=author,
        config=config,
        log_ctx=log_ctx,
    )

    if not response:
        log_ctx.warning("empty_response")
        return

    # ── Check for providers-down sentinel ─────────────────────────────────
    if isinstance(response, dict) and is_providers_down(response):
        response = make_degraded_response(response)

    # ── Post to GitHub (with model disclosure) ─────────────────────────────
    footer = getattr(config, "footer", "")
    full = (
        f"{response}\n\n---\n*🤖 `{cmd}` — requested by @{author}{last_model_disclosure()}*{footer}"
    )
    _post_comment(repo, issue_number, token, full, log_ctx)


# ── Internal helpers ──────────────────────────────────────────────────────────


def _post_comment(
    repo: str,
    issue_number: int,
    token: str,
    body: str,
    log_ctx: EventLogger,
) -> None:
    """Post a comment to GitHub. Logs on failure, never raises."""
    try:
        gh_post(f"/repos/{repo}/issues/{issue_number}/comments", token, {"body": body})
        log_ctx.done("comment_posted")
    except GitHubError as exc:
        log_ctx.error("post_failed", reason=str(exc)[:100])
    except Exception as exc:
        log_ctx.error("post_unexpected", reason=str(exc)[:100])


def _dispatch(
    cmd: str,
    cmd_args: str,
    context: str,
    repo: str,
    issue_number: int,
    issue: dict,
    token: str,
    author: str,
    config,
    log_ctx: EventLogger,
) -> str | dict | None:
    """
    Route a command to the appropriate handler module.

    Returns a markdown string, a providers-down sentinel dict, or None.
    Never raises.
    """
    from . import generator as G
    from . import reviewer as R
    from . import publisher as P

    try:
        ctx_title = issue.get("title", "")

        match cmd:
            # ── Generator: AI content ──────────────────────────────────
            case "/fix":
                return G.cmd_fix(ctx_title, context, repo)
            case "/explain":
                return G.cmd_explain(context)
            case "/improve":
                return G.cmd_improve(context)
            case "/test":
                return G.cmd_test(context)
            case "/docs":
                return G.cmd_docs(context)
            case "/refactor":
                return G.cmd_refactor(context)
            case "/gaps":
                return G.cmd_gaps(context)
            case "/perf":
                return G.cmd_perf(context)
            case "/arch":
                return G.cmd_arch(repo, issue_number, issue, token)

            # ── Reviewer: read-only analysis ───────────────────────────
            case "/health":
                return R.cmd_health(repo, token)
            case "/version":
                return R.cmd_version(repo, token)
            case "/summarize":
                return R.cmd_summarize(repo, issue_number, token)
            case "/ci":
                return R.cmd_ci(cmd_args, repo=repo, token=token)
            case "/budget":
                return R.cmd_budget()
            case "/report":
                return R.cmd_report(repo)
            case "/impact":
                return R.cmd_impact(repo, issue_number, issue, token)
            case "/changelog":
                return R.cmd_changelog(repo, token)

            # ── Publisher: GitHub writes ───────────────────────────────
            case "/merge":
                return P.cmd_merge(repo, issue_number, issue, token, author, config)
            case "/apply":
                return P.cmd_apply(repo, issue_number, token, cmd_args)
            case "/rollback":
                return P.cmd_rollback(repo, issue_number, token, cmd_args, author)
            case "/release":
                return P.cmd_release(repo, token, author)
            case "/runtests":
                return P.cmd_runtests(repo, token)
            case "/notify":
                return P.cmd_notify(repo, issue_number, issue, token, cmd_args)
            case "/security":
                return P.cmd_security(repo, issue_number, issue, token)
            case "/secfull":
                return P.cmd_secfull(repo, token)

            # ── Shared handlers ────────────────────────────────────────
            case "/autofix":
                from app.handlers.autofix import run_autofix

                return run_autofix(repo, issue_number, issue, token, cmd_args.strip())

            case _:
                log_ctx.warning("unknown_command")
                return None

    except Exception as exc:
        log_ctx.error("dispatch_error", cmd=cmd, reason=str(exc)[:120])
        return fmt_error(f"Command `{cmd}` failed", exc)
