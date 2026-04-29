"""
app/core/analytics.py
V4 Sprint 6: Repo analytics tracker.
Tracks PR velocity, issue resolution, bot usage, review scores.
"""

import logging
from datetime import datetime, timezone, timedelta
from app.core import redis_client

log = logging.getLogger(__name__)


def record_pr_merged(repo: str, pr_number: int, hours_open: float):
    _incr(f"analytics:{repo}:prs_merged:{_today()}")
    _lpush(f"analytics:{repo}:pr_merge_hours:{_week()}", hours_open)


def record_issue_closed(repo: str, issue_number: int, hours_open: float):
    _incr(f"analytics:{repo}:issues_closed:{_today()}")
    _lpush(f"analytics:{repo}:issue_hours:{_week()}", hours_open)


def record_command_used(repo: str, command: str):
    cmd = command.lstrip("/")
    _incr(f"analytics:{repo}:cmd:{cmd}:{_today()}")
    _incr(f"analytics:{repo}:cmd_total:{cmd}")


def record_review_score(repo: str, score: float):
    _lpush(f"analytics:{repo}:review_scores:{_week()}", score)


def record_bot_action(repo: str, action: str):
    _incr(f"analytics:{repo}:actions:{action}:{_today()}")


def get_weekly_report(repo: str) -> dict:
    week  = _week()
    today = _today()

    merge_hours = _get_list(f"analytics:{repo}:pr_merge_hours:{week}")
    issue_hours = _get_list(f"analytics:{repo}:issue_hours:{week}")
    scores      = _get_list(f"analytics:{repo}:review_scores:{week}")
    avg_merge   = _avg(merge_hours)
    avg_issue   = _avg(issue_hours)
    avg_score   = _avg(scores)

    return {
        "repo":         repo,
        "week":         week,
        "prs": {
            "merged_today":    _get_int(f"analytics:{repo}:prs_merged:{today}"),
            "avg_merge_hours": round(avg_merge, 1),
            "avg_merge_days":  round(avg_merge / 24, 1) if avg_merge else 0,
        },
        "issues": {
            "closed_today":    _get_int(f"analytics:{repo}:issues_closed:{today}"),
            "avg_close_hours": round(avg_issue, 1),
        },
        "code_quality": {
            "avg_review_score":  round(avg_score, 1),
            "reviews_this_week": len(scores),
            "grade":             _score_to_grade(avg_score),
        },
        "bot_usage": {
            "top_commands": _get_top_commands(repo),
            "total_actions": _get_total_actions(repo, today),
        },
    }


def format_report_comment(repo: str) -> str:
    d    = get_weekly_report(repo)
    prs  = d["prs"]
    iss  = d["issues"]
    qual = d["code_quality"]
    bot  = d["bot_usage"]

    grade   = qual["grade"]
    g_emoji = {"A": "🟢", "B": "🟡", "C": "🟠", "D": "🔴", "F": "🔴"}.get(grade, "⚪")

    merge_disp = f"{prs['avg_merge_hours']}h" if prs["avg_merge_hours"] < 24 else f"{prs['avg_merge_days']}d"
    close_disp = f"{iss['avg_close_hours']}h" if iss["avg_close_hours"] < 24 else f"{round(iss['avg_close_hours']/24,1)}d"

    cmd_rows = "\n".join(
        f"| `/{cmd}` | {cnt} |"
        for cmd, cnt in (bot["top_commands"] or {}).items()
    ) or "| — | — |"

    return f"""## 📊 Weekly Repo Report — `{repo}`
*Week of {d['week']}*

### 🔀 Pull Requests
| Metric | Value |
|--------|-------|
| **Merged Today** | {prs['merged_today']} |
| **Avg Merge Time** | {merge_disp} |

### 🐛 Issues
| Metric | Value |
|--------|-------|
| **Closed Today** | {iss['closed_today']} |
| **Avg Resolution** | {close_disp} |

### 🔍 Code Quality
| Metric | Value |
|--------|-------|
| **Avg Review Score** | {qual['avg_review_score']}/10 |
| **Grade** | {g_emoji} {grade} |
| **Reviews This Week** | {qual['reviews_this_week']} |

### 🤖 Bot Usage
| Command | Uses |
|---------|------|
{cmd_rows}

**Total actions:** {bot['total_actions']}

---
*🤖 AI Repo Manager V4 — Use `/report` anytime for fresh stats*"""


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _week() -> str:
    now  = datetime.now(timezone.utc)
    week = now - timedelta(days=now.weekday())
    return week.strftime("%Y-W%V")


def _incr(key: str, ttl: int = 604800):
    try:
        r = redis_client.get_redis()
        r.incr(key)
        r.expire(key, ttl)
    except Exception:
        pass


def _lpush(key: str, value, ttl: int = 604800):
    try:
        r = redis_client.get_redis()
        r.lpush(key, str(value))
        r.ltrim(key, 0, 999)
        r.expire(key, ttl)
    except Exception:
        pass


def _get_int(key: str) -> int:
    try:
        val = redis_client.get_redis().get(key)
        return int(val) if val else 0
    except Exception:
        return 0


def _get_list(key: str) -> list:
    try:
        vals = redis_client.get_redis().lrange(key, 0, -1)
        return [float(v) for v in vals if v]
    except Exception:
        return []


def _avg(values: list) -> float:
    return sum(values) / len(values) if values else 0.0


def _score_to_grade(score: float) -> str:
    if score >= 9:
        return "A"
    if score >= 8:
        return "B"
    if score >= 7:
        return "C"
    if score >= 5:
        return "D"
    return "F"


def _get_top_commands(repo: str, top_n: int = 5) -> dict:
    commands = ["fix", "explain", "improve", "test", "review",
                "gaps", "ci", "security", "rollback", "autofix",
                "report", "budget", "changelog", "refactor", "docs"]
    counts = {}
    try:
        r = redis_client.get_redis()
        for cmd in commands:
            val = r.get(f"analytics:{repo}:cmd_total:{cmd}")
            if val and int(val) > 0:
                counts[cmd] = int(val)
    except Exception:
        pass
    return dict(sorted(counts.items(), key=lambda x: x[1], reverse=True)[:top_n])


def _get_total_actions(repo: str, today: str) -> int:
    actions = ["comment_posted", "pr_analyzed", "issue_triaged",
               "autofix_created", "label_applied"]
    return sum(_get_int(f"analytics:{repo}:actions:{a}:{today}") for a in actions)
