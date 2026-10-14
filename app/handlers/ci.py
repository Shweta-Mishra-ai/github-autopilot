"""
app/handlers/ci.py
V4 Sprint 4: CI check_run event handler.

Triggers when a CI check completes.
On failure: posts an AI analysis comment with a suggested fix, and flags
recurring failures (same check failing 3+ times in 24h) as a pattern alert.
"""

import logging
import re
from app.github.auth import get_installation_token
from app.core.config import load_config
from app.core.logger import EventLogger
from app.core.sanitizer import wrap_user_content

log = logging.getLogger(__name__)

SKIP_CONCLUSIONS = {"skipped", "neutral", "cancelled"}


def handle(payload: dict):
    action = payload.get("action")
    if action not in ("completed",):
        return

    check_run = payload.get("check_run", {})
    conclusion = check_run.get("conclusion", "")
    check_name = check_run.get("name", "")
    repo = payload["repository"]["full_name"]
    installation_id = payload.get("installation", {}).get("id")

    if not installation_id:
        return
    if conclusion in SKIP_CONCLUSIONS:
        return

    log_ctx = EventLogger("ci", repo=repo)

    if conclusion != "failure":
        return

    log_ctx.info(f"CI failure: {check_name}")

    try:
        token = get_installation_token(installation_id)
    except Exception as e:
        log_ctx.error(f"Auth failed: {e}")
        return

    config = load_config(repo, token)
    # Helper, not a raw get(): it also honours the bot.enabled kill switch.
    if not config.ci_enabled():
        return

    # GitHub sends these as explicit null (its own documented check_run
    # payload does), so `.get(key, "")` returned None and `None[:2000]` raised
    # outside any try: no CI analysis was ever produced for such a run.
    output = check_run.get("output") or {}
    title = output.get("title") or ""
    summary = (output.get("summary") or "")[:2000]
    details = (output.get("text") or "")[:3000]

    pull_requests = check_run.get("pull_requests", [])
    if not pull_requests:
        log_ctx.info("No PR associated with check run — skipping comment")
        return

    pr_number = pull_requests[0]["number"]

    head_sha = check_run.get("head_sha", "") or check_run.get("check_suite", {}).get("head_sha", "")
    if _ci_already_alerted(repo, pr_number, head_sha, check_name):
        log_ctx.info(
            f"ci.duplicate_suppressed pr={pr_number} sha={head_sha[:7]} check={check_name}"
        )
        return

    # The log itself. Actions leaves output.title/summary/text null for most
    # jobs, and the model was then asked for the "exact reason" of a failure
    # it could see nothing of — and answered one ("ImportError … in
    # tests/test_cfg.py") from the check's name alone.
    log_tail = _job_log_tail(repo, check_run, token)
    if not (summary.strip() or details.strip() or log_tail.strip()):
        log_ctx.info(
            f"ci.no_log pr={pr_number} check={check_name} — nothing to analyse, no comment"
        )
        _release_ci_alert(repo, pr_number, head_sha, check_name)
        return

    # CI logs are attacker-reachable: anything a contributor's test prints ends
    # up here. Delimit it so the model treats it as a log, not as instructions.
    failure_context = wrap_user_content(
        f"CI Check: {check_name}\nConclusion: {conclusion}\n"
        f"Title: {title}\nSummary: {summary}\nDetails: {details}\n"
        f"Log (last lines):\n{log_tail}",
        "CI_LOG",
    )

    try:
        from app.ai.guarded import guarded_ask, is_degraded

        r, _verdict = guarded_ask(
            "Senior DevOps engineer. Analyze CI failures concisely. JSON only.",
            f"Analyze this CI failure and suggest a fix. Name a cause only if the log "
            f"shows it; otherwise say the log does not show the cause.\n\n{failure_context}\n\n"
            f'Return JSON:\n{{\n  "root_cause": "one sentence, quoting the failing line from the log",\n'
            f'  "evidence": "the exact log line that shows it, copied verbatim",\n'
            f'  "category": "test_failure|build_error|lint_error|dependency|timeout|other",\n'
            f'  "fix": "concrete steps to fix — 2-4 bullet points"\n}}',
            task="ci_analysis",
            response_type="ci",
        )

        # No usable analysis means no comment. A CI failure is already visible
        # in the checks UI; a bot comment that says nothing is pure noise.
        if is_degraded(r):
            log_ctx.warning(f"ci.analysis_degraded pr={pr_number} — no comment posted")
            _release_ci_alert(repo, pr_number, head_sha, check_name)
            return

        category = str(r.get("category") or "other")
        cat_emoji = {
            "test_failure": "🧪",
            "build_error": "🏗️",
            "lint_error": "🔍",
            "dependency": "📦",
            "timeout": "⏱️",
            "other": "❌",
        }.get(category, "❌")

        # The cause is shown as established only when the line it rests on
        # really is in the log. A model's "is_flaky" guess is no longer shown
        # at all: nothing in one failed run can show flakiness.
        evidence = str(r.get("evidence") or "").strip().strip("`")
        corpus = "\n".join((summary, details, log_tail))
        grounded = len(evidence) >= 8 and evidence in corpus
        root_cause = str(r.get("root_cause") or "").strip()
        if grounded:
            cause_md = f"**Likely cause:** {root_cause}\n\n```\n{evidence[:300]}\n```"
        else:
            cause_md = (
                f"**Possible cause (not confirmed by the log):** {root_cause or 'not determined'}"
            )

        # "2-4 bullet points" is naturally answered as a JSON list; that raised
        # AttributeError on .startswith and lost the analysis.
        fix = r.get("fix") or ""
        if isinstance(fix, list):
            fix_text = "\n".join(f"- {str(x).lstrip('- ').strip()}" for x in fix if str(x).strip())
        else:
            fix_text = str(fix)
            if fix_text and not fix_text.startswith("-"):
                fix_text = "- " + fix_text.replace("\n", "\n- ")

        # Recurring-failure detection: if this same check has now failed 3 times
        # in 24h, surface a pattern alert so maintainers know it isn't a one-off.
        pattern_note = ""
        if _track_failure_pattern(repo, check_name, root_cause):
            pattern_note = (
                f"\n\n> 🔁 **Recurring failure** — `{check_name}` has failed "
                "3+ times in the last 24h. This looks like a persistent issue, "
                "not a flake."
            )

        section = (
            f"### {cat_emoji} `{check_name}`\n\n{cause_md}\n\n#### Fix\n{fix_text}{pattern_note}"
        )

        from app.github.sticky import MARKER_CI_REPORT, upsert_sticky

        body = _merge_ci_report(
            _current_ci_report(repo, pr_number, token), head_sha, check_name, section
        )
        upsert_sticky(
            repo,
            pr_number,
            token,
            MARKER_CI_REPORT,
            f"{body}\n\n---\n*🤖 GitHub Autopilot — CI Analysis*{config.footer}",
        )
        log_ctx.done(f"CI failure comment upserted PR #{pr_number}")

    except Exception as e:
        log_ctx.error(f"CI handler failed: {e}")
        # The dedup key was taken before the analysis. Released, so a retry
        # or re-run of this check can still produce its section.
        _release_ci_alert(repo, pr_number, head_sha, check_name)


_LOG_CHARS = 4000


def _job_log_tail(repo: str, check_run: dict, token: str) -> str:
    """The end of a GitHub Actions job's log, or "" when it cannot be read
    (another CI app, no permission, expired log)."""
    if ((check_run.get("app") or {}).get("slug")) != "github-actions" or not check_run.get("id"):
        return ""
    try:
        from app.github.client import gh_get_text

        text = gh_get_text(f"/repos/{repo}/actions/jobs/{int(check_run['id'])}/logs", token)
        return text[-_LOG_CHARS:]
    except Exception as e:
        log.info(f"ci.log_unavailable repo={repo} job={check_run.get('id')}: {e}")
        return ""


_SHA_MARK = "<!-- ci-sha:{} -->"
_SECTION = re.compile(r"<!-- ci-check:(.*?) -->\n(.*?)\n<!-- /ci-check -->", re.S)


def _current_ci_report(repo: str, pr_number: int, token: str) -> str:
    """The body of this bot's CI sticky, or "" when there is none."""
    try:
        from app.github.client import gh_get
        from app.github.sticky import MARKER_CI_REPORT, find_sticky

        cid = find_sticky(repo, pr_number, token, MARKER_CI_REPORT)
        if cid is None:
            return ""
        return str((gh_get(f"/repos/{repo}/issues/comments/{cid}", token) or {}).get("body") or "")
    except Exception:
        return ""


def _merge_ci_report(existing: str, head_sha: str, check_name: str, section: str) -> str:
    """
    One section per failing check on the current commit.

    The alert was deduplicated per commit, so when tests failed after lint on
    the same commit, the tests analysis was silenced and the comment named
    only the lint check. Sections from an older commit are dropped: they
    describe code that is no longer the PR's head.
    """
    sections: dict[str, str] = {}
    if _SHA_MARK.format(head_sha) in existing:
        sections = dict(_SECTION.findall(existing))
    sections[check_name] = section
    parts = [
        f"<!-- ci-check:{name} -->\n{body}\n<!-- /ci-check -->" for name, body in sections.items()
    ]
    return (
        f"## CI Failures — commit `{head_sha[:7]}`\n{_SHA_MARK.format(head_sha)}\n\n"
        + "\n\n".join(parts)
    )


_CI_ALERT_TTL = 21600  # 6h — covers a matrix plus a couple of re-runs
_CI_PATTERN_WINDOW = 86400
_CI_PATTERN_THRESHOLD = 3


def _ci_already_alerted(repo: str, pr_number: int, head_sha: str, check_name: str = "") -> bool:
    """
    True when this commit already produced a CI comment.

    A 5-job matrix failing on one commit fires five check_run events, and each
    one used to run its own AI analysis and post its own comment. Keyed on the
    SHA, the first wins and the rest are silent.

    Fails closed, like every other dedup helper: a missed comment beats five
    duplicates, and the failure is visible in the checks UI regardless.
    """
    try:
        from app.core.redis_client import get_redis

        key = f"ci_alert:{repo}:{pr_number}:{head_sha}:{check_name}"
        return get_redis().set(key, "1", nx=True, ex=_CI_ALERT_TTL) is None
    except Exception as e:
        log.warning(f"ci.dedup_unavailable repo={repo} sha={head_sha[:7]}: {e} — suppressing")
        return True


def _release_ci_alert(repo: str, pr_number: int, head_sha: str, check_name: str = "") -> None:
    """Undo _ci_already_alerted for a commit whose analysis produced nothing."""
    try:
        from app.core.redis_client import get_redis

        get_redis().delete(f"ci_alert:{repo}:{pr_number}:{head_sha}:{check_name}")
    except Exception as e:
        log.debug(f"ci.release_failed repo={repo}: {e}")


def _track_failure_pattern(repo: str, check_name: str, root_cause: str):
    """
    True the first time this check crosses the failure threshold in the window.

    Two fixes over the Sprint 5 version:
      - `== 3` fired exactly once and never again if the counter skipped a
        value (concurrent events increment it more than one at a time). Now
        `>= threshold` with an NX flag so it fires once per window regardless.
      - expire() ran on EVERY increment, which reset the TTL continuously, so
        a check failing steadily kept the window alive forever. It now starts
        on the first failure only and is allowed to roll.
    """
    try:
        from app.core.redis_client import get_redis

        r = get_redis()
        key = f"ci_fail:{repo}:{check_name}"
        count = r.incr(key)
        if int(count) == 1:
            r.expire(key, _CI_PATTERN_WINDOW)

        # NX flag: fires once per window, not on every failure past the threshold.
        if (
            int(count) >= _CI_PATTERN_THRESHOLD
            and r.set(f"{key}:alerted", "1", nx=True, ex=_CI_PATTERN_WINDOW) is not None
        ):
            log.warning(
                f"ci.pattern_detected repo={repo} check={check_name} "
                f"count={count} root_cause={root_cause[:60]}"
            )
            return True  # Caller posts pattern alert
    except Exception as e:
        log.debug(f"ci.track_failure_pattern_failed repo={repo} check={check_name}: {e}")
    return False


def _get_failure_count(repo: str, check_name: str) -> int:
    """Returns how many times this check has failed today."""
    try:
        from app.core.redis_client import get_redis

        r = get_redis()
        key = f"ci_fail:{repo}:{check_name}"
        val = r.get(key)
        return int(val) if val else 0
    except Exception:
        return 0
