"""
app/core/learning.py
Sprint 7: AI Learning and Feedback Tracking System.
Records user acceptance rates and specific coding patterns per repository.
"""

import logging
import app.core.redis_client

log = logging.getLogger(__name__)

def record_fix_outcome(repo: str, action: str, accepted: bool):
    """Records whether an AI suggestion (like a fix or review) was accepted by the user."""
    try:
        r = app.core.redis_client.get_redis()

        # Increment action-specific counters in Redis
        if accepted:
            r.incr(f"learning:{repo}:{action}:accepted")
        else:
            r.incr(f"learning:{repo}:{action}:rejected")

    except Exception as e:
        log.error(f"Failed to record outcome for {repo}: {e}")

def get_acceptance_rate(repo: str, action: str = None) -> float:
    """
    Calculates the ratio of accepted AI suggestions (0.0 to 1.0).
    Returns 0.5 as a neutral default if no data exists.
    """
    try:
        r = app.core.redis_client.get_redis()
        action_str = action if action else "fix"

        # Fetch raw byte values from Redis
        accepted_val = r.get(f"learning:{repo}:{action_str}:accepted")
        rejected_val = r.get(f"learning:{repo}:{action_str}:rejected")

        # Convert to integers safely
        accepted = int(accepted_val) if accepted_val else 0
        rejected = int(rejected_val) if rejected_val else 0
        total = accepted + rejected

        if total == 0:
            return 0.5  # Neutral default when no feedback exists yet

        return float(accepted) / total

    except Exception as e:
        log.error(f"Failed to calculate acceptance rate for {repo}: {e}")
        return 0.5  # Fallback safely without crashing

def get_repo_patterns(repo: str) -> dict:
    """
    Retrieves learned coding patterns for this specific repo.
    Returns a dictionary mapping pattern names to their active status (bool).
    """
    try:
        r = app.core.redis_client.get_redis()
        key = f"learning:{repo}:patterns"

        # Retrieve all patterns stored in a Redis Hash
        raw_patterns = r.hgetall(key)

        if not raw_patterns:
            return {}

        # Redis returns bytes, so we must decode them into strings and booleans
        patterns_dict = {
            k.decode("utf-8"): v.decode("utf-8") == "True"
            for k, v in raw_patterns.items()
        }
        return patterns_dict

    except Exception as e:
        log.error(f"Failed to get repo patterns for {repo}: {e}")
        return {}

def record_pattern(repo: str, pattern: str, active: bool):
    """
    Records a new learned pattern or updates an existing one for a repository.
    """
    try:
        r = app.core.redis_client.get_redis()
        key = f"learning:{repo}:patterns"

        # Store pattern in a Redis Hash (pattern_name -> "True"/"False")
        r.hset(key, pattern, str(active))

    except Exception as e:
        log.error(f"Failed to record pattern for {repo}: {e}")
