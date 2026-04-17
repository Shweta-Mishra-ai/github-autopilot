"""
app/core/snapshot.py
V4 Sprint 3: Repo snapshot and rollback system.

What it does:
  - Takes a snapshot of repo state (open issues, PRs, recent commits)
    before any major automated action
  - Stores snapshots in Redis (last 10 per repo, 7-day TTL)
  - /rollback command shows history and lets maintainers restore

Snapshot stores:
  - Timestamp
  - Trigger (what action caused the snapshot)
  - Open issues count + titles
  - Open PRs count + titles
  - Latest commit SHA
  - Bot-created issues/comments (so they can be cleaned up)

Usage:
    from app.core.snapshot import take_snapshot, list_snapshots, get_snapshot

    # Before doing anything automated
    snap_id = take_snapshot(repo, token, trigger="pr_analysis")

    # List available snapshots
    snaps = list_snapshots(repo)

    # Get specific snapshot
    snap = get_snapshot(repo, snap_id)
"""

import json
import logging
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

MAX_SNAPSHOTS  = 10
SNAPSHOT_TTL   = 7 * 24 * 60 * 60  # 7 days in seconds


def take_snapshot(repo: str, token: str, trigger: str = "manual") -> str | None:
    """
    Take a snapshot of current repo state.
    Returns snapshot_id or None on failure.
    """
    try:
        from app.github.client import gh_get
        from app.core.redis_client import get_redis

        # Gather state
        issues_data = gh_get(f"/repos/{repo}/issues?state=open&per_page=20", token)
        prs_data    = gh_get(f"/repos/{repo}/pulls?state=open&per_page=10", token)
        commits     = gh_get(f"/repos/{repo}/commits?per_page=5", token)
        repo_data   = gh_get(f"/repos/{repo}", token)

        open_issues = [
            {"number": i["number"], "title": i["title"], "labels": [l["name"] for l in i["labels"]]}
            for i in issues_data if "pull_request" not in i
        ]
        open_prs = [
            {"number": p["number"], "title": p["title"], "head": p["head"]["ref"]}
            for p in prs_data
        ]
        latest_sha = commits[0]["sha"] if commits else ""

        snapshot = {
            "id":           _make_id(),
            "repo":         repo,
            "trigger":      trigger,
            "timestamp":    datetime.now(timezone.utc).isoformat(),
            "timestamp_ts": int(time.time()),
            "state": {
                "open_issues_count": len(open_issues),
                "open_prs_count":    len(open_prs),
                "open_issues":       open_issues[:20],
                "open_prs":          open_prs[:10],
                "latest_commit":     latest_sha,
                "default_branch":    repo_data.get("default_branch", "main"),
                "stars":             repo_data.get("stargazers_count", 0),
            },
            "bot_actions": [],  # populated after bot takes actions
        }

        r         = get_redis()
        snap_id   = snapshot["id"]
        snap_key  = f"snapshot:{repo}:{snap_id}"
        index_key = f"snapshot_index:{repo}"

        # Store snapshot
        r.set(snap_key, json.dumps(snapshot), ex=SNAPSHOT_TTL)

        # Update index (list of snap IDs, newest first)
        index_raw = r.get(index_key)
        index = json.loads(index_raw) if index_raw else []
        index.insert(0, snap_id)
        index = index[:MAX_SNAPSHOTS]  # keep only last 10
        r.set(index_key, json.dumps(index), ex=SNAPSHOT_TTL)

        log.info(f"snapshot.taken repo={repo} id={snap_id} trigger={trigger}")
        return snap_id

    except Exception as e:
        log.error(f"snapshot.take_failed repo={repo}: {e}")
        return None


def record_bot_action(repo: str, snap_id: str, action: dict):
    """
    Record what the bot did after taking a snapshot.
    Used to enable rollback (undo bot actions).
    action = {"type": "create_issue", "number": 27, "title": "..."}
    """
    try:
        from app.core.redis_client import get_redis
        r        = get_redis()
        snap_key = f"snapshot:{repo}:{snap_id}"
        raw      = r.get(snap_key)
        if not raw:
            return
        snapshot = json.loads(raw)
        snapshot["bot_actions"].append(action)
        r.set(snap_key, json.dumps(snapshot), ex=SNAPSHOT_TTL)
    except Exception as e:
        log.error(f"snapshot.record_action_failed: {e}")


def list_snapshots(repo: str) -> list[dict]:
    """
    Returns list of snapshot summaries for a repo, newest first.
    Used by /rollback command to show available restore points.
    """
    try:
        from app.core.redis_client import get_redis
        r         = get_redis()
        index_key = f"snapshot_index:{repo}"
        index_raw = r.get(index_key)

        if not index_raw:
            return []

        index    = json.loads(index_raw)
        summaries = []

        for snap_id in index:
            raw = r.get(f"snapshot:{repo}:{snap_id}")
            if not raw:
                continue
            snap = json.loads(raw)
            summaries.append({
                "id":           snap_id,
                "number":       len(summaries) + 1,
                "trigger":      snap.get("trigger", "unknown"),
                "timestamp":    snap.get("timestamp", ""),
                "issues_count": snap["state"]["open_issues_count"],
                "prs_count":    snap["state"]["open_prs_count"],
                "commit":       snap["state"]["latest_commit"][:7] if snap["state"].get("latest_commit") else "—",
                "bot_actions":  len(snap.get("bot_actions", [])),
            })

        return summaries

    except Exception as e:
        log.error(f"snapshot.list_failed repo={repo}: {e}")
        return []


def get_snapshot(repo: str, snap_id: str) -> dict | None:
    """Get full snapshot data by ID."""
    try:
        from app.core.redis_client import get_redis
        r   = get_redis()
        raw = r.get(f"snapshot:{repo}:{snap_id}")
        return json.loads(raw) if raw else None
    except Exception as e:
        log.error(f"snapshot.get_failed repo={repo} id={snap_id}: {e}")
        return None


def get_snapshot_by_number(repo: str, number: int) -> dict | None:
    """Get snapshot by its display number (1 = most recent)."""
    snapshots = list_snapshots(repo)
    for snap in snapshots:
        if snap["number"] == number:
            return get_snapshot(repo, snap["id"])
    return None


def format_snapshot_list(repo: str) -> str:
    """
    Format snapshot list as GitHub comment.
    Used by /rollback (no args) to show available snapshots.
    """
    snapshots = list_snapshots(repo)

    if not snapshots:
        return (
            "## 📸 No Snapshots Available\n\n"
            "Snapshots are taken automatically before major bot actions.\n"
            "No recent snapshots found for this repo."
        )

    rows = []
    for s in snapshots:
        ts   = s["timestamp"][:16].replace("T", " ") if s["timestamp"] else "—"
        rows.append(
            f"| **#{s['number']}** | `{s['trigger']}` | {ts} UTC | "
            f"{s['issues_count']} issues, {s['prs_count']} PRs | "
            f"`{s['commit']}` | {s['bot_actions']} actions |"
        )

    table = "\n".join(rows)

    return f"""## 📸 Repo Snapshots — `{repo}`

| # | Trigger | Taken At | State | Commit | Bot Actions |
|---|---------|----------|-------|--------|-------------|
{table}

### How to restore
```
/rollback 1    ← restore most recent snapshot
/rollback 2    ← restore second most recent
```

> ⚠️ Rollback closes bot-created issues and reverts bot-edited PR titles.
> It does **not** revert code commits.

---
*Snapshots expire after 7 days. Last {len(snapshots)} shown.*"""


def format_rollback_result(
    repo: str,
    snap: dict,
    restored: list[str],
    failed: list[str],
) -> str:
    """Format rollback result comment."""
    snap_ts = snap.get("timestamp", "")[:16].replace("T", " ")

    success_lines = "\n".join(f"- ✅ {r}" for r in restored) or "- Nothing to restore"
    fail_lines    = "\n".join(f"- ❌ {f}" for f in failed)

    result = f"""## ↩️ Rollback Complete

**Restored to snapshot from:** `{snap_ts} UTC`
**Trigger:** `{snap.get('trigger', 'unknown')}`

### Actions Taken
{success_lines}
"""

    if fail_lines:
        result += f"\n### Failed\n{fail_lines}\n"

    result += "\n---\n*State before rollback is saved as a new snapshot automatically.*"
    return result

