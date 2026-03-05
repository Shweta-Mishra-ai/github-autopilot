"""
Config Loader - app/core/config.py
Loads .ai-repo-manager.yml from repo root.
Falls back to safe defaults if file missing or malformed.
"""

import logging
import base64
from typing import Any

log = logging.getLogger(__name__)

# ── Safe defaults ────────────────────────────────────────────────────────────
DEFAULTS = {
    "bot": {
        "enabled": True,
        "footer": "🤖 [AI Repo Manager](https://github.com/apps/ai-repo-manager) — AI-powered repo management",
    },
    "pull_requests": {
        "enabled": True,
        "auto_polish_title": True,
        "auto_fill_description": True,
        "code_review": True,
        "max_files_reviewed": 4,
    },
    "issues": {
        "enabled": True,
        "auto_triage": True,
        "auto_label": True,
    },
    "push": {
        "enabled": True,
        "enforce_conventional_commits": True,
        "create_issue_threshold": 3,   # only create issue if >= N bad commits
    },
    "auto_merge": {
        "enabled": False,              # OFF by default — opt-in only
        "require_passing_checks": True,
        "require_no_blocking_reviews": True,
        "allowed_risk_levels": ["low"],
    },
    "ai": {
        "primary_model": "llama-3.3-70b-versatile",
        "fallback_model": "llama-3.1-8b-instant",
        "max_tokens": 1500,
        "temperature": 0.2,
        "timeout_seconds": 45,
    },
    "labels": {
        "auto_create": True,
    },
    "commands": {
        "enabled": ["fix", "explain", "improve", "test", "docs",
                    "refactor", "health", "version", "merge"],
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. Base keys not in override are kept."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class Config:
    """
    Repo-level config loaded from .ai-repo-manager.yml.
    Always falls back to DEFAULTS — never crashes on bad config.
    """

    def __init__(self, data: dict):
        self._data = _deep_merge(DEFAULTS, data)

    def get(self, *keys: str, default: Any = None) -> Any:
        """Safe nested key access. config.get('auto_merge', 'enabled')"""
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

    def auto_merge_risk_ok(self, risk: str) -> bool:
        allowed = self.get("auto_merge", "allowed_risk_levels", default=["low"])
        return risk in allowed

    def command_enabled(self, cmd: str) -> bool:
        enabled = self.get("commands", "enabled", default=[])
        return cmd.lstrip("/") in enabled

    @property
    def footer(self) -> str:
        text = self.get("bot", "footer", default="🤖 AI Repo Manager")
        return f"\n\n---\n*{text}*"


def load_config(repo: str, token: str) -> Config:
    """
    Fetch .ai-repo-manager.yml from repo root.
    Returns Config with defaults if file not found or invalid.
    """
    try:
        from app.github.client import gh_get
        data = gh_get(f"/repos/{repo}/contents/.ai-repo-manager.yml", token)
        content = base64.b64decode(data["content"]).decode("utf-8")

        import yaml
        parsed = yaml.safe_load(content) or {}
        if not isinstance(parsed, dict):
            log.warning(f"[{repo}] .ai-repo-manager.yml is not a dict — using defaults")
            return Config({})

        log.info(f"[{repo}] Loaded .ai-repo-manager.yml")
        return Config(parsed)

    except Exception as e:
        # File not found OR invalid YAML — silent fallback to defaults
        log.debug(f"[{repo}] No config file found, using defaults: {e}")
        return Config({})
