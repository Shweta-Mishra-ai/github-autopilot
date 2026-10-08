"""
app/handlers/push.py
V5: All-branch secret scanning + configurable full-scan branches.

FIXED (Sprint 2): Duplicate issues — Redis dedup (24h dep scan, 6h commit lint).
NEW (Sprint 2): Only HIGH/CRITICAL vulnerabilities create GitHub issues.
     LOW/MODERATE = logged only (no spam).

FIXED (V5): Secret scan now runs on ALL branches, not just main/master.
     Secrets pushed to feature branches are the most common vector.

FIXED (Sprint 8): _scan_secrets was missing dedup entirely.
     _already_reported() existed and was used for dep scan + commit lint but
     was never called inside _scan_secrets. Result: every push containing
     the same secret created a duplicate security issue.

     Fix: Deduplicate per FINDING (file, pattern, redacted value; 1h TTL),
     so the same secret pushed again inside the hour is reported once, and a
     different secret is always reported — appended to the open alert issue
     (_open_secret_issue) rather than opening another.

     The dedup key used to be per repo ("push_reported:{repo}:secret_scan"),
     which contradicted all of the above: after one alert, every other secret
     pushed to that repo in the next hour was dropped with an info log and
     never re-scanned. Found by the 2026-10-08 audit.
"""

import base64
import hashlib
import logging
import re

from app.github.auth import get_installation_token
from app.github.client import gh_get, gh_post, GitHubError
from app.github.notifications import notify_secret_detected
from app.core.config import load_config
from app.core.logger import EventLogger
from app.security.enhanced_secrets import (
    format_findings as format_secret_findings,
    is_example_path,
    scan_diff,
)
from app.security.dependencies import (
    scan_requirements_txt,
    get_actionable_findings,
    format_dep_findings,
)

_log = logging.getLogger(__name__)

CONVENTIONAL_TYPES = {
    "feat",
    "fix",
    "docs",
    "refactor",
    "test",
    "chore",
    "perf",
    "ci",
    "style",
    "build",
}
SKIP_AUTHORS = {
    "dependabot[bot]",
    "renovate[bot]",
    "github-actions[bot]",
    "ai-repo-manager[bot]",
    "github-autopilot[bot]",
}

# Sprint 8: TTL for secret-finding dedup (seconds).
# 1 h = short enough to re-alert on persistent leaks, long enough to absorb
# rapid successive pushes of the same commit.
_SECRET_DEDUP_TTL = 3600


# The repo config file, by the name load_config() actually reads.
CONFIG_FILENAME = ".ai-repo-manager.yml"


def _config_file_touched(commits: list) -> bool:
    """
    True when any commit in this push added, modified or removed the config.

    `removed` counts: deleting the file means the repo falls back to defaults,
    which changes behaviour exactly as much as editing it does.
    """
    for commit in commits or []:
        if not isinstance(commit, dict):
            continue
        for field in ("added", "modified", "removed"):
            paths = commit.get(field) or []
            if any(str(path).endswith(CONFIG_FILENAME) for path in paths):
                return True
    return False


def handle(payload: dict) -> None:
    repo = payload["repository"]["full_name"]
    installation_id = payload["installation"]["id"]
    pusher = payload.get("pusher", {}).get("name", "")
    commits = payload.get("commits", [])
    ref = payload.get("ref", "")
    # The repository's real default branch. This was `ref in (main, master)`,
    # so a repo whose default is `develop` or `trunk` never got the
    # default-branch scans, and a stray `master` in a `main` repo did.
    default_branch = payload["repository"].get("default_branch") or "main"

    log = EventLogger("push", repo=repo)

    if pusher in SKIP_AUTHORS or pusher.endswith("[bot]"):
        return
    if not commits:
        return
    if not ref.startswith("refs/heads/"):
        return  # Skip tag pushes

    try:
        token = get_installation_token(installation_id)
    except Exception as e:
        log.error(f"Auth failed: {e}")
        return

    # A push that edits the config file must not be read through a cache that
    # predates it. Both caches are dropped for this repo, not just the config
    # one: `commands.permissions.maintainer_only` lives in that file, so a
    # permission decision made from the old config is stale for the same
    # reason. Without this, a maintainer fixing their config waited up to five
    # minutes to find out whether the fix worked — long enough to conclude it
    # had not and change something else.
    #
    # This is also what the two invalidate_* helpers were written for. They had
    # no callers, which is why the TTL was the only thing ever expiring an
    # entry, and why neither cache reclaimed memory.
    if _config_file_touched(commits):
        from app.core.authorization import invalidate_permission_cache
        from app.core.config import invalidate_config_cache

        invalidate_config_cache(repo)
        invalidate_permission_cache(repo)
        log.info("push.config_changed — config and permission caches dropped")

    config = load_config(repo, token)
    latest_sha = commits[-1].get("id", "") if commits else ""
    branch = ref[len("refs/heads/") :]

    # Helper, not a raw get(): it also honours the bot.enabled kill switch.
    if not config.push_enabled():
        return

    # Secret scan runs on ALL branches (secrets are dangerous everywhere).
    # Dependency + commit lint only run on default branch by default,
    # but can be extended via config push.scan_all_branches = true.
    is_default_branch = branch == default_branch
    scan_all = config.get("push", "scan_all_branches", default=False)
    run_full_scan = is_default_branch or scan_all

    # Secret scan: ALL branches — secrets don't care which branch they're on
    if config.get("push", "scan_secrets", default=True):
        _scan_secrets(repo, commits, token, config, log)

    # Dep scan + commit lint: default branch only (or all if scan_all_branches=true)
    if run_full_scan:
        if config.get("push", "scan_dependencies", default=True):
            _scan_dependencies(repo, commits, token, config, log, ref=latest_sha)

        if config.get("push", "enforce_conventional_commits", default=True):
            _lint_commits(repo, commits, token, config, log, branch=branch)

        # Writes the replacement message rather than only naming the problem.
        # Runs on the same branches as the lint it complements, and is its own
        # config switch: an operator who wants the report without a bot
        # commenting on their commits can have exactly that.
        if config.get("push", "suggest_commit_messages", default=True):
            from app.handlers.commit_message import suggest_commit_messages

            suggest_commit_messages(repo, commits, token, config, log)

        # Refresh the README blocks that restate what the code already knows
        # (module counts, the command registry, the import graph) and open a PR
        # if any drifted. No-ops unless README_SELF_UPDATE_REPO names this repo
        # — the renderers read the local source tree, so pointing them at an
        # arbitrary repository would describe the bot rather than that repo.
        if config.get("push", "update_readme", default=True):
            from app.handlers.readme import maybe_update_readme

            maybe_update_readme(repo, commits, token, config, log)


# ── Dedup ──────────────────────────────────────────────────────────────────────


def _already_reported(repo: str, report_type: str, ttl_seconds: int = 86400) -> bool:
    """
    True when this report was already filed inside the window.

    FAILS CLOSED. The old implementation returned False on any Redis error —
    meaning "not reported yet, go ahead and file it" — so a Redis blip produced
    a burst of duplicate issues. A missed alert during an outage is strictly
    better than seven duplicates; the suppression is logged and metered so an
    operator can see it happening.
    """
    try:
        from app.core.redis_client import get_redis

        r = get_redis()
        key = f"push_reported:{repo}:{report_type}"
        return r.set(key, "1", nx=True, ex=ttl_seconds) is None
    except Exception as e:
        from app.core.metrics import metrics

        metrics.increment("dedup.redis_unavailable")
        _log.warning(
            f"push.dedup_unavailable repo={repo} type={report_type}: {e} — suppressing report"
        )
        return True


# ── Secret scan ────────────────────────────────────────────────────────────────


def _skip_secret_scan(filename: str) -> bool:
    """
    True for a file whose purpose is to hold stand-in values (a test, a
    fixture, an `.example`), as the scanner itself defines them.

    This had its own list, matched as substrings: "test/" skipped
    `app/contest/config.py` and `infra/protest/keys.env`, "docs/" skipped
    `src/mydocs/creds.py`, and `docs/` and `examples/` were skipped outright —
    overriding the scanner, which deliberately still scans documentation with
    its high-specificity patterns, because a real token pasted into a README
    is one of the commonest ways a credential leaks.
    """
    return is_example_path(filename)


# Only these open a GitHub issue. medium/low are logged — the same policy the
# dependency scanner has always applied. This is the single biggest lever on
# secret-alert noise: the entropy heuristic fires on hashes, UUIDs and lockfile
# digests, and those land in the medium bucket.
_ACTIONABLE_SECRET_SEVERITIES = {"critical", "high"}

# How long one open alert issue is reused before a new one is opened.
_SECRET_ALERT_TTL = 86400


def _actionable_secrets(findings: list) -> list:
    """Findings severe enough to be worth interrupting a maintainer for."""
    return [f for f in findings if getattr(f, "severity", "") in _ACTIONABLE_SECRET_SEVERITIES]


def _scan_secrets(repo, commits, token, config, log) -> None:
    """
    Scan all added/modified file patches in `commits` for secrets.

    Uses enhanced_secrets, which the codebase already documents as a drop-in
    replacement with false-positive reduction. push.py — the only path that
    files GitHub issues — was still importing the legacy scanner, so the
    quieter one was reachable only via /security and MCP.
    """
    all_findings = []
    for commit in commits:
        sha = commit.get("id", "")
        if not sha:
            continue
        try:
            diff_data = gh_get(f"/repos/{repo}/commits/{sha}", token)
            for f in diff_data.get("files", []):
                filename = f.get("filename", "")
                if _skip_secret_scan(filename):
                    continue  # test/example/docs — dummy secrets expected, skip
                patch = f.get("patch", "")
                if patch:
                    # Passing file_path engages the scanner's own per-path
                    # false-positive suppression.
                    all_findings.extend(scan_diff(patch, file_path=filename))
        except Exception as e:
            log.error(f"Secret scan failed for {sha[:7]}: {e}")

    if not all_findings:
        return

    actionable = _actionable_secrets(all_findings)
    if not actionable:
        log.info(
            f"push.secret_scan_ok repo={repo} low_severity={len(all_findings)} — no issue created"
        )
        return

    fresh = _unreported_secrets(repo, actionable)
    if not fresh:
        log.info(f"push.secret_scan_dedup repo={repo} findings={len(actionable)}")
        return

    try:
        _open_secret_issue(repo, token, fresh, log, config)
    except Exception as e:
        log.error(f"Failed to post secret alert: {e}")


def _secret_fingerprint(finding) -> str:
    """One secret in one file. Built from the redacted value: the raw value
    is never stored, not even hashed."""
    raw = f"{finding.file_path}|{finding.pattern_name}|{finding.redacted_match}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _unreported_secrets(repo: str, findings: list) -> list:
    """
    The findings not already alerted on inside _SECRET_DEDUP_TTL — the same
    secret in several commits of one push, or pushed again after a rebase, is
    reported once; a different secret always is.

    Fails closed like _already_reported: with Redis down nothing is reported,
    and that is metered, rather than every push opening a duplicate.
    """
    try:
        from app.core.redis_client import get_redis

        r = get_redis()
        fresh, seen = [], set()
        for f in findings:
            fp = _secret_fingerprint(f)
            if fp in seen:
                continue
            seen.add(fp)
            if r.set(f"push_secret_seen:{repo}:{fp}", "1", nx=True, ex=_SECRET_DEDUP_TTL):
                fresh.append(f)
        return fresh
    except Exception as e:
        from app.core.metrics import metrics

        metrics.increment("dedup.redis_unavailable")
        _log.warning(f"push.secret_dedup_unavailable repo={repo}: {e} — suppressing report")
        return []


def _open_secret_issue(repo: str, token: str, findings: list, log, config=None) -> None:
    """
    One open secret alert per repo per 24h.

    Subsequent findings comment on that issue rather than opening another. The
    old key hashed the SET OF PATTERN NAMES, so two pushes with different
    finding mixes produced different keys and bypassed each other entirely —
    seven issues landed in this repo inside 73 seconds that way.
    """
    body = format_secret_findings(findings, repo)
    key = f"secret_alert:{repo}"

    existing = None
    try:
        from app.core.redis_client import get_redis

        existing = get_redis().get(key)
    except Exception as e:
        log.error(f"push.secret_alert_lookup_failed repo={repo}: {e}")

    if existing:
        try:
            issue = gh_get(f"/repos/{repo}/issues/{int(existing)}", token)
            if issue.get("state") == "open":
                gh_post(f"/repos/{repo}/issues/{int(existing)}/comments", token, {"body": body})
                log.info(f"push.secret_alert_appended issue=#{existing}")
                return
        except Exception as e:
            log.warning(f"push.secret_alert_reuse_failed issue=#{existing}: {e} — opening new")

    created = gh_post(
        f"/repos/{repo}/issues",
        token,
        {
            "title": f"🚨 Secret detected in push — {len(findings)} finding(s)",
            "body": body,
            "labels": ["security", "critical"],
        },
    )

    try:
        from app.core.redis_client import get_redis

        get_redis().set(key, str(created.get("number", "")), ex=_SECRET_ALERT_TTL)
    except Exception as e:
        log.debug(f"push.secret_alert_record_failed: {e}")

    notify_secret_detected(repo, len(findings), config=config)
    log.warning(f"Secret scan: {len(findings)} actionable findings posted")


# ── Dependency scan ────────────────────────────────────────────────────────────


def _scan_dependencies(repo, commits, token, config, log, ref: str = "") -> None:
    """
    Sprint 2 fix:
    - Only HIGH/CRITICAL findings create GitHub issues
    - LOW/MODERATE are logged only (no spam)
    - 24h dedup per file per repo
    """
    changed_files = set()
    for commit in commits:
        changed_files.update(commit.get("added", []))
        changed_files.update(commit.get("modified", []))

    dep_files = [f for f in changed_files if f in ("requirements.txt", "requirements-dev.txt")]

    for dep_file in dep_files:
        try:
            # At the pushed commit. Without ?ref= this read the DEFAULT
            # branch, so a feature-branch change to requirements.txt (with
            # scan_all_branches) was judged by a file it had not changed.
            path = f"/repos/{repo}/contents/{dep_file}" + (f"?ref={ref}" if ref else "")
            file_data = gh_get(path, token)
            content = base64.b64decode(file_data["content"]).decode("utf-8")
            all_findings = scan_requirements_txt(content)

            if not all_findings:
                log.info(f"push.dep_scan_clean file={dep_file}")
                continue

            for f in all_findings:
                log.info(
                    f"push.dep_finding pkg={f.package} ver={f.version} "
                    f"sev={f.severity} cve={f.cve_id}"
                )

            actionable = get_actionable_findings(all_findings)

            if not actionable:
                low_count = len([f for f in all_findings if f.severity == "LOW"])
                mod_count = len([f for f in all_findings if f.severity == "MODERATE"])
                log.info(
                    f"push.dep_scan_ok file={dep_file} "
                    f"low={low_count} moderate={mod_count} — no issue created (accepted risk)"
                )
                continue

            report_key = f"dep_high_{dep_file}"
            if _already_reported(repo, report_key, ttl_seconds=86400):
                log.info(f"push.dep_scan_dedup file={dep_file} (HIGH reported in last 24h)")
                continue

            gh_post(
                f"/repos/{repo}/issues",
                token,
                {
                    "title": f"🔴 HIGH severity dependency in {dep_file}",
                    "body": format_dep_findings(all_findings),
                    "labels": ["security", "dependencies"],
                },
            )
            log.warning(f"Dep scan: {len(actionable)} HIGH findings in {dep_file}")

        except Exception as e:
            log.error(f"Dep scan failed for {dep_file}: {e}")


# ── Commit lint ────────────────────────────────────────────────────────────────


def _lint_commits(repo, commits, token, config, log, branch: str = "") -> None:
    bad_commits = []
    for commit in commits:
        msg = commit.get("message", "").split("\n")[0].strip()
        if not _is_conventional(msg):
            bad_commits.append({"sha": commit["id"][:7], "message": msg})

    threshold = config.get("push", "create_issue_threshold", default=3)

    if len(bad_commits) < threshold:
        log.info(f"push.commit_lint ok — {len(bad_commits)} non-conventional below threshold")
        return

    if _already_reported(repo, "commit_lint", ttl_seconds=21600):
        log.info("push.commit_lint_skipped (reported in last 6h)")
        return

    rows = "\n".join(f"| `{c['sha']}` | {c['message']} |" for c in bad_commits)
    body = f"""## ⚡ Commit Convention Alert

These commits don't follow [Conventional Commits](https://www.conventionalcommits.org/) format:

| SHA | Message |
|-----|---------|
{rows}

### Required Format
```
type(scope): description
```

### Valid Types
`feat` `fix` `docs` `refactor` `test` `chore` `perf` `ci` `style` `build`

> 💡 Use `/fix` on this issue for AI help.
> ⚡ Use `/apply` to auto-fix commit messages.
"""
    try:
        gh_post(
            f"/repos/{repo}/issues",
            token,
            {
                "title": (
                    f"⚡ {len(bad_commits)} non-conventional commits pushed to "
                    f"{branch or 'the default branch'}"
                ),
                "body": body,
                "labels": ["commit-convention", "help wanted ⚠️"],
            },
        )
        log.done(f"Commit lint issue created: {len(bad_commits)} bad commits")
    except GitHubError as e:
        log.error(f"Failed to create lint issue: {e}")


# ── Helpers ────────────────────────────────────────────────────────────────────


def _is_conventional(msg: str) -> bool:
    if not msg:
        return False
    pattern = r"^(" + "|".join(CONVENTIONAL_TYPES) + r")(\([^)]+\))?!?:\s.+"
    return bool(re.match(pattern, msg))
