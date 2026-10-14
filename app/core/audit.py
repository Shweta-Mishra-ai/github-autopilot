"""
app/core/audit.py
The record of irreversible actions taken by command: who merged what, when.

The /merge audit entry was written inside `except Exception: pass`, so when
Redis refused the write the merge went unrecorded and nothing said so. The
entry is now also written to the log, which is the record that survives when
Redis is down or is the in-memory fallback (which a restart empties), and a
failed Redis write is logged as an error and counted.
"""

from __future__ import annotations

import json
import logging
import time

log = logging.getLogger(__name__)

MERGE_KEY = "audit:merge"
MAX_ENTRIES = 1000


def record_merge(repo: str, pr_number: int, author: str, sha: str) -> bool:
    """Record a merge. True when it reached Redis. Never raises."""
    entry = {
        "repo": repo,
        "pr": pr_number,
        "by": author,
        "at": int(time.time()),
        "sha": (sha or "")[:12],
    }
    line = json.dumps(entry, separators=(",", ":"))
    log.info(f"audit.merge {line}")
    try:
        from app.core.redis_client import get_redis

        r = get_redis()
        r.lpush(MERGE_KEY, line)
        r.ltrim(MERGE_KEY, 0, MAX_ENTRIES - 1)
        return True
    except Exception as e:
        log.error(f"audit.merge_unrecorded {line}: {type(e).__name__}: {e}")
        try:
            from app.core.metrics import metrics

            metrics.increment("audit.merge_unrecorded")
        except Exception:  # nosec B110 - counting the failure must not raise either
            pass
        return False
