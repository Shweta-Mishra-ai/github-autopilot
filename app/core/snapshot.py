"""
app/core/snapshot.py
V5 — Atomic bot_actions using Redis list.

FIXES vs V4:
  1. RACE CONDITION in record_bot_action(): V4 did r.get() → mutate → r.set(),
     a classic non-atomic read-modify-write. Two concurrent /autofix calls on
     the same repo could both read the same snapshot, each append their action,
     and one silently overwrite the other. Actions were lost.

     Fixed by storing bot_actions as a separate Redis list key
     (snapshot_actions:{repo}:{snap_id}) and using lpush() which is atomic.
     list_snapshots() and get_snapshot() reconstruct the full action list from
     the separate list key. The base snapshot JSON is now write-once.

  2. take_snapshot() index update: also replaced non-atomic get→modify→set
     with atomic lpush + ltrim on a Redis list for the snapshot index.
"""

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from app.core.redis_client import get_redis
from app.github.client import gh_get

log = logging.getLogger(__name__)

MAX_SNAPSHOTS = 10
SNAPSHOT_TTL = 7 * 24 * 60 * 60  # 7 days


def _make_id() -> str:
    return uuid.uuid4().hex[:8]


def take_snapshot(repo: str, token: str, trigger: str = "manual") -> str | None:
    try:
        issues_data = gh_get(f"/repos/{repo}/issues?state=open&per_page=20", token)
        prs_data = gh_get(f"/repos/{repo}/pulls?state=open&per_page=10", token)
        commits = gh_get(f"/repos/{repo}/commits?per_page=5", token)
        repo_data = gh_get(f"/repos/{repo}", token)

        # Every read below is guarded. A snapshot is the only thing standing
        # between /rollback and an unrecoverable change, and take_snapshot()
        # swallows exceptions and returns None — so a single unexpected field
        # in any of four API responses used to mean "no snapshot", silently,
        # with the rollback proceeding regardless. `or []` rather than a
        # truthiness check because an error response is a dict, and iterating
        # a dict yields its keys as strings.
        issues_data = issues_data if isinstance(issues_data, list) else []
        prs_data = prs_data if isinstance(prs_data, list) else []
        commits = commits if isinstance(commits, list) else []
        repo_data = repo_data if isinstance(repo_data, dict) else {}

        open_issues = [
            {
                "number": i.get("number"),
                "title": i.get("title", ""),
                "labels": [
                    lbl.get("name", "") for lbl in (i.get("labels") or []) if isinstance(lbl, dict)
                ],
            }
            for i in issues_data
            if isinstance(i, dict) and "pull_request" not in i
        ]

        open_prs = [
            {
                "number": p.get("number"),
                "title": p.get("title", ""),
                # GitHub sends a null head for a PR from a deleted fork.
                "head": (p.get("head") or {}).get("ref", ""),
            }
            for p in prs_data
            if isinstance(p, dict)
        ]

        latest_sha = ""
        if commits and isinstance(commits[0], dict):
            latest_sha = commits[0].get("sha", "") or ""

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
        }

        r = get_redis()
        snap_id = snapshot["id"]
        snap_key = f"snapshot:{repo}:{snap_id}"
        index_key = f"snapshot_index:{repo}"

        # Write the base snapshot (write-once; bot_actions are stored separately)
        r.set(snap_key, json.dumps(snapshot), ex=SNAPSHOT_TTL)

        # FIXED: atomic list-based index — lpush + ltrim replaces get→modify→set
        r.lpush(index_key, snap_id)
        r.ltrim(index_key, 0, MAX_SNAPSHOTS - 1)
        r.expire(index_key, SNAPSHOT_TTL)

        log.info(f"snapshot.taken repo={repo} id={snap_id} trigger={trigger}")
        return snap_id

    except Exception as e:
        log.error(f"snapshot.take_failed repo={repo}: {e}")
        return None


def record_bot_action(repo: str, snap_id: str, action: dict):
    """
    Append a bot action to a snapshot.

    FIXED: V4 used r.get() → json.loads → mutate → r.set(), which is
    non-atomic and loses actions under concurrent writes. V5 stores actions
    in a separate Redis list and uses lpush() which is atomic.
    """
    try:
        from app.core.redis_client import get_redis

        r = get_redis()
        actions_key = f"snapshot_actions:{repo}:{snap_id}"
        r.lpush(actions_key, json.dumps(action))
        r.expire(actions_key, SNAPSHOT_TTL)

    except Exception as e:
        log.error(f"snapshot.record_action_failed: {e}")


def _get_bot_actions(r, repo: str, snap_id: str) -> list:
    """Load bot actions from the atomic list key. Returns newest-first."""
    try:
        actions_key = f"snapshot_actions:{repo}:{snap_id}"
        raw_list = r.lrange(actions_key, 0, -1)
        return [json.loads(item) for item in raw_list if item]
    except Exception:
        return []


def list_snapshots(repo: str) -> list[dict]:
    try:
        from app.core.redis_client import get_redis

        r = get_redis()
        index_key = f"snapshot_index:{repo}"
        snap_ids = r.lrange(index_key, 0, MAX_SNAPSHOTS - 1)

        if not snap_ids:
            return []

        summaries = []
        for snap_id in snap_ids:
            raw = r.get(f"snapshot:{repo}:{snap_id}")
            if not raw:
                continue

            snap = json.loads(raw)
            actions = _get_bot_actions(r, repo, snap_id)
            # .get() throughout: one snapshot written by an older version, or
            # truncated, used to raise KeyError out of the loop and take the
            # entire list with it — so a single malformed entry made every
            # snapshot unlistable and /rollback report "none available".
            state = snap.get("state") or {}
            latest_commit = state.get("latest_commit") or ""

            summaries.append(
                {
                    "id": snap_id,
                    "number": len(summaries) + 1,
                    "trigger": snap.get("trigger", "unknown"),
                    "timestamp": snap.get("timestamp", ""),
                    "issues_count": state.get("open_issues_count", 0),
                    "prs_count": state.get("open_prs_count", 0),
                    "commit": latest_commit[:7] if latest_commit else "—",
                    "bot_actions": len(actions),
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
        if not raw:
            return None

        snap = json.loads(raw)
        snap["bot_actions"] = _get_bot_actions(r, repo, snap_id)
        return snap

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
    # Derived from what actually exists. The examples were hardcoded to 1 and
    # 2, so a repo with a single snapshot was told to run `/rollback 2`, which
    # can only answer "Snapshot #2 Not Found".
    highest = snapshots[-1]["number"]
    examples = ["- `/rollback 1` — preview the most recent snapshot"]
    if highest > 1:
        examples.append(f"- `/rollback {highest}` — preview the oldest kept snapshot")
    examples.append("- `/rollback 1 confirm` — actually restore it")

    return f"""## 📸 Repo Snapshots — `{repo}`

| # | Trigger | Taken At | State | Commit | Bot Actions |
|---|---------|----------|-------|--------|-------------|
{table}

### How to restore
{chr(10).join(examples)}

> ⚠️ Rollback undoes the bot's own recorded actions (issues it opened, titles
> it rewrote, labels it added). It does **not** revert code commits.

---
*Snapshots expire after 7 days. Last {len(snapshots)} shown.*"""


def format_rollback_result(repo: str, snap: dict, restored: list[str], failed: list[str]) -> str:
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
