"""
app/handlers/ci.py
V4 Sprint 4: CI check_run event handler.

Triggers when a CI check completes.
On failure: posts an AI analysis comment with a suggested fix, and flags
recurring failures (same check failing 3+ times in 24h) as a pattern alert.
"""

import logging
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
    if not config.get("ci", "enabled", default=True):
        return

    output = check_run.get("output", {})
    title = output.get("title", "")
    summary = output.get("summary", "")[:2000]
    details = output.get("text", "")[:3000]

    pull_requests = check_run.get("pull_requests", [])
    if not pull_requests:
        log_ctx.info("No PR associated with check run — skipping comment")
        return

    pr_number = pull_requests[0]["number"]

    head_sha = check_run.get("head_sha", "") or check_run.get("check_suite", {}).get("head_sha", "")
    if _ci_already_alerted(repo, pr_number, head_sha):
        log_ctx.info(f"ci.duplicate_suppressed pr={pr_number} sha={head_sha[:7]}")
        return

    # CI logs are attacker-reachable: anything a contributor's test prints ends
    # up here. Delimit it so the model treats it as a log, not as instructions.
    failure_context = wrap_user_content(
        f"CI Check: {check_name}\nConclusion: {conclusion}\n"
        f"Title: {title}\nSummary: {summary}\nDetails: {details}",
        "CI_LOG",
    )

    try:
        from app.ai.guarded import guarded_ask, is_degraded

        r, _verdict = guarded_ask(
            "Senior DevOps engineer. Analyze CI failures concisely. JSON only.",
            f'Analyze this CI failure and suggest a fix:\n\n{failure_context}\n\nReturn JSON:\n{{\n  "root_cause": "one sentence — exact reason",\n  "category": "test_failure|build_error|lint_error|dependency|timeout|other",\n  "fix": "concrete steps to fix — 2-4 bullet points",\n  "is_flaky": false,\n  "confidence": 0.8\n}}',
            task="ci_analysis",
            response_type="ci",
        )

        # No usable analysis means no comment. A CI failure is already visible
        # in the checks UI; a bot comment that says nothing is pure noise.
        if is_degraded(r):
            log_ctx.warning(f"ci.analysis_degraded pr={pr_number} — no comment posted")
            return

        category = r.get("category", "other")
        cat_emoji = {
            "test_failure": "🧪",
            "build_error": "🏗️",
            "lint_error": "🔍",
            "dependency": "📦",
            "timeout": "⏱️",
            "other": "❌",
        }.get(category, "❌")

        flaky_note = ""
        if r.get("is_flaky"):
            flaky_note = "\n\n> 🎲 **Possibly flaky** — this might pass on re-run."

        fix_text = r.get("fix", "")
        if fix_text and not fix_text.startswith("-"):
            fix_text = "- " + fix_text.replace("\n", "\n- ")

        # Recurring-failure detection: if this same check has now failed 3 times
        # in 24h, surface a pattern alert so maintainers know it isn't a one-off.
        root_cause = r.get("root_cause", "See details below")
        pattern_note = ""
        if _track_failure_pattern(repo, check_name, root_cause):
            pattern_note = (
                f"\n\n> 🔁 **Recurring failure** — `{check_name}` has failed "
                "3+ times in the last 24h. This looks like a persistent issue, "
                "not a flake."
            )

        comment = f"## {cat_emoji} CI Failure — `{check_name}`\n\n**Root cause:** {root_cause}\n\n### Fix\n{fix_text}\n{flaky_note}{pattern_note}\n\n---\n*🤖 GitHub Autopilot — CI Analysis*{config.footer}"

        from app.github.sticky import MARKER_CI_REPORT, upsert_sticky

        upsert_sticky(repo, pr_number, token, MARKER_CI_REPORT, comment)
        log_ctx.done(f"CI failure comment upserted PR #{pr_number}")

    except Exception as e:
        log_ctx.error(f"CI handler failed: {e}")


_CI_ALERT_TTL = 21600  # 6h — covers a matrix plus a couple of re-runs
_CI_PATTERN_WINDOW = 86400
_CI_PATTERN_THRESHOLD = 3


def _ci_already_alerted(repo: str, pr_number: int, head_sha: str) -> bool:
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

        key = f"ci_alert:{repo}:{pr_number}:{head_sha}"
        return get_redis().set(key, "1", nx=True, ex=_CI_ALERT_TTL) is None
    except Exception as e:
        log.warning(f"ci.dedup_unavailable repo={repo} sha={head_sha[:7]}: {e} — suppressing")
        return True


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
