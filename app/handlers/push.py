"""
Push Handler - app/handlers/push.py
V4: Dedup fix — no more duplicate issues on every push.

FIXED: _scan_dependencies() creates issue on EVERY push touching requirements.txt
  → Same vulnerabilities → new issue every time → spam.
  Fix: Redis key with 24h TTL. Same finding → skip.

FIXED: _lint_commits() same problem — duplicate commit convention issues.
  Fix: Redis key with 6h TTL per repo.
"""

import base64
import re

from app.github.auth import get_installation_token
from app.github.client import gh_get, gh_post, GitHubError
from app.github.notifications import notify_secret_detected
from app.core.config import load_config
from app.core.logger import EventLogger
from app.security.secrets import scan_diff, format_findings as format_secret_findings
from app.security.dependencies import scan_requirements_txt, format_findings as format_dep_findings

CONVENTIONAL_TYPES = {
    "feat", "fix", "docs", "refactor", "test",
    "chore", "perf", "ci", "style", "build"
}
SKIP_AUTHORS = {
    "dependabot[bot]", "renovate[bot]",
    "github-actions[bot]", "ai-repo-manager[bot]"
}


def handle(payload: dict):
    repo            = payload["repository"]["full_name"]
    installation_id = payload["installation"]["id"]
    pusher          = payload.get("pusher", {}).get("name", "")
    commits         = payload.get("commits", [])
    ref             = payload.get("ref", "")

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

    config     = load_config(repo, token)
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


# ── Dedup helper ──────────────────────────────────────────────────────────────

def _already_reported(repo: str, report_type: str, ttl_seconds: int = 86400) -> bool:
    """
    Returns True if same report was already created recently.
    Uses Redis NX key with TTL.
    First call → sets key → returns False (not duplicate).
    Second call within TTL → key exists → returns True (duplicate → skip).
    """
    try:
        from app.core.redis_client import get_redis
        r      = get_redis()
        key    = f"push_reported:{repo}:{report_type}"
        result = r.set(key, "1", nx=True, ex=ttl_seconds)
        return result is None   # None = key existed = already reported
    except Exception:
        return False  # Redis unavailable → allow (better missing nothing than spamming)


# ── Handlers ──────────────────────────────────────────────────────────────────

def _scan_secrets(repo, commits, token, config, log):
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

    if all_findings:
        try:
            gh_post(f"/repos/{repo}/issues", token, {
                "title":  f"🚨 Secret detected in push — {len(all_findings)} finding(s)",
                "body":   format_secret_findings(all_findings, repo),
                "labels": ["security", "critical"]
            })
            notify_secret_detected(repo, len(all_findings))
            log.warning(f"Secret detection: {len(all_findings)} findings posted")
        except Exception as e:
            log.error(f"Failed to post secret alert: {e}")


def _scan_dependencies(repo, commits, token, config, log):
    changed_files = set()
    for commit in commits:
        changed_files.update(commit.get("added", []))
        changed_files.update(commit.get("modified", []))

    dep_files = [f for f in changed_files
                 if f in ("requirements.txt", "requirements-dev.txt")]

    for dep_file in dep_files:
        try:
            file_data = gh_get(f"/repos/{repo}/contents/{dep_file}", token)
            content   = base64.b64decode(file_data["content"]).decode("utf-8")
            findings  = scan_requirements_txt(content)

            if not findings:
                continue

            # ✅ DEDUP FIX: Skip if same dep scan reported in last 24 hours
            report_key = f"dep_scan_{dep_file}"
            if _already_reported(repo, report_key, ttl_seconds=86400):
                log.info(f"push.dep_scan_skipped file={dep_file} (reported in last 24h)")
                continue

            gh_post(f"/repos/{repo}/issues", token, {
                "title":  f"⚠️ Vulnerable dependencies found in {dep_file}",
                "body":   format_dep_findings(findings),
                "labels": ["security", "dependencies"]
            })
            log.warning(f"Dependency scan: {len(findings)} vulnerable packages in {dep_file}")

        except Exception as e:
            log.error(f"Dependency scan failed for {dep_file}: {e}")


def _lint_commits(repo, commits, token, config, log):
    bad_commits = []
    for commit in commits:
        msg = commit.get("message", "").split("\n")[0].strip()
        if not _is_conventional(msg):
            bad_commits.append({"sha": commit["id"][:7], "message": msg})

    threshold = config.get("push", "create_issue_threshold", default=3)

    if len(bad_commits) < threshold:
        log.info(f"push | {len(bad_commits)} non-conventional — below threshold")
        return

    # ✅ DEDUP FIX: Skip if commit lint issue reported in last 6 hours
    if _already_reported(repo, "commit_lint", ttl_seconds=21600):
        log.info(f"push.commit_lint_skipped (reported in last 6h)")
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

> 💡 Use `/fix` command on this issue for AI help fixing commit messages.
> ⚡ Use `/apply` to automatically fix all commit messages.
"""

    try:
        gh_post(f"/repos/{repo}/issues", token, {
            "title":  f"⚡ {len(bad_commits)} non-conventional commits pushed to main",
            "body":   body,
            "labels": ["commit-convention", "help wanted ⚠️"]
        })
        log.done(f"Commit lint issue created: {len(bad_commits)} bad commits")
    except GitHubError as e:
        log.error(f"Failed to create issue: {e}")


def _index_changed_files(repo, commits, token, latest_sha, log):
    """Index changed files into vector DB — silent."""
    try:
        from app.intelligence.embeddings import embed_file

        changed_files = set()
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
                file_data = gh_get(f"/repos/{repo}/contents/{filepath}", token)
                content   = base64.b64decode(file_data["content"]).decode("utf-8")
                if embed_file(repo, filepath, content, latest_sha):
                    indexed += 1
            except Exception:
                pass

        if indexed > 0:
            log.info(f"intelligence.indexed {indexed}/{len(indexable)} files")

    except Exception as e:
        log.debug(f"Intelligence indexing skipped: {e}")


def _is_conventional(msg: str) -> bool:
    if not msg:
        return False
    pattern = r'^(' + '|'.join(CONVENTIONAL_TYPES) + r')(\([^)]+\))?!?:\s.+'
    return bool(re.match(pattern, msg))
