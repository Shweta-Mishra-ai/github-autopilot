"""
app/core/snapshot.py
V4 Sprint 3: Repo snapshot and rollback system.
"""

import json
import logging
import time
import uuid
from datetime import datetime, timezone

log = logging.getLogger(__name__)

MAX_SNAPSHOTS = 10
SNAPSHOT_TTL = 7 * 24 * 60 * 60  # 7 days


def _make_id() -> str:
    return uuid.uuid4().hex[:8]


def take_snapshot(repo: str, token: str, trigger: str = "manual") -> str | None:
    try:
        from app.github.client import gh_get
        from app.core.redis_client import get_redis

        issues_data = gh_get(f"/repos/{repo}/issues?state=open&per_page=20", token)
        prs_data = gh_get(f"/repos/{repo}/pulls?state=open&per_page=10", token)
        commits = gh_get(f"/repos/{repo}/commits?per_page=5", token)
        repo_data = gh_get(f"/repos/{repo}", token)

        open_issues = [
            {
                "number": i["number"],
                "title": i["title"],
                "labels": [item["name"] for item in i["labels"]],
            }
            for i in issues_data
            if "pull_request" not in i
        ]

        open_prs = [
            {
                "number": p["number"],
                "title": p["title"],
                "head": p["head"]["ref"],
            }
            for p in prs_data
        ]

        latest_sha = commits[0]["sha"] if commits else ""

        snapshot = {
            "id": _make_id(),
            "repo": repo,
            "trigger": trigger,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "timestamp_ts": int(time.time()),
            "state": {
                "open_issues_count": len(open_issues),
                "open_prs_count": len(open_prs),
                "open_issues": open_issues[:20],
                "open_prs": open_prs[:10],
                "latest_commit": latest_sha,
                "default_branch": repo_data.get("default_branch", "main"),
                "stars": repo_data.get("stargazers_count", 0),
            },
            "bot_actions": [],
        }

        r = get_redis()
        snap_id = snapshot["id"]
        snap_key = f"snapshot:{repo}:{snap_id}"
        index_key = f"snapshot_index:{repo}"

        r.set(snap_key, json.dumps(snapshot), ex=SNAPSHOT_TTL)

        index_raw = r.get(index_key)
        index = json.loads(index_raw) if index_raw else []
        index.insert(0, snap_id)
        index = index[:MAX_SNAPSHOTS]

        r.set(index_key, json.dumps(index), ex=SNAPSHOT_TTL)

        log.info(f"snapshot.taken repo={repo} id={snap_id} trigger={trigger}")
        return snap_id

    except Exception as e:
        log.error(f"snapshot.take_failed repo={repo}: {e}")
        return None


def record_bot_action(repo: str, snap_id: str, action: dict):
    try:
        from app.core.redis_client import get_redis

        r = get_redis()
        snap_key = f"snapshot:{repo}:{snap_id}"
        raw = r.get(snap_key)

        if not raw:
            return

        snapshot = json.loads(raw)
        snapshot["bot_actions"].append(action)

        r.set(snap_key, json.dumps(snapshot), ex=SNAPSHOT_TTL)

    except Exception as e:
        log.error(f"snapshot.record_action_failed: {e}")


def list_snapshots(repo: str) -> list[dict]:
    try:
        from app.core.redis_client import get_redis

        r = get_redis()
        index_key = f"snapshot_index:{repo}"
        index_raw = r.get(index_key)

        if not index_raw:
            return []

        index = json.loads(index_raw)
        summaries = []

        for snap_id in index:
            raw = r.get(f"snapshot:{repo}:{snap_id}")
            if not raw:
                continue

            snap = json.loads(raw)

            summaries.append(
                {
                    "id": snap_id,
                    "number": len(summaries) + 1,
                    "trigger": snap.get("trigger", "unknown"),
                    "timestamp": snap.get("timestamp", ""),
                    "issues_count": snap["state"]["open_issues_count"],
                    "prs_count": snap["state"]["open_prs_count"],
                    "commit": snap["state"]["latest_commit"][:7]
                    if snap["state"].get("latest_commit")
                    else "—",
                    "bot_actions": len(snap.get("bot_actions", [])),
                }
            )

        return summaries

    except Exception as e:
        log.error(f"snapshot.list_failed repo={repo}: {e}")
        return []


def get_snapshot(repo: str, snap_id: str) -> dict | None:
    try:
        from app.core.redis_client import get_redis

        r = get_redis()
        raw = r.get(f"snapshot:{repo}:{snap_id}")

        return json.loads(raw) if raw else None

    except Exception as e:
        log.error(f"snapshot.get_failed repo={repo} id={snap_id}: {e}")
        return None


def get_snapshot_by_number(repo: str, number: int) -> dict | None:
    snapshots = list_snapshots(repo)

    for snap in snapshots:
        if snap["number"] == number:
            return get_snapshot(repo, snap["id"])

    return None


def format_snapshot_list(repo: str) -> str:
    snapshots = list_snapshots(repo)

    if not snapshots:
        return (
            "## 📸 No Snapshots Available\n\n"
            "Snapshots are taken automatically before major bot actions.\n"
            "No recent snapshots found for this repo."
        )

    rows = []
    for s in snapshots:
        ts = s["timestamp"][:16].replace("T", " ") if s["timestamp"] else "—"

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
- `/rollback 1`
- `/rollback 2`

> ⚠️ Rollback does NOT revert code commits.

---
*Snapshots expire after 7 days. Last {len(snapshots)} shown.*"""


def format_rollback_result(
    repo: str, snap: dict, restored: list[str], failed: list[str]
) -> str:
    snap_ts = snap.get("timestamp", "")[:16].replace("T", " ")

    success_lines = "\n".join(f"- ✅ {r}" for r in restored) or "- Nothing to restore"
    fail_lines = "\n".join(f"- ❌ {f}" for f in failed)

    result = f"""## ↩️ Rollback Complete

**Restored to snapshot from:** `{snap_ts} UTC`
**Trigger:** `{snap.get("trigger", "unknown")}`

### Actions Taken
{success_lines}
"""

    if fail_lines:
        result += f"\n### Failed\n{fail_lines}\n"

    result += "\n---\n*State before rollback is saved as a new snapshot automatically.*"

    return result
