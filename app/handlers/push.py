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

     Fix: Deduplicate per unique set of secret patterns (1h TTL).
     Key = "secret_findings:{repo}:{sorted_pattern_hash}" so:
       - Same secrets on the same repo within 1h → one issue only.
       - New/different secrets always create a fresh issue.
       - TTL is intentionally short (1h) so repeated leaks after a window
         are still caught and reported.
"""

import base64
import logging
import re

from app.github.auth import get_installation_token
from app.github.client import gh_get, gh_post, GitHubError
from app.github.notifications import notify_secret_detected
from app.core.config import load_config
from app.core.logger import EventLogger
from app.security.enhanced_secrets import scan_diff, format_findings as format_secret_findings
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


def handle(payload: dict) -> None:
    repo = payload["repository"]["full_name"]
    installation_id = payload["installation"]["id"]
    pusher = payload.get("pusher", {}).get("name", "")
    commits = payload.get("commits", [])
    ref = payload.get("ref", "")

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

    config = load_config(repo, token)
    latest_sha = commits[-1].get("id", "") if commits else ""

    # Helper, not a raw get(): it also honours the bot.enabled kill switch.
    if not config.push_enabled():
        return

    # Secret scan runs on ALL branches (secrets are dangerous everywhere).
    # Dependency + commit lint only run on default branch by default,
    # but can be extended via config push.scan_all_branches = true.
    is_default_branch = ref in ("refs/heads/main", "refs/heads/master")
    scan_all = config.get("push", "scan_all_branches", default=False)
    run_full_scan = is_default_branch or scan_all

    # Secret scan: ALL branches — secrets don't care which branch they're on
    if config.get("push", "scan_secrets", default=True):
        _scan_secrets(repo, commits, token, config, log)

    # Dep scan + commit lint: default branch only (or all if scan_all_branches=true)
    if run_full_scan:
        if config.get("push", "scan_dependencies", default=True):
            _scan_dependencies(repo, commits, token, config, log)

        if config.get("push", "enforce_conventional_commits", default=True):
            _lint_commits(repo, commits, token, config, log)

        # Writes the replacement message rather than only naming the problem.
        # Runs on the same branches as the lint it complements, and is its own
        # config switch: an operator who wants the report without a bot
        # commenting on their commits can have exactly that.
        if config.get("push", "suggest_commit_messages", default=True):
            from app.handlers.commit_message import suggest_commit_messages

            suggest_commit_messages(repo, commits, token, config, log)

    _index_changed_files(repo, commits, token, latest_sha, log)


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


# Paths that legitimately contain example/dummy secrets — scanning them only
# produces false-positive "secret detected" issues (the scanner's own fixtures,
# .env.example, docs snippets).
_SECRET_SCAN_SKIP = (
    ".env.example",
    ".env.sample",
    ".env.template",
)
_SECRET_SCAN_SKIP_DIRS = ("tests/", "test/", "fixtures/", "examples/", "docs/")


def _skip_secret_scan(filename: str) -> bool:
    """True if the file is a test/example/docs path where dummy secrets are expected."""
    if not filename:
        return False
    fn = filename.lower()
    base = fn.rsplit("/", 1)[-1]
    if base in _SECRET_SCAN_SKIP or base.endswith(".example"):
        return True
    if base.startswith("test_") or base.endswith("_test.py"):
        return True
    return any(d in fn for d in _SECRET_SCAN_SKIP_DIRS)


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

    if _already_reported(repo, "secret_scan", ttl_seconds=_SECRET_DEDUP_TTL):
        log.info(f"push.secret_scan_dedup repo={repo} findings={len(actionable)}")
        return

    try:
        _open_secret_issue(repo, token, actionable, log, config)
    except Exception as e:
        log.error(f"Failed to post secret alert: {e}")


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


def _scan_dependencies(repo, commits, token, config, log) -> None:
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
            file_data = gh_get(f"/repos/{repo}/contents/{dep_file}", token)
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


def _lint_commits(repo, commits, token, config, log) -> None:
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
                "title": (f"⚡ {len(bad_commits)} non-conventional commits pushed to main"),
                "body": body,
                "labels": ["commit-convention", "help wanted ⚠️"],
            },
        )
        log.done(f"Commit lint issue created: {len(bad_commits)} bad commits")
    except GitHubError as e:
        log.error(f"Failed to create lint issue: {e}")


# ── File indexing ──────────────────────────────────────────────────────────────


def _index_changed_files(repo, commits, token, latest_sha, log) -> None:
    """Index changed files into vector DB — silent."""
    try:
        from app.intelligence.embeddings import embed_file

        changed_files: set[str] = set()
        for commit in commits:
            changed_files.update(commit.get("added", []))
            changed_files.update(commit.get("modified", []))

        indexable = [
            f
            for f in changed_files
            if f.endswith((".py", ".md", ".yml", ".yaml", ".json", ".txt"))
            and not f.startswith("tests/")
        ]

        if not indexable:
            return

        indexed = 0
        failed = 0
        for filepath in indexable[:10]:
            try:
                file_data = gh_get(f"/repos/{repo}/contents/{filepath}", token)
                content = base64.b64decode(file_data["content"]).decode("utf-8")
                if embed_file(repo, filepath, content, latest_sha):
                    indexed += 1
            except Exception as e:
                # Optional feature — never fatal — but make the failure observable
                # instead of silent, so a repo that never indexes is diagnosable.
                failed += 1
                log.debug(f"intelligence.index_skip file={filepath}: {e}")

        if indexed > 0:
            log.info(f"intelligence.indexed {indexed}/{len(indexable)} files")
        if failed:
            from app.core.metrics import metrics

            metrics.increment("intelligence.index_failed")

    except Exception as e:
        log.debug(f"Intelligence indexing skipped: {e}")


# ── Helpers ────────────────────────────────────────────────────────────────────


def _is_conventional(msg: str) -> bool:
    if not msg:
        return False
    pattern = r"^(" + "|".join(CONVENTIONAL_TYPES) + r")(\([^)]+\))?!?:\s.+"
    return bool(re.match(pattern, msg))
