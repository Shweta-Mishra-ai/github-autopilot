"""
app/core/analytics.py
Repo analytics for /report: merges, issue closures and command use.

Only what something records is reported. record_bot_action() and
record_review_score() had no callers, so the report's "Total actions" was
always 0 and its code-quality grade was computed from nothing; both are gone.
"""

import logging
from datetime import datetime, timezone, timedelta
from app.core import redis_client

log = logging.getLogger(__name__)


def hours_between(start_iso: str, end_iso: str) -> float | None:
    """Hours between two GitHub ISO timestamps, or None if either is missing."""
    try:
        start = datetime.fromisoformat(str(start_iso).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(end_iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return max(0.0, (end - start).total_seconds() / 3600)


def record_pr_merged(repo: str, pr_number: int, hours_open: float):
    _incr(f"analytics:{repo}:prs_merged:{_today()}")
    _lpush(f"analytics:{repo}:pr_merge_hours:{_week()}", hours_open)


def record_issue_closed(repo: str, issue_number: int, hours_open: float):
    _incr(f"analytics:{repo}:issues_closed:{_today()}")
    _lpush(f"analytics:{repo}:issue_hours:{_week()}", hours_open)


def record_command_used(repo: str, command: str):
    cmd = command.lstrip("/")
    _incr(f"analytics:{repo}:cmd:{cmd}:{_today()}")


class AnalyticsUnavailable(Exception):
    """The counters could not be read. Every read swallowed its error and
    answered 0, so with Redis down /report said "No activity recorded" — a
    claim about the repository, made from a failure to look."""


def _week_days() -> list[str]:
    """Monday of this ISO week through today, as the daily key suffixes."""
    now = datetime.now(timezone.utc)
    monday = now - timedelta(days=now.weekday())
    return [(monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(now.weekday() + 1)]


def get_weekly_report(repo: str) -> dict:
    """
    This ISO week so far, every figure over the same window.

    The report was titled "weekly" but showed merges and closures for TODAY
    beside averages for the week, and command counts that were cumulative
    (their key's TTL was refreshed on every use). Daily counters are summed
    over the week's days instead. Raises AnalyticsUnavailable when Redis
    cannot be read.
    """
    week = _week()
    days = _week_days()
    try:
        r = redis_client.get_redis()
        merge_hours = _get_list(r, f"analytics:{repo}:pr_merge_hours:{week}")
        issue_hours = _get_list(r, f"analytics:{repo}:issue_hours:{week}")
        merged = sum(_get_int(r, f"analytics:{repo}:prs_merged:{d}") for d in days)
        closed = sum(_get_int(r, f"analytics:{repo}:issues_closed:{d}") for d in days)
        commands = _get_top_commands(r, repo, days)
    except Exception as e:
        raise AnalyticsUnavailable(f"{type(e).__name__}: {e}") from e
    avg_merge = _avg(merge_hours)
    avg_issue = _avg(issue_hours)

    return {
        "repo": repo,
        "week": week,
        "prs": {
            "merged": merged,
            "avg_merge_hours": round(avg_merge, 1),
            "avg_merge_days": round(avg_merge / 24, 1) if avg_merge else 0,
        },
        "issues": {
            "closed": closed,
            "avg_close_hours": round(avg_issue, 1),
        },
        "bot_usage": {"top_commands": commands},
    }


def format_report_comment(repo: str) -> str:
    """
    The weekly report, or "" when nothing has been recorded yet.

    Nothing recorded these figures until 2026-10-08: record_pr_merged,
    record_issue_closed and record_command_used (except for /report itself)
    had no callers, so every repository was shown "0 merged" and "Grade F".
    The code-quality grade is gone: it was an average of review SCORES, and
    reviews no longer invent a score (counted findings replaced it).
    """
    d = get_weekly_report(repo)
    prs = d["prs"]
    iss = d["issues"]
    bot = d["bot_usage"]
    if not (
        prs["merged"]
        or prs["avg_merge_hours"]
        or iss["closed"]
        or iss["avg_close_hours"]
        or bot["top_commands"]
    ):
        return ""

    merge_disp = (
        f"{prs['avg_merge_hours']}h" if prs["avg_merge_hours"] < 24 else f"{prs['avg_merge_days']}d"
    )
    close_disp = (
        f"{iss['avg_close_hours']}h"
        if iss["avg_close_hours"] < 24
        else f"{round(iss['avg_close_hours'] / 24, 1)}d"
    )

    cmd_rows = (
        "\n".join(f"| `/{cmd}` | {cnt} |" for cmd, cnt in (bot["top_commands"] or {}).items())
        or "| — | — |"
    )

    return f"""## 📊 Weekly Repo Report — `{repo}`
*Week of {d["week"]}*

### 🔀 Pull Requests
| Metric | Value |
|--------|-------|
| **Merged this week** | {prs["merged"]} |
| **Avg Merge Time** | {merge_disp} |

### 🐛 Issues
| Metric | Value |
|--------|-------|
| **Closed this week** | {iss["closed"]} |
| **Avg Resolution** | {close_disp} |

### 🤖 Commands this week
| Command | Uses |
|---------|------|
{cmd_rows}

---
*🤖 GitHub Autopilot — Use `/report` anytime for fresh stats*"""


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _week() -> str:
    now = datetime.now(timezone.utc)
    week = now - timedelta(days=now.weekday())
    # %G (ISO year) with %V (ISO week): %Y-W%V labelled Monday 2024-12-30
    # "2024-W01", a week that is a year earlier.
    return week.strftime("%G-W%V")


def _incr(key: str, ttl: int = 604800):
    try:
        r = redis_client.get_redis()
        r.incr(key)
        r.expire(key, ttl)
    except Exception as e:
        log.debug(f"analytics.incr_failed key={key}: {e}")


def _lpush(key: str, value, ttl: int = 604800):
    try:
        r = redis_client.get_redis()
        r.lpush(key, str(value))
        r.ltrim(key, 0, 999)
        r.expire(key, ttl)
    except Exception as e:
        log.debug(f"analytics.lpush_failed key={key}: {e}")


def _get_int(r, key: str) -> int:
    val = r.get(key)
    return int(val) if val else 0


def _get_list(r, key: str) -> list:
    return [float(v) for v in r.lrange(key, 0, -1) or [] if v]


def _avg(values: list) -> float:
    return sum(values) / len(values) if values else 0.0


def _get_top_commands(r, repo: str, days: list[str], top_n: int = 5) -> dict:
    """
    The most-used commands this week. Every registered command is counted:
    this read a hard-coded list of fifteen, so /merge, /summarize, /impact,
    /apply and the rest were recorded and never shown — a repository that
    used only those was told "No Data Yet".
    """
    from app.core.commands import ALL_COMMANDS

    counts = {}
    for command in ALL_COMMANDS:
        cmd = command.lstrip("/")
        n = sum(_get_int(r, f"analytics:{repo}:cmd:{cmd}:{d}") for d in days)
        if n > 0:
            counts[cmd] = n
    return dict(sorted(counts.items(), key=lambda x: x[1], reverse=True)[:top_n])
