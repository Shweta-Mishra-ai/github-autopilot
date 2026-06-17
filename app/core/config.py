"""
Config Loader - app/core/config.py
V4 changes:

FIXED (BUG 9): 5-minute cache.
  Old: load_config() called gh_get() on EVERY webhook event.
  Active repo, 50 events/day = 50 redundant GitHub API calls for same YAML.
  Fix: Cache with 5-min TTL. Same config reused. Rate limit preserved.

FIXED (LOOPHOLE 10): Config value validation.
  Old: Bad config value (e.g. threshold: "high" instead of 0.85) caused
       a crash deep in ConfidenceGate with a confusing traceback.
  Fix: Validate and sanitize values on load. Log warning. Fall back to default.

V4 NEW: Extended defaults for all new V4 commands and features.
"""

import base64
import logging
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

# ── Cache (5-minute TTL, thread-safe) ────────────────────────────────────────
_config_cache: dict[str, tuple] = {}  # {repo: (Config, timestamp)}
_config_lock  = threading.RLock()     # RLock: reentrant so invalidate can be called inside load
_CONFIG_TTL   = 300  # 5 minutes in seconds


# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULTS: dict = {
    "bot": {
        "enabled": True,
        "footer": (
            "🤖 [AI Repo Manager V4]"
            "(https://github.com/Shweta-Mishra-ai/github-autopilot)"
        ),
    },
    "pull_requests": {
        "enabled": True,
        "auto_polish_title": False,  # Changed: silent rewrite off by default. Enable in .ai-repo-manager.yml
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
            "pr_description": 0.75,
            "issue_label": 0.70,
            "auto_merge": 0.95,
            "fix_command": 0.75,
            "auto_apply": 0.92,
            "code_review": 0.75,
            "security_finding": 0.85,
            "issue_triage": 0.75,
        }
    },
    "notifications": {
        "slack": False,
        "discord": False,
        "on_secret_detected": True,
        "on_high_risk_pr": True,
        "on_health_degraded": True,
        "on_all_providers_down": True,
    },
    "labels": {
        "auto_create": True,
    },
    "commands": {
        "enabled": [
            # V2.1
            "fix",
            "apply",
            "explain",
            "improve",
            "test",
            "docs",
            "refactor",
            "health",
            "version",
            "merge",
            # V3
            "summarize",
            "ci",
            "security",
            "gaps",
            "changelog",
            # V4
            "rollback",
            "autofix",
            "impact",
            "perf",
            "arch",
            "release",
            "runtests",
            "secfull",
            "budget",
        ],
        "permissions": {
            "maintainer_only": ["merge", "release", "rollback"],
        },
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────


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
    Validate and sanitize user config values.
    Bad values → warning logged → default used. Never crashes.
    """
    # ── Confidence thresholds: must be float 0.0-1.0 ────────────────────────
    raw_thresholds = data.get("confidence", {}).get("thresholds", {})
    if isinstance(raw_thresholds, dict):
        clean: dict = {}
        for k, v in raw_thresholds.items():
            try:
                fv = float(v)
                if 0.0 <= fv <= 1.0:
                    clean[k] = fv
                else:
                    log.warning(
                        f"config.invalid_threshold key={k} value={v} "
                        f"(must be 0.0–1.0) — using default"
                    )
            except (TypeError, ValueError):
                log.warning(
                    f"config.invalid_threshold key={k} value={v!r} "
                    f"(must be a float) — using default"
                )
        data.setdefault("confidence", {})["thresholds"] = clean

    # ── max_files_reviewed: must be int 1-20 ────────────────────────────────
    mfr = data.get("pull_requests", {}).get("max_files_reviewed")
    if mfr is not None:
        try:
            mfr = int(mfr)
            if not (1 <= mfr <= 20):
                log.warning(
                    f"config.invalid max_files_reviewed={mfr} (must be 1–20) — using 6"
                )
                data.setdefault("pull_requests", {})["max_files_reviewed"] = 6
        except (TypeError, ValueError):
            log.warning(
                f"config.invalid max_files_reviewed={mfr!r} (must be int) — using 6"
            )
            data.setdefault("pull_requests", {})["max_files_reviewed"] = 6

    # ── create_issue_threshold: must be int 1-20 ────────────────────────────
    cit = data.get("push", {}).get("create_issue_threshold")
    if cit is not None:
        try:
            cit = int(cit)
            if not (1 <= cit <= 20):
                log.warning(
                    f"config.invalid create_issue_threshold={cit} (must be 1–20) — using 3"
                )
                data.setdefault("push", {})["create_issue_threshold"] = 3
        except (TypeError, ValueError):
            data.setdefault("push", {})["create_issue_threshold"] = 3

    return data


# ── Config class ──────────────────────────────────────────────────────────────


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

    # ── Convenience shortcuts ─────────────────────────────────────────────────

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


# ── Loader ────────────────────────────────────────────────────────────────────


def load_config(repo: str, token: str) -> Config:
    """
    Load .ai-repo-manager.yml from repo with 5-minute cache.
    Falls back to defaults if file missing or invalid.
    Thread-safe: uses RLock to prevent concurrent cache writes.
    """
    now = time.time()

    with _config_lock:
        if repo in _config_cache:
            cached_config, cached_at = _config_cache[repo]
            if now - cached_at < _CONFIG_TTL:
                return cached_config

    # Cache miss — fetch from GitHub (outside lock to avoid blocking other threads)
    try:
        from app.github.client import gh_get

        data    = gh_get(f"/repos/{repo}/contents/.ai-repo-manager.yml", token)
        content = base64.b64decode(data["content"]).decode("utf-8")

        import yaml

        parsed = yaml.safe_load(content) or {}
        if not isinstance(parsed, dict):
            log.warning(f"config.invalid_yaml repo={repo} — using defaults")
            parsed = {}

        config = Config(parsed)
        log.info(f"config.loaded repo={repo}")

    except Exception as e:
        log.debug(f"config.using_defaults repo={repo} reason={e}")
        config = Config({})

    with _config_lock:
        _config_cache[repo] = (config, now)
    return config


def invalidate_config_cache(repo: str = None):
    """
    Force-clear config cache.
    Call when .ai-repo-manager.yml is updated in a push event
    so next webhook picks up the new config immediately.
    """
    with _config_lock:
        if repo:
            _config_cache.pop(repo, None)
            log.debug(f"config.cache_invalidated repo={repo}")
        else:
            _config_cache.clear()
        log.debug("config.cache_invalidated all")
