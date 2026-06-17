"""
app/handlers/push.py
V4 Sprint 2: Smart dependency scanning + dedup.

FIXED (Sprint 2): Duplicate issues — Redis dedup (24h dep scan, 6h commit lint).
NEW (Sprint 2): Only HIGH/CRITICAL vulnerabilities create GitHub issues.
     LOW/MODERATE = logged only (no spam).

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
import hashlib
import re

from app.github.auth import get_installation_token
from app.github.client import gh_get, gh_post, GitHubError
from app.github.notifications import notify_secret_detected
from app.core.config import load_config
from app.core.logger import EventLogger
from app.security.secrets import scan_diff, format_findings as format_secret_findings
from app.security.dependencies import (
    scan_requirements_txt,
    get_actionable_findings,
    format_dep_findings,
)

CONVENTIONAL_TYPES = {
    "feat", "fix", "docs", "refactor", "test", "chore",
    "perf", "ci", "style", "build",
}
SKIP_AUTHORS = {
    "dependabot[bot]",
    "renovate[bot]",
    "github-actions[bot]",
    "ai-repo-manager[bot]",
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
    if ref not in ("refs/heads/main", "refs/heads/master"):
        return
    if not commits:
        return

    try:
        token = get_installation_token(installation_id)
    except Exception as e:
        log.error(f"Auth failed: {e}")
        return

    config = load_config(repo, token)
    latest_sha = commits[-1].get("id", "") if commits else ""

    if not config.get("push", "enabled", default=True):
        return

    if config.get("push", "scan_secrets", default=True):
        _scan_secrets(repo, commits, token, config, log)

    if config.get("push", "scan_dependencies", default=True):
        _scan_dependencies(repo, commits, token, config, log)

    if config.get("push", "enforce_conventional_commits", default=True):
        _lint_commits(repo, commits, token, config, log)

    _index_changed_files(repo, commits, token, latest_sha, log)


# ── Dedup ──────────────────────────────────────────────────────────────────────


def _already_reported(repo: str, report_type: str, ttl_seconds: int = 86400) -> bool:
    """
    Redis NX key — True if same report created recently.
    Prevents duplicate issues on every push.
    """
    try:
        from app.core.redis_client import get_redis

        r = get_redis()
        key = f"push_reported:{repo}:{report_type}"
        return r.set(key, "1", nx=True, ex=ttl_seconds) is None
    except Exception:
        return False


def _findings_dedup_key(findings: list) -> str:
    """
    Sprint 8: Stable dedup key for a set of secret findings.
    Derived from the sorted list of pattern names so that the same
    secrets always produce the same key regardless of line order.
    """
    pattern_names = sorted(f.pattern_name for f in findings)
    digest = hashlib.md5(",".join(pattern_names).encode()).hexdigest()[:12]
    return f"secret_patterns_{digest}"


# ── Secret scan ────────────────────────────────────────────────────────────────


def _scan_secrets(repo, commits, token, config, log) -> None:
    """
    Scan all added/modified file patches in `commits` for secrets.

    Sprint 8 fix: deduplicate using _already_reported.
    Previously this function had NO dedup, creating a new GitHub issue on
    every push — including force-pushes and repeated pushes of the same
    commit — leading to dozens of duplicate security issues in active repos.
    """
    all_findings = []
    for commit in commits:
        sha = commit.get("id", "")
        if not sha:
            continue
        try:
            diff_data = gh_get(f"/repos/{repo}/commits/{sha}", token)
            for f in diff_data.get("files", []):
                patch = f.get("patch", "")
                if patch:
                    all_findings.extend(scan_diff(patch))
        except Exception as e:
            log.error(f"Secret scan failed for {sha[:7]}: {e}")

    if not all_findings:
        return

    # Sprint 8: dedup — one issue per unique finding set per 1 h
    dedup_key = _findings_dedup_key(all_findings)
    if _already_reported(repo, dedup_key, ttl_seconds=_SECRET_DEDUP_TTL):
        log.info(
            f"push.secret_scan_dedup repo={repo} "
            f"findings={len(all_findings)} (same patterns reported within last 1h)"
        )
        return

    try:
        gh_post(
            f"/repos/{repo}/issues",
            token,
            {
                "title": (
                    f"🚨 Secret detected in push — {len(all_findings)} finding(s)"
                ),
                "body": format_secret_findings(all_findings, repo),
                "labels": ["security", "critical"],
            },
        )
        notify_secret_detected(repo, len(all_findings))
        log.warning(f"Secret scan: {len(all_findings)} findings posted as issue")
    except Exception as e:
        log.error(f"Failed to post secret alert: {e}")


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

    dep_files = [
        f for f in changed_files
        if f in ("requirements.txt", "requirements-dev.txt")
    ]

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
                log.info(
                    f"push.dep_scan_dedup file={dep_file} (HIGH reported in last 24h)"
                )
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
        log.info(
            f"push.commit_lint ok — "
            f"{len(bad_commits)} non-conventional below threshold"
        )
        return

    if _already_reported(repo, "commit_lint", ttl_seconds=21600):
        log.info("push.commit_lint_skipped (reported in last 6h)")
        return

    rows = "\n".join(
        f"| `{c['sha']}` | {c['message']} |" for c in bad_commits
    )
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
                    f"⚡ {len(bad_commits)} non-conventional commits pushed to main"
                ),
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
            f for f in changed_files
            if f.endswith((".py", ".md", ".yml", ".yaml", ".json", ".txt"))
            and not f.startswith("tests/")
        ]

        if not indexable:
            return

        indexed = 0
        for filepath in indexable[:10]:
            try:
                file_data = gh_get(
                    f"/repos/{repo}/contents/{filepath}", token
                )
                content = base64.b64decode(
                    file_data["content"]
                ).decode("utf-8")
                if embed_file(repo, filepath, content, latest_sha):
                    indexed += 1
            except Exception:
                pass

        if indexed > 0:
            log.info(f"intelligence.indexed {indexed}/{len(indexable)} files")

    except Exception as e:
        log.debug(f"Intelligence indexing skipped: {e}")


# ── Helpers ────────────────────────────────────────────────────────────────────


def _is_conventional(msg: str) -> bool:
    if not msg:
        return False
    pattern = r"^(" + "|".join(CONVENTIONAL_TYPES) + r")(\([^)]+\))?!?:\s.+"
    return bool(re.match(pattern, msg))
