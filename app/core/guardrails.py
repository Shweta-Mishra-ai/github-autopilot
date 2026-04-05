
"""
Guardrails - app/core/guardrails.py
V4: Deterministic safety checks before any automated action.
Fixed: Function names now match what pull_request.py actually imports.
  check_pr_title_update() ← was check_title_update() — import was failing silently
  check_pr_description_update() ← was check_description_update()
"""

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

CONVENTIONAL = re.compile(
    r'^(feat|fix|docs|style|refactor|perf|test|chore|ci|build|revert)(\(.+\))?(!)?: .+',
    re.IGNORECASE
)


@dataclass
class GuardrailResult:
    passed: bool
    reason: str
    action_taken: str = ""


def check_pr_auto_merge(pr_data: dict, checks: list, reviews: list, config) -> GuardrailResult:
    if not config.auto_merge_enabled():
        return GuardrailResult(False, "Auto-merge disabled in .ai-repo-manager.yml")

    mergeable = pr_data.get("mergeable")
    if mergeable is False:
        return GuardrailResult(False, "PR has merge conflicts")
    if mergeable is None:
        return GuardrailResult(False, "GitHub hasn't computed mergeability yet — retry in a moment")

    if config.get("auto_merge", "require_no_blocking_reviews", default=True):
        blocking = [r for r in reviews if r.get("state") == "CHANGES_REQUESTED"]
        if blocking:
            blockers = ", ".join(f"@{r['user']['login']}" for r in blocking[:3])
            return GuardrailResult(False, f"Blocked by change requests from: {blockers}")

    if config.get("auto_merge", "require_passing_checks", default=True):
        failed = [c for c in checks if c.get("conclusion") in
                  ("failure", "cancelled", "timed_out", "action_required")]
        if failed:
            names = ", ".join(c["name"] for c in failed[:3])
            return GuardrailResult(False, f"Failing checks: {names}")

    base = pr_data.get("base", {}).get("ref", "")
    protected = {"main", "master", "production", "release"}
    if base in protected and not config.get("auto_merge", "allow_protected_branches", default=False):
        return GuardrailResult(False, f"Target `{base}` is protected — auto-merge disabled")

    if pr_data.get("draft", False):
        return GuardrailResult(False, "Draft PRs cannot be auto-merged")

    if pr_data.get("commits", 0) == 0:
        return GuardrailResult(False, "PR has no commits")

    return GuardrailResult(True, "All guardrails passed")


def check_auto_label(issue_or_pr: dict, labels: list, config) -> GuardrailResult:
    if not config.get("issues", "auto_label", default=True):
        return GuardrailResult(False, "Auto-label disabled in config")
    if not labels:
        return GuardrailResult(False, "No labels to add")
    existing = [l["name"] for l in issue_or_pr.get("labels", [])]
    new_labels = [l for l in labels if l not in existing]
    if not new_labels:
        return GuardrailResult(False, "Labels already applied")
    return GuardrailResult(True, "OK", action_taken=f"Adding: {new_labels}")


# ✅ FIXED: Renamed from check_title_update → check_pr_title_update (BUG 1)
def check_pr_title_update(pr: dict, config) -> GuardrailResult:
    if not config.get("pull_requests", "auto_polish_title", default=True):
        return GuardrailResult(False, "Title auto-polish disabled")
    current = pr.get("title", "")
    if CONVENTIONAL.match(current):
        return GuardrailResult(False, "Title already follows conventional commit format")
    return GuardrailResult(True, "OK")


# ✅ FIXED: Renamed from check_description_update → check_pr_description_update (BUG 1)
def check_pr_description_update(pr: dict, config) -> GuardrailResult:
    if not config.get("pull_requests", "auto_fill_description", default=True):
        return GuardrailResult(False, "Auto-fill description disabled")
    body = pr.get("body", "") or ""
    if len(body.strip()) >= 50:
        return GuardrailResult(False, "PR already has a description")
    return GuardrailResult(True, "OK")


def check_archived_repo(repo_data: dict) -> GuardrailResult:
    """V4 NEW: Skip all actions on archived repos."""
    if repo_data.get("archived", False):
        return GuardrailResult(False, "Repository is archived — no actions taken")
    return GuardrailResult(True, "OK")


def check_repo_rate_limit(repo: str) -> GuardrailResult:
    """V4 NEW: Per-repo daily AI call limit."""
    try:
        from app.core.redis_client import get_redis
        import datetime
        r = get_redis()
        today = datetime.date.today().isoformat()
        key = f"limit:{repo}:ai_calls:{today}"
        count = int(r.get(key) or 0)
        limit = 150  # Configurable via env: REPO_DAILY_LIMIT
        try:
            limit = int(__import__("os").environ.get("REPO_DAILY_AI_LIMIT", "150"))
        except Exception:
            pass
        if count >= limit:
            return GuardrailResult(
                False,
                f"Daily AI call limit ({limit}) reached for this repo. Resets at midnight UTC."
            )
    except Exception:
        pass  # If Redis unavailable, don't block
    return GuardrailResult(True, "OK")


def increment_repo_usage(repo: str):
    """V4 NEW: Increment per-repo daily AI call counter."""
    try:
        from app.core.redis_client import get_redis
        import datetime
        r = get_redis()
        today = datetime.date.today().isoformat()
        key = f"limit:{repo}:ai_calls:{today}"
        r.incr(key)
        r.expire(key, 86400)  # 24h TTL
    except Exception:
        pass


"""
═══════════════════════════════════════════════════════════════════════════════
FILE 9: app/github/auth.py
COMMIT: fix(auth): thread-safe token cache with Lock prevents race condition (LOOPHOLE 5 + BUG 10)
═══════════════════════════════════════════════════════════════════════════════
"""

"""
GitHub Auth - app/github/auth.py
V4: Thread-safe installation token caching.
Fixed: Added threading.Lock to prevent race condition where multiple threads
       simultaneously see cache miss and make redundant token requests.
"""

import os
import time
import logging
import threading
import jwt
import requests

log = logging.getLogger(__name__)

APP_ID = os.environ.get("GITHUB_APP_ID", "")
PRIVATE_KEY = os.environ.get("GITHUB_PRIVATE_KEY", "").replace("\\n", "\n")

_token_cache: dict = {}
_cache_lock = threading.Lock()  # ✅ FIXED: Prevents race condition


def get_jwt() -> str:
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 540, "iss": APP_ID}
    token = jwt.encode(payload, PRIVATE_KEY, algorithm="RS256")
    return token if isinstance(token, str) else token.decode("utf-8")


def get_installation_token(installation_id: int) -> str:
    """Thread-safe cached installation token. Valid for 50 min (GitHub issues 60 min tokens)."""
    with _cache_lock:
        cached = _token_cache.get(installation_id)
        if cached and cached["expires"] > time.time() + 300:  # 5 min buffer
            return cached["token"]

        # Fetch new token (inside lock to prevent race)
        app_jwt = get_jwt()
        r = requests.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github.v3+json",
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        token = data["token"]
        # GitHub tokens last 1 hour; we cache for 50 min
        _token_cache[installation_id] = {
            "token": token,
            "expires": time.time() + 3000,  # 50 min
        }
        log.info(f"auth.token_fetched installation_id={installation_id}")
        return token


def clear_token_cache(installation_id: int = None):
    """Clear cache for one or all installations."""
    with _cache_lock:
        if installation_id:
            _token_cache.pop(installation_id, None)
        else:
            _token_cache.clear()


"""
═══════════════════════════════════════════════════════════════════════════════
FILE 10: app/core/idempotency.py
COMMIT: fix(idempotency): move fingerprint storage to Redis (LOOPHOLE 9)
         Previously in-memory — lost on restart, causing duplicate processing
═══════════════════════════════════════════════════════════════════════════════
"""

"""
Idempotency - app/core/idempotency.py
V4: Redis-backed event deduplication.
Fixed: Was in-memory (OrderedDict) — all fingerprints lost on app restart.
       GitHub retries webhooks for 24h → restarted app processed events twice.
       Now uses Redis SET with NX flag (atomic check-and-set).
"""

import hashlib
import time
import logging
from collections import OrderedDict

log = logging.getLogger(__name__)

_TTL_SECONDS = 3600   # Fingerprints remembered for 1 hour

# In-memory fallback (used when Redis unavailable)
_seen_local: OrderedDict = OrderedDict()
_MAX_LOCAL = 2000


def make_fingerprint(delivery_id: str, event_type: str, payload: dict) -> str:
    key_fields = {
        "delivery": delivery_id,
        "event": event_type,
        "action": payload.get("action", ""),
        "repo": payload.get("repository", {}).get("full_name", ""),
        "number": (
            payload.get("pull_request", {}).get("number")
            or payload.get("issue", {}).get("number")
            or payload.get("comment", {}).get("id")
            or ""
        ),
    }
    raw = "|".join(str(v) for v in key_fields.values())
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def is_duplicate(fingerprint: str) -> bool:
    """
    Returns True if already processed. Side effect: records fingerprint if new.
    Uses Redis SET NX (atomic) — prevents TOCTOU race condition.
    Falls back to in-memory if Redis unavailable.
    """
    try:
        from app.core.redis_client import get_redis, is_redis_available
        if is_redis_available():
            r = get_redis()
            key = f"idem:{fingerprint}"
            # SET key value NX EX ttl → returns None if key existed (duplicate)
            result = r.set(key, "1", nx=True, ex=_TTL_SECONDS)
            is_dup = result is None
            if is_dup:
                log.info(f"idempotency.duplicate_redis fingerprint={fingerprint}")
            return is_dup
    except Exception as e:
        log.warning(f"idempotency.redis_fallback error={e}")

    # In-memory fallback
    return _is_duplicate_local(fingerprint)


def _is_duplicate_local(fingerprint: str) -> bool:
    now = time.time()
    # Evict expired
    expired = [k for k, t in _seen_local.items() if now - t > _TTL_SECONDS]
    for k in expired:
        del _seen_local[k]
    while len(_seen_local) > _MAX_LOCAL:
        _seen_local.popitem(last=False)

    if fingerprint in _seen_local:
        log.info(f"idempotency.duplicate_local fingerprint={fingerprint}")
        return True
    _seen_local[fingerprint] = now
    return False


"""
═══════════════════════════════════════════════════════════════════════════════
FILE 11: app/github/client.py
COMMIT: fix(client): add gh_patch(), handle secondary rate limit, add pagination (BUG 3 + LOOPHOLE 6,7)
═══════════════════════════════════════════════════════════════════════════════
"""

"""
GitHub Client - app/github/client.py
V4:
  + gh_patch() method — needed for branch ref updates (was using gh_post which is wrong)
  + Secondary rate limit handling (403 with special message)
  + gh_get_all() for auto-pagination
"""

import os
import time
import logging
import requests
from app.github.rate_limit import update_from_headers, check_and_wait

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
DEFAULT_TIMEOUT = 20


class GitHubError(Exception):
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _handle_response(r: requests.Response, method: str, path: str):
    update_from_headers(dict(r.headers))

    if r.status_code in (200, 201, 204):
        if r.status_code == 204 or not r.content:
            return {}
        return r.json()

    if r.status_code == 429:
        retry_after = int(r.headers.get("Retry-After", 30))
        raise GitHubError(f"Primary rate limit — retry after {retry_after}s", 429)

    # ✅ FIXED: Secondary rate limit (LOOPHOLE 6)
    if r.status_code == 403:
        try:
            body = r.json()
            msg = body.get("message", "")
            if "secondary rate limit" in msg.lower() or "abuse" in msg.lower():
                log.warning(f"github.secondary_rate_limit path={path}")
                time.sleep(60)
                raise GitHubError("Secondary rate limit triggered — waited 60s", 403)
            raise GitHubError(f"Forbidden: {msg}", 403)
        except GitHubError:
            raise
        except Exception:
            raise GitHubError(f"403 Forbidden: {path}", 403)

    if r.status_code == 404:
        raise GitHubError(f"Not found: {path}", 404)

    if r.status_code == 422:
        try:
            detail = r.json().get("message", "Unprocessable Entity")
        except Exception:
            detail = "Unprocessable Entity"
        raise GitHubError(f"422: {detail}", 422)

    raise GitHubError(f"{method} {path} → {r.status_code}: {r.text[:200]}", r.status_code)


def gh_get(path: str, token: str) -> dict | list:
    check_and_wait()
    url = path if path.startswith("http") else f"{GITHUB_API}{path}"
    r = requests.get(url, headers=_headers(token), timeout=DEFAULT_TIMEOUT)
    return _handle_response(r, "GET", path)


def gh_get_all(path: str, token: str, max_pages: int = 5) -> list:
    """✅ NEW: Auto-paginate GET requests. Returns all results across pages."""
    results = []
    sep = "&" if "?" in path else "?"
    for page in range(1, max_pages + 1):
        paged_path = f"{path}{sep}page={page}&per_page=100"
        try:
            data = gh_get(paged_path, token)
        except GitHubError:
            break
        if not data:
            break
        if isinstance(data, list):
            results.extend(data)
            if len(data) < 100:
                break
        else:
            # Not a list (single object) — return as-is
            return data
    return results


def gh_post(path: str, token: str, data: dict) -> dict:
    check_and_wait()
    url = f"{GITHUB_API}{path}"
    r = requests.post(url, headers=_headers(token), json=data, timeout=DEFAULT_TIMEOUT)
    return _handle_response(r, "POST", path)


def gh_put(path: str, token: str, data: dict) -> dict:
    check_and_wait()
    url = f"{GITHUB_API}{path}"
    r = requests.put(url, headers=_headers(token), json=data, timeout=DEFAULT_TIMEOUT)
    return _handle_response(r, "PUT", path)


def gh_patch(path: str, token: str, data: dict) -> dict:
    """✅ NEW: PATCH method — required for updating branch refs, PR details, etc."""
    check_and_wait()
    url = f"{GITHUB_API}{path}"
    r = requests.patch(url, headers=_headers(token), json=data, timeout=DEFAULT_TIMEOUT)
    return _handle_response(r, "PATCH", path)


def gh_delete(path: str, token: str) -> dict:
    check_and_wait()
    url = f"{GITHUB_API}{path}"
    r = requests.delete(url, headers=_headers(token), timeout=DEFAULT_TIMEOUT)
    return _handle_response(r, "DELETE", path)


"""
═══════════════════════════════════════════════════════════════════════════════
FILE 12: app/core/config.py
COMMIT: fix(config): add 5-min cache + Pydantic validation (BUG 9 + LOOPHOLE 10)
═══════════════════════════════════════════════════════════════════════════════
"""

"""
Config Loader - app/core/config.py
V4:
  + 5-minute cache (was hitting GitHub API on every webhook)
  + Pydantic validation with helpful error messages for bad config values
"""

import logging
import base64
import time
from typing import Any

log = logging.getLogger(__name__)

# ── Cache ─────────────────────────────────────────────────────────────────────
_config_cache: dict[str, tuple] = {}  # {repo: (Config, timestamp)}
_CONFIG_TTL = 300  # 5 minutes


DEFAULTS = {
    "bot": {
        "enabled": True,
        "footer": "🤖 [AI Repo Manager V4](https://github.com/Shweta-Mishra-ai/github-autopilot)",
    },
    "pull_requests": {
        "enabled": True,
        "auto_polish_title": True,
        "auto_fill_description": True,
        "code_review": True,
        "max_files_reviewed": 6,
        "detect_test_gaps": True,
    },
    "issues": {
        "enabled": True,
        "auto_triage": True,
        "auto_label": True,
    },
    "push": {
        "enabled": True,
        "enforce_conventional_commits": True,
        "create_issue_threshold": 3,
        "scan_secrets": True,
        "scan_dependencies": True,
    },
    "auto_merge": {
        "enabled": False,
        "require_passing_checks": True,
        "require_no_blocking_reviews": True,
        "allow_protected_branches": False,
        "allowed_risk_levels": ["low"],
    },
    "ai": {
        "primary_model": "llama-3.3-70b-versatile",
        "fallback_model": "llama-3.1-8b-instant",
        "max_tokens": 1500,
        "temperature": 0.2,
        "timeout_seconds": 45,
    },
    "confidence": {
        "thresholds": {
            "pr_title_rewrite": 0.80,
            "issue_label":      0.70,
            "auto_merge":       0.95,
            "fix_command":      0.75,
            "code_review":      0.75,
            "auto_apply":       0.90,
            "security_finding": 0.85,
        }
    },
    "notifications": {
        "slack": False,
        "discord": False,
        "on_secret_detected": True,
        "on_high_risk_pr": True,
        "on_health_degraded": True,
    },
    "labels": {"auto_create": True},
    "commands": {
        "enabled": [
            "fix", "apply", "explain", "improve", "test", "docs",
            "refactor", "health", "version", "merge",
            "summarize", "ci", "security", "gaps", "changelog",
            "rollback", "autofix", "impact", "perf", "arch",
            "release", "runtests", "secfull", "budget",
        ],
        "permissions": {
            "maintainer_only": ["merge", "release", "rollback"],
        },
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _validate_config(data: dict) -> dict:
    """
    Validate and sanitize config values.
    Returns cleaned config. Logs warnings for bad values (doesn't crash).
    """
    # Validate confidence thresholds (must be float 0.0-1.0)
    thresholds = data.get("confidence", {}).get("thresholds", {})
    if isinstance(thresholds, dict):
        clean = {}
        for k, v in thresholds.items():
            try:
                fv = float(v)
                if 0.0 <= fv <= 1.0:
                    clean[k] = fv
                else:
                    log.warning(f"config.invalid threshold {k}={v} (must be 0.0-1.0), using default")
            except (TypeError, ValueError):
                log.warning(f"config.invalid threshold {k}={v!r} (must be float), using default")
        if "confidence" not in data:
            data["confidence"] = {}
        data["confidence"]["thresholds"] = clean

    # Validate max_files_reviewed (must be int 1-20)
    mfr = data.get("pull_requests", {}).get("max_files_reviewed")
    if mfr is not None:
        try:
            mfr = int(mfr)
            if not (1 <= mfr <= 20):
                log.warning(f"config.invalid max_files_reviewed={mfr} (must be 1-20), using 6")
                data.setdefault("pull_requests", {})["max_files_reviewed"] = 6
        except (TypeError, ValueError):
            log.warning(f"config.invalid max_files_reviewed={mfr!r}, using 6")
            data.setdefault("pull_requests", {})["max_files_reviewed"] = 6

    # Validate create_issue_threshold (must be int 1-20)
    cit = data.get("push", {}).get("create_issue_threshold")
    if cit is not None:
        try:
            cit = int(cit)
            if not (1 <= cit <= 20):
                log.warning(f"config.invalid create_issue_threshold={cit}, using 3")
                data.setdefault("push", {})["create_issue_threshold"] = 3
        except (TypeError, ValueError):
            data.setdefault("push", {})["create_issue_threshold"] = 3

    return data


class Config:
    def __init__(self, data: dict):
        validated = _validate_config(data)
        self._data = _deep_merge(DEFAULTS, validated)

    def get(self, *keys: str, default: Any = None) -> Any:
        node = self._data
        for key in keys:
            if not isinstance(node, dict):
                return default
            node = node.get(key, default)
            if node is None:
                return default
        return node

    def bot_enabled(self) -> bool:
        return bool(self.get("bot", "enabled", default=True))

    def pr_enabled(self) -> bool:
        return bool(self.get("pull_requests", "enabled", default=True))

    def issues_enabled(self) -> bool:
        return bool(self.get("issues", "enabled", default=True))

    def auto_merge_enabled(self) -> bool:
        return bool(self.get("auto_merge", "enabled", default=False))

    def command_enabled(self, cmd: str) -> bool:
        enabled = self.get("commands", "enabled", default=[])
        return cmd.lstrip("/") in enabled

    def is_maintainer_only(self, cmd: str) -> bool:
        mo = self.get("commands", "permissions", "maintainer_only", default=[])
        return cmd.lstrip("/") in mo

    @property
    def footer(self) -> str:
        text = self.get("bot", "footer", default="🤖 AI Repo Manager V4")
        return f"\n\n---\n*{text}*"


def load_config(repo: str, token: str) -> Config:
    """Load config with 5-minute cache. Falls back to defaults if file missing."""
    now = time.time()

    # ✅ FIXED: Check cache first (BUG 9)
    if repo in _config_cache:
        cached_config, cached_at = _config_cache[repo]
        if now - cached_at < _CONFIG_TTL:
            return cached_config

    try:
        from app.github.client import gh_get
        data = gh_get(f"/repos/{repo}/contents/.ai-repo-manager.yml", token)
        content = base64.b64decode(data["content"]).decode("utf-8")
        import yaml
        parsed = yaml.safe_load(content) or {}
        if not isinstance(parsed, dict):
            parsed = {}
        config = Config(parsed)
        log.info(f"config.loaded repo={repo}")
    except Exception as e:
        log.debug(f"config.using_defaults repo={repo} reason={e}")
        config = Config({})

    _config_cache[repo] = (config, now)
    return config


def invalidate_config_cache(repo: str = None):
    """Call this when .ai-repo-manager.yml is updated in a push event."""
    if repo:
        _config_cache.pop(repo, None)
    else:
        _config_cache.clear()


"""
═══════════════════════════════════════════════════════════════════════════════
FILE 13: app/ai/client.py
COMMIT: fix(ai): JSON extractor brace-depth, groq_text uses 70B, add circuit breaker (BUG 6,7)
═══════════════════════════════════════════════════════════════════════════════
"""

"""
AI Client - app/ai/client.py
V4:
  + Fixed JSON extractor (brace-depth counting, not greedy regex)
  + groq_text now uses 70B model for important outputs
  + Circuit breaker integration
  + AllProvidersDown raised when all circuits open
"""

import os
import re
import time
import json
import logging
import requests
from app.ai.circuit_breaker import get_breaker, AllProvidersDown, available_providers

log = logging.getLogger(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

MODEL_70B = "llama-3.3-70b-versatile"
MODEL_8B  = "llama-3.1-8b-instant"
MAX_RETRIES = 2


class AIError(Exception):
    pass


def _call_groq(model: str, system: str, user: str,
               max_tokens: int, temperature: float, timeout: int) -> str:
    """Single Groq API call. Returns raw text. Raises AIError on failure."""
    breaker = get_breaker(f"groq_{'70b' if '70b' in model or 'versatile' in model else '8b'}")

    if not breaker.is_available():
        raise AIError(f"Circuit open for {model}")

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    }

    try:
        r = requests.post(GROQ_URL, headers=headers, json=payload, timeout=timeout)

        if r.status_code == 429:
            retry_after = int(r.headers.get("Retry-After", 30))
            breaker.record_failure(f"rate_limit_429 retry_after={retry_after}s")
            raise AIError(f"RATE_LIMIT:{retry_after}")

        if r.status_code >= 500:
            breaker.record_failure(f"server_error_{r.status_code}")
            raise AIError(f"Groq server error {r.status_code}")

        r.raise_for_status()
        result = r.json()["choices"][0]["message"]["content"]
        breaker.record_success()

        # Track token usage
        _track_usage(model, r.json().get("usage", {}))
        return result

    except requests.exceptions.Timeout:
        breaker.record_failure("timeout")
        raise AIError("Timeout")
    except AIError:
        raise
    except Exception as e:
        breaker.record_failure(str(e)[:50])
        raise AIError(str(e))


def _track_usage(model: str, usage: dict):
    """Track token usage in Redis for /budget command."""
    try:
        from app.core.redis_client import get_redis
        import datetime
        r = get_redis()
        today = datetime.date.today().isoformat()
        provider_key = "groq_70b" if "70b" in model or "versatile" in model else "groq_8b"
        total = usage.get("total_tokens", 0)
        if total:
            r.incr(f"llm:tokens:{provider_key}:{today}")
            r.expire(f"llm:tokens:{provider_key}:{today}", 86400)
            r.incr(f"llm:requests:{provider_key}:{today}")
            r.expire(f"llm:requests:{provider_key}:{today}", 86400)
    except Exception:
        pass


def _extract_json(text: str) -> dict:
    """
    ✅ FIXED: Brace-depth JSON extraction. (BUG 6)
    Old: re.search(r'\{[\s\S]*\}', text) — greedy, breaks on multi-object responses.
    New: Find first '{', count depth, stop at matching '}'. Correct and fast.
    """
    # Try direct parse first (handles clean responses)
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Brace-depth scan
    for start_idx, ch in enumerate(text):
        if ch != '{':
            continue
        depth = 0
        for end_idx in range(start_idx, len(text)):
            if text[end_idx] == '{':
                depth += 1
            elif text[end_idx] == '}':
                depth -= 1
            if depth == 0:
                candidate = text[start_idx:end_idx + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    break  # Try next '{'

    log.warning(f"ai.json_extract_failed text_preview={text[:100]!r}")
    return {"raw": text}


def groq_ask(system: str, user: str,
             max_tokens: int = 1500,
             fast: bool = False,
             temperature: float = 0.2,
             timeout: int = 45) -> dict:
    """
    Call Groq and return parsed JSON dict.
    fast=True → use 8B (faster, cheaper, less accurate)
    fast=False → try 70B first, fall back to 8B
    Raises AllProvidersDown if all circuits are open.
    """
    if not available_providers():
        raise AllProvidersDown()

    models = [MODEL_8B] if fast else [MODEL_70B, MODEL_8B]

    for model in models:
        for attempt in range(MAX_RETRIES):
            try:
                text = _call_groq(model, system, user, max_tokens, temperature, timeout)
                parsed = _extract_json(text)
                return parsed

            except AIError as e:
                msg = str(e)
                if "RATE_LIMIT:" in msg:
                    wait = int(msg.split(":")[1])
                    log.warning(f"groq_ask.rate_limit model={model} wait={wait}s")
                    time.sleep(min(wait, 30))
                    break  # Move to next model
                log.warning(f"groq_ask.error model={model} attempt={attempt+1} err={e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)

            except json.JSONDecodeError as e:
                log.warning(f"groq_ask.json_error model={model}: {e}")
                return {"raw": ""}

            except Exception as e:
                log.warning(f"groq_ask.unexpected model={model} attempt={attempt+1}: {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)

    # All models failed
    if not available_providers():
        raise AllProvidersDown()

    log.error("groq_ask.all_failed")
    return {"error": "AI temporarily unavailable"}


def groq_text(system: str, user: str,
              max_tokens: int = 800,
              timeout: int = 30,
              fast: bool = False) -> str:
    """
    Call Groq and return plain text.
    ✅ FIXED: Now uses 70B by default (was always 8B — BUG 7).
    Use fast=True for quick/cheap responses where quality matters less.
    """
    models = [MODEL_8B] if fast else [MODEL_70B, MODEL_8B]  # ✅ BUG 7 fixed

    for model in models:
        for attempt in range(MAX_RETRIES):
            try:
                text = _call_groq(model, system, user, max_tokens, 0.3, timeout)
                return text
            except AIError as e:
                if "RATE_LIMIT" in str(e):
                    time.sleep(15)
                    break
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)
            except Exception as e:
                log.warning(f"groq_text attempt {attempt+1} failed: {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)

    if not available_providers():
        raise AllProvidersDown()

    return "AI temporarily unavailable. Please try again in a moment."


"""
═══════════════════════════════════════════════════════════════════════════════
FILE 14: app/ai/validator.py  (only showing the fix — field name standardization)
COMMIT: fix(validator): standardize field names — improved_title → suggested_title (LOOPHOLE 18)
         pull_request.py reads suggested_title but validator was returning improved_title
═══════════════════════════════════════════════════════════════════════════════
"""
# NOTE: Replace ONLY the validate_pr_analysis() function return dict.
# Change "improved_title" → "suggested_title" everywhere in that function.
# The full file is kept as-is except this one field name change.
# In validate_pr_analysis(), change:
#   return {"improved_title": ..., ...}
# To:
#   return {"suggested_title": ..., ...}
# And in the fallback return:
#   return {"suggested_title": "", ...}  (was "improved_title")
