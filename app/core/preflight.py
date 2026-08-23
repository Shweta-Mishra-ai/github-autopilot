"""
app/core/preflight.py — answer "why doesn't this work on my repo?" with evidence.

THE PROBLEM THIS SOLVES
  When the collaborator-permission API returns 403, seven commands refuse to
  run. The reply correctly says the check could not be completed and points at
  the App installation — but it cannot say WHICH permission is missing, so the
  operator is left comparing checkboxes against a list in a README.

HOW IT ANSWERS
  Not by mapping endpoints to permission names from documentation, which would
  be a guess that rots the moment GitHub changes a requirement. Two sources of
  ground truth instead:

    1. What GitHub says it granted. The installation-token response carries a
       `permissions` object; app/github/auth.py now keeps it.
    2. What actually happens. Each capability is probed by making the real call
       and recording the literal outcome.

  A probe that returns 403 is not interpreted, it is reported: this call, this
  status, these commands stop working. That is information the operator can act
  on without anyone having to be right about GitHub's permission model.

  Read-only by construction: every probe is a GET.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class Capability:
    """One thing the bot needs to be able to do, and what breaks without it."""

    name: str
    path: str  # {repo} is substituted
    enables: tuple[str, ...]
    required: bool = True

    def endpoint(self, repo: str, actor: str) -> str:
        return self.path.format(repo=repo, actor=actor)


# Ordered by consequence. The first entry is the one that silently disabled
# seven commands, so it is checked and reported first.
CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "collaborator permission",
        "/repos/{repo}/collaborators/{actor}/permission",
        ("/merge", "/apply", "/autofix", "/rollback", "/release", "/ignore", "/secfull"),
    ),
    Capability("repository metadata", "/repos/{repo}", ("every command",)),
    Capability("issues", "/repos/{repo}/issues?per_page=1", ("triage", "all comment commands")),
    Capability("pull requests", "/repos/{repo}/pulls?per_page=1", ("PR review", "/merge")),
    Capability("contents", "/repos/{repo}/contents/README.md", ("/autofix", "config", "/security")),
    Capability("actions", "/repos/{repo}/actions/runs?per_page=1", ("/ci", "/runtests")),
    Capability(
        "code scanning",
        "/repos/{repo}/code-scanning/alerts?per_page=1",
        ("/secfull CodeQL section",),
        required=False,
    ),
    Capability(
        "dependabot alerts",
        "/repos/{repo}/dependabot/alerts?per_page=1",
        ("/secfull dependency section",),
        required=False,
    ),
)


@dataclass
class ProbeResult:
    capability: str
    ok: bool
    status: int | str
    detail: str
    enables: tuple[str, ...]
    required: bool


@dataclass
class Diagnosis:
    repo: str
    installation_id: int
    granted: dict = field(default_factory=dict)
    probes: list[ProbeResult] = field(default_factory=list)
    error: str = ""

    @property
    def broken(self) -> list[ProbeResult]:
        return [p for p in self.probes if not p.ok and p.required]

    @property
    def healthy(self) -> bool:
        return not self.error and not self.broken


@dataclass
class EnvFinding:
    """One deployment setting, its state, and what it costs when unset."""

    name: str
    state: str  # "ok" | "warn" | "off" | "unknown"
    detail: str


def inspect_environment() -> list[EnvFinding]:
    """
    The deployment-level settings that are silently optional. Never raises.

    Separate from the capability probes because these need no token and no
    repository — which is exactly when they matter most, since a deployment
    whose credentials are wrong still needs to be told its backups are off.

    Reports whether a secret is SET, never what it contains.
    """
    import os

    findings: list[EnvFinding] = []

    # Rate-limit trust model, judged against traffic rather than assumed.
    try:
        from app.core.webhook_security import proxy_configuration_verdict

        verdict, detail = proxy_configuration_verdict()
        findings.append(EnvFinding("X-Forwarded-For trust", verdict, detail))
    except Exception as exc:  # a diagnostic must not break the diagnostic
        findings.append(EnvFinding("X-Forwarded-For trust", "unknown", str(exc)[:160]))

    # Encrypted memory backup. Partial configuration is the dangerous state:
    # it looks configured and silently keeps nothing off-box.
    key = bool(os.environ.get("MEMORY_BACKUP_KEY", "").strip())
    repo = bool(os.environ.get("MEMORY_BACKUP_REPO", "").strip())
    token = bool(os.environ.get("MEMORY_BACKUP_TOKEN", "").strip())
    if key and repo and token:
        findings.append(
            EnvFinding("Memory backup", "ok", "Configured — learned state survives a redeploy.")
        )
    elif not key and not repo and not token:
        findings.append(
            EnvFinding(
                "Memory backup",
                "off",
                "MEMORY_BACKUP_KEY, MEMORY_BACKUP_REPO and MEMORY_BACKUP_TOKEN are "
                "all unset, so nothing is backed up. Everything the bot has learned "
                "about this repository is lost when the instance restarts — which on "
                "a free tier is routine, not exceptional. Generate a key with "
                "`python -m app.core.memory_backup genkey`.",
            )
        )
    else:
        missing = [
            n
            for n, present in (
                ("MEMORY_BACKUP_KEY", key),
                ("MEMORY_BACKUP_REPO", repo),
                ("MEMORY_BACKUP_TOKEN", token),
            )
            if not present
        ]
        findings.append(
            EnvFinding(
                "Memory backup",
                "warn",
                f"Partially configured — {', '.join(missing)} still unset, so no "
                f"backup is written. This is the worst of the three states: it "
                f"looks configured and keeps nothing.",
            )
        )

    # Local triage gate. Absent is a valid choice, not a fault.
    if os.environ.get("OLLAMA_HOST", "").strip():
        findings.append(
            EnvFinding(
                "Local triage gate",
                "ok",
                "OLLAMA_HOST is set — trivial events are filtered locally before "
                "any hosted model is called.",
            )
        )
    else:
        findings.append(
            EnvFinding(
                "Local triage gate",
                "off",
                "OLLAMA_HOST is unset, so the gate is inert and every event goes "
                "to a hosted provider. Valid, and the default; setting it cuts "
                "spend on events that were never worth a review.",
            )
        )

    return findings


def format_environment_report(findings: list[EnvFinding]) -> str:
    """Markdown for the deployment settings. Never raises."""
    marks = {"ok": "✅", "warn": "⚠️", "off": "⚪", "unknown": "❔"}
    lines = ["### Deployment settings", "", "| Setting | State | Notes |", "|---|---|---|"]
    for f in findings:
        lines.append(f"| {f.name} | {marks.get(f.state, '❔')} {f.state} | {f.detail} |")
    return "\n".join(lines)


def _probe(path: str, token: str) -> tuple[bool, int | str, str]:
    """Make the real call. Never raises; the failure IS the result."""
    from app.github.client import GitHubError, gh_get

    try:
        gh_get(path, token)
        return True, 200, "ok"
    except GitHubError as exc:
        return False, getattr(exc, "status_code", "?") or "?", str(exc)[:160]
    except Exception as exc:
        return False, "network", f"{type(exc).__name__}: {str(exc)[:120]}"


def diagnose(repo: str, installation_id: int, actor: str = "") -> Diagnosis:
    """
    Probe every capability against a real repository. Never raises.

    `actor` is the login whose permission is checked; it only has to be a real
    user for the collaborator probe to be meaningful, so it defaults to the
    repository owner.
    """
    actor = actor or (repo.split("/")[0] if "/" in repo else "")
    result = Diagnosis(repo=repo, installation_id=installation_id)

    try:
        from app.github.auth import get_installation_permissions, get_installation_token

        token = get_installation_token(installation_id)
        result.granted = get_installation_permissions(installation_id)
    except Exception as exc:
        # No token means nothing else can be probed, and the cause is the App
        # credentials rather than any repository permission.
        result.error = (
            f"Could not obtain an installation token: {str(exc)[:200]}. "
            "Check GITHUB_APP_ID and GITHUB_PRIVATE_KEY, and that the App is "
            "installed on this repository."
        )
        return result

    for cap in CAPABILITIES:
        ok, status, detail = _probe(cap.endpoint(repo, actor), token)
        result.probes.append(ProbeResult(cap.name, ok, status, detail, cap.enables, cap.required))
    return result


def format_report(d: Diagnosis) -> str:
    """Markdown an operator can act on. Never raises."""
    env = format_environment_report(inspect_environment())
    if d.error:
        return f"## ❌ Setup check failed\n\n{d.error}\n\n{env}"

    head = "## ✅ Setup looks correct" if d.healthy else "## ⚠️ Setup is incomplete"
    lines = [head, "", f"Repository `{d.repo}` · installation `{d.installation_id}`", ""]

    lines += ["| Capability | Result | Enables |", "|---|---|---|"]
    for p in d.probes:
        mark = "✅" if p.ok else ("❌" if p.required else "⚪")
        status = "ok" if p.ok else f"`{p.status}`"
        lines.append(f"| {p.capability} | {mark} {status} | {', '.join(p.enables)} |")

    if d.broken:
        lines += ["", "### What is not working", ""]
        for p in d.broken:
            lines.append(
                f"**{p.capability}** returned `{p.status}` — {', '.join(p.enables)} "
                f"will not run."
            )
            lines.append(f"> {p.detail}")
            lines.append("")
        lines.append(
            "Fix this in the App's **repository permissions**, then reinstall or "
            "accept the new permissions on the installation. GitHub does not "
            "apply added permissions to an existing installation until they are "
            "accepted."
        )

    if d.granted:
        lines += [
            "",
            "<details><summary>Permissions GitHub reports for this installation</summary>",
            "",
        ]
        for k, v in sorted(d.granted.items()):
            lines.append(f"- `{k}`: {v}")
        lines += ["", "</details>"]
    else:
        lines += ["", "_GitHub reported no permission list for this installation._"]

    lines += ["", env]
    return "\n".join(lines)
