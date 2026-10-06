"""
Pull Request Handler — app/handlers/pull_request/

V7: every sub-analysis RETURNS markdown; handle() assembles one report and
    upserts it into a single sticky comment. Before V7 each sub-analysis
    posted its own comment — four on open, two more on every push, none of
    them ever updated — which is what made the bot exhausting to work with.

V3: PR analysis + AI code review + embedding-based context
    + AI PR Summary + Test gap detection

Package layout (was one 760-line module; split along the same seams as
handlers/comments/):

    classify.py  file kind, review ordering, blast radius — pure, no I/O
    analysis.py  risk/title analysis + reviewer summary
    review.py    AI code review + Reviews-API posting
    gaps.py      test-coverage gap detection
    report.py    assembly of the sticky comment body

handle() stays here and calls the sub-analyses as module-level names, so this
module remains the single place that decides what runs, in what order, and
whether anything is posted at all.
"""

from __future__ import annotations

from app.github.auth import get_installation_token
from app.github.client import gh_get, gh_post, gh_put, GitHubError
from app.github.notifications import notify_high_risk_pr, notify_pr_opened
from app.github.sticky import MARKER_PR_REPORT, update_sticky_if_present, upsert_sticky
from app.ai.router import router
from app.ai.validator import is_unusable, validate_pr_analysis, validate_code_review
from app.core.config import load_config
from app.core.logger import EventLogger
from app.core.confidence import ConfidenceGate
from app.core.guardrails import check_pr_title_update
from app.core.sanitizer import InjectionRejected, wrap_user_content

from .analysis import _analyze_pr, _build_pr_summary
from .classify import (
    _blast_radius,
    _file_review_priority,
    _is_generated,
    _is_test_file,
    _review_sort_key,
)
from .gaps import _detect_test_gaps
from .report import _build_pr_report
from .review import _post_inline_review, _review_code

# Re-exported so `from app.handlers.pull_request import X` keeps working for
# every existing caller: handlers/comments/reviewer.py (_blast_radius),
# evals/run.py (_review_code), and the test suite.
__all__ = [
    "handle",
    "SKIP_AUTHORS",
    "_analyze_pr",
    "_blast_radius",
    "_build_pr_report",
    "_build_pr_summary",
    "_detect_test_gaps",
    "_file_review_priority",
    "_is_generated",
    "_is_test_file",
    "_post_inline_review",
    "_review_code",
    "_review_sort_key",
]

# Names imported above that handle() itself does not call are kept because
# tests and callers patch them at this path; referencing them here also stops
# linters from stripping the imports.
_REEXPORTED = (
    gh_post,
    is_unusable,
    validate_code_review,
    validate_pr_analysis,
    check_pr_title_update,
    notify_high_risk_pr,
    wrap_user_content,
    router,
    gh_put,
)

SKIP_AUTHORS = {
    "dependabot[bot]",
    "renovate[bot]",
    "github-actions[bot]",
    "ai-repo-manager[bot]",
    "github-autopilot[bot]",
}


def handle(payload: dict):
    action = payload.get("action")
    if action not in ("opened", "reopened", "synchronize"):
        return

    pr = payload["pull_request"]
    repo = payload["repository"]["full_name"]
    installation_id = payload["installation"]["id"]
    # `user` is an explicit null when the account was deleted. This line runs
    # BEFORE the EventLogger exists, so the TypeError was caught by the blanket
    # handler in server._run_handler and the event vanished with a log line
    # that named no cause. `repository` and `installation` are left as bare
    # subscripts on purpose: GitHub guarantees them for this event type, and a
    # payload missing one is malformed rather than merely unusual.
    author = (pr.get("user") or {}).get("login", "")
    pr_number = pr["number"]

    log = EventLogger("pull_request", repo=repo, pr=pr_number)

    if author in SKIP_AUTHORS or author.endswith("[bot]"):
        return

    try:
        token = get_installation_token(installation_id)
    except Exception as e:
        log.error(f"Auth failed: {e}")
        return

    config = load_config(repo, token)
    gate = ConfidenceGate(config)

    if not config.pr_enabled():
        return

    # Per-repo daily AI budget — see issues.py for why this exists. A PR event
    # can trigger several LLM calls, so it is metered like any other.
    from app.core.guardrails import check_repo_rate_limit, increment_repo_usage

    budget = check_repo_rate_limit(repo)
    if not budget.passed:
        log.warning(f"pr.rate_limited repo={repo}: {budget.reason}")
        return
    increment_repo_usage(repo)

    # Archived repositories are read-only by intent. check_archived_repo()
    # existed with zero callers, so the bot commented, labelled and reviewed
    # on them regardless.
    try:
        from app.core.guardrails import check_archived_repo

        repo_meta = gh_get(f"/repos/{repo}", token)
        archived = check_archived_repo(repo_meta)
        if not archived.passed:
            log.info(f"skip_archived repo={repo}: {archived.reason}")
            return
    except Exception as e:
        log.debug(f"archived_check_skipped repo={repo}: {e}")

    try:
        # Every page. A single fetch returned GitHub's default of 30 files, so
        # on a larger PR the rest were never reviewed or counted.
        from app.github.helpers import pr_files

        files = pr_files(repo, pr_number, token, get=gh_get)
    except Exception:
        files = []

    context = ""
    try:
        from app.intelligence.retrieval import get_context_for_pr

        context = get_context_for_pr(repo, files)
        if context:
            log.info("intelligence.context_retrieved")
    except Exception as e:
        log.debug(f"Context retrieval skipped: {e}")

    # Local triage gate. Inert unless OLLAMA_HOST is configured, and it can
    # only ever SKIP work: every error path answers "review it", which is what
    # happens with no gate at all. Placed after the files fetch (it needs the
    # diff) and before any cloud call, which is the whole point — the calls it
    # prevents are the expensive ones.
    try:
        from app.ai.gatekeeper import is_substantive

        substantive, reason = is_substantive(files, pr.get("title", ""))
        if not substantive:
            log.info(f"pr.skipped_by_local_triage reason={reason}")
            return
    except Exception as e:
        log.debug(f"gatekeeper_skipped: {e}")

    analysis_md = summary_md = review_md = gaps_md = ""
    inline_comments: list = []

    if action == "opened":
        try:
            notify_pr_opened(
                repo=repo,
                pr_number=pr_number,
                title=pr.get("title", ""),
                risk="unknown",
            )
        except Exception as e:
            log.debug(f"notify_pr_opened skipped: {e}")

    # Analysis and summary run on EVERY head, not only on open. Run only on
    # open, they had two consequences: each push rewrote the sticky without
    # its Analysis and Summary sections, and the risk level auto-merge reads
    # (keyed by head SHA) was never recorded for any head after the first —
    # so /merge refused every PR that had been pushed to, telling the user to
    # re-open it, which did not run the analysis either. Rewriting the title
    # and description, and the high-risk alert, stay first-look only.
    #
    # A section whose input the sanitiser rejects outright is reported, not
    # dropped. The rejection used to escape handle() entirely, so a PR whose
    # body or diff carried a critical injection pattern got no report at all
    # — the one PR that most needs a human to look was the one the bot went
    # quiet on.
    rejected: list[str] = []

    def _guarded(section, fn, default):
        try:
            return fn()
        except InjectionRejected as e:
            log.warning(f"pr.{section}_input_rejected: {e}")
            rejected.append(section)
            return default

    analysis_md = _guarded(
        "analysis",
        lambda: _analyze_pr(
            pr,
            repo,
            pr_number,
            files,
            token,
            config,
            gate,
            context,
            log,
            apply_metadata=(action == "opened"),
        ),
        "",
    )
    summary_md = _guarded(
        "summary",
        lambda: _build_pr_summary(pr, repo, pr_number, files, token, config, log),
        "",
    )

    if config.get("pull_requests", "code_review", default=True):
        review_md, inline_comments = _guarded(
            "code review",
            lambda: _review_code(pr, repo, pr_number, files, token, config, gate, context, log),
            ("", []),
        )

    if config.get("pull_requests", "detect_test_gaps", default=True):
        gaps_md = _guarded(
            "test gaps",
            lambda: _detect_test_gaps(pr, repo, pr_number, files, token, config, log),
            "",
        )

    if rejected:
        notice = (
            "> ⚠️ **Automated "
            + ", ".join(rejected)
            + " refused.** This PR's title, description or diff contains text "
            "matching a prompt-injection pattern, so it was not sent to the model. "
            "Review it manually."
        )
        review_md = f"{notice}\n\n{review_md}".strip()

    if not any([analysis_md, summary_md, review_md, gaps_md]):
        log.info("pr.nothing_to_report — staying silent")
        return

    # A push with nothing to flag never CREATES a comment — "still fine" is not
    # worth a notification to every subscriber. But it must still EDIT the
    # existing report (an edit notifies nobody): this used to return here, so
    # a push that fixed every finding left the report listing them as open.
    edit_only = action == "synchronize" and not (review_md or gaps_md)

    # Line-anchored findings still go through the Reviews API: they land on
    # the diff itself, which is the one place bot output is unambiguously
    # useful. Only the conversation-tab noise is being consolidated.
    if inline_comments:
        # The return value is the recovery path, not a status code. Findings
        # that anchor to a diff line are left out of review_md on the
        # assumption they will appear on the diff; if GitHub refuses the
        # review, this is the only remaining copy of them.
        unposted_md = _post_inline_review(pr, repo, pr_number, token, config, inline_comments, log)
        if unposted_md:
            review_md = f"{review_md}\n\n{unposted_md}".strip()

    body = _build_pr_report(analysis_md, summary_md, review_md, gaps_md, pr, files)
    try:
        if edit_only:
            if update_sticky_if_present(
                repo, pr_number, token, MARKER_PR_REPORT, body + config.footer
            ):
                log.done("pr_report_refreshed")
            else:
                log.info("pr.nothing_to_flag — no report to refresh, staying silent")
            return
        upsert_sticky(repo, pr_number, token, MARKER_PR_REPORT, body + config.footer)
        log.done("pr_report_upserted")
    except GitHubError as e:
        log.error(f"Failed to upsert PR report: {e}")
