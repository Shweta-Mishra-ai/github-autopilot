"""
app/handlers/pull_request/risk.py
A risk level computed from WHAT a PR touches, never from what a model says.

The recorded risk gates auto-merge (auto_merge.allowed_risk_levels, default
["low"]). It used to be the model's answer alone, and the model was often
shown no diff at all — a 16-file PR that granted a workflow `write-all`
permissions and replaced `verify(token)` with `pass` was recorded "low" and
passed every merge guardrail. The model may raise the level; it can never
lower it below this floor.

Pure functions, no I/O.
"""

from __future__ import annotations

import re

_ORDER = {"low": 0, "medium": 1, "high": 2}

# Paths whose change can grant access, run code in CI, or alter stored data.
_HIGH_PATH = re.compile(
    r"(^|/)\.github/(workflows|actions)/"
    r"|(^|/)(dockerfile|containerfile)[^/]*$"
    r"|(^|/)docker-compose[^/]*$"
    r"|(^|/)(migrations?|alembic|db/migrate)/"
    r"|\.sql$"
    r"|(^|/)codeowners$"
    r"|(^|[/_.-])(auth|authn|authz|authentication|authorization|security|crypto|"
    r"permissions?|secrets?|tokens?|passwords?|login|oauth|jwt|sessions?|acl|rbac|"
    r"webhook_security|sanitizer)([/_.-]|$)",
    re.I,
)
# Dependency manifests and lockfiles: supply-chain changes.
_MEDIUM_PATH = re.compile(
    r"(^|/)(requirements[^/]*\.txt|setup\.py|setup\.cfg|pyproject\.toml|pipfile(\.lock)?|"
    r"poetry\.lock|package(-lock)?\.json|yarn\.lock|pnpm-lock\.yaml|go\.(mod|sum)|"
    r"cargo\.(toml|lock)|gemfile(\.lock)?|composer\.(json|lock))$",
    re.I,
)
MANY_FILES = 20
MANY_LINES = 500


def higher(a: str, b: str) -> str:
    """The more severe of two levels; an unknown level counts as medium."""
    ra, rb = _ORDER.get(a, 1), _ORDER.get(b, 1)
    return a if ra >= rb else b


def risk_floor(files: list) -> tuple[str, list[str]]:
    """
    (minimum risk, reasons) for a PR's changed files.

    high:   CI workflows/actions, container and compose files, migrations and
            SQL, CODEOWNERS, and auth/security/crypto/secret/session paths.
    medium: dependency manifests and lockfiles, a deleted file, a changed
            file GitHub sent no patch for (binary or too large to diff), more
            than MANY_FILES files or MANY_LINES changed lines.
    """
    from .classify import _is_test_file

    high, medium = [], []
    lines = 0
    for f in files or []:
        if not isinstance(f, dict):
            continue
        name = str(f.get("filename") or "")
        lines += int(f.get("additions") or 0) + int(f.get("deletions") or 0)
        if _is_test_file(name):
            continue  # a test of the auth code does not change the auth code
        if _HIGH_PATH.search(name):
            high.append(name)
        elif _MEDIUM_PATH.search(name):
            medium.append(f"dependency file `{name}`")
        elif f.get("status") == "removed":
            medium.append(f"deletes `{name}`")
        elif not f.get("patch") and f.get("status") != "renamed":
            medium.append(f"no diff available for `{name}`")
    if high:
        shown = ", ".join(f"`{n}`" for n in high[:3]) + (" …" if len(high) > 3 else "")
        return "high", [f"touches {shown}"]
    if len(files or []) > MANY_FILES:
        medium.append(f"{len(files)} files changed")
    if lines > MANY_LINES:
        medium.append(f"{lines} lines changed")
    return ("medium", medium[:3]) if medium else ("low", [])
