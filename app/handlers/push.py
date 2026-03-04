"""
Push Handler - app/handlers/push.py
Handles push webhook events — enforces conventional commits.
"""

import re
import logging
from app.github.auth import get_installation_token
from app.github.client import gh_post, GitHubError
from app.core.config import load_config
from app.core.logger import EventLogger

SKIP_AUTHORS = {"dependabot[bot]", "renovate[bot]", "github-actions[bot]"}

CONVENTIONAL = re.compile(
    r'^(feat|fix|docs|style|refactor|perf|test|chore|ci|build|revert)(\(.+\))?(!)?: .+',
    re.IGNORECASE
)


def handle(payload: dict):
    ref = payload.get("ref", "")
    if not any(b in ref for b in ["/main", "/master"]):
        return

    commits = payload.get("commits", [])
    repo = payload["repository"]["full_name"]
    installation_id = payload.get("installation", {}).get("id")

    if not installation_id or not commits:
        return

    log = EventLogger("push", repo=repo)

    try:
        token = get_installation_token(installation_id)
    except Exception as e:
        log.error(f"Auth failed: {e}")
        return

    config = load_config(repo, token)

    if not config.get("push", "enabled", default=True):
        return

    if not config.get("push", "enforce_conventional_commits", default=True):
        return

    # Find non-conventional commits
    bad = [
        (c["id"][:7], c["message"].split("\n")[0])
        for c in commits[:10]
        if not CONVENTIONAL.match(c["message"].split("\n")[0])
        and not c["message"].startswith("Merge")
        and c.get("author", {}).get("name", "") not in SKIP_AUTHORS
    ]

    if not bad:
        log.info(f"All {len(commits)} commits follow convention ✅")
        return

    threshold = config.get("push", "create_issue_threshold", default=3)
    log.info(f"{len(bad)} non-conventional commits — threshold is {threshold}")

    if len(bad) < threshold:
        log.info("Below threshold — skipping issue creation")
        return

    rows = "\n".join(f"| `{sha}` | `{msg[:60]}` |" for sha, msg in bad)

    try:
        gh_post(f"/repos/{repo}/issues", token, {
            "title": f"⚡ {len(bad)} non-conventional commits pushed to main",
            "body": f"""## Commit Convention Alert

These commits don't follow [Conventional Commits](https://conventionalcommits.org) format:

| SHA | Message |
|-----|---------|
{rows}

### Required Format
```
type(scope): description
```

### Valid Types
`feat` `fix` `docs` `refactor` `test` `chore` `perf` `ci` `style` `build`

### Examples
```
feat: add user authentication
fix(api): handle null response from Groq
docs: update README setup guide
refactor(auth): simplify JWT encoding
```

> 💡 Use `/fix` command on this issue for AI help fixing commit messages.
""",
            "labels": ["help wanted 🙏"]
        })
        log.done(f"Created commit convention issue for {len(bad)} bad commits")
    except GitHubError as e:
        log.warning(f"Could not create issue: {e}")
