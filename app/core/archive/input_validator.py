"""
Input Validator - app/core/input_validator.py
V4: Validates ALL slash commands before any AI call.

FIXED (ruff F401): Removed COMMAND_SPECS + ALL_COMMANDS from import — unused.
FIXED (ruff F541): Two f-strings had no placeholders — removed f prefix.
"""

import re
import logging
from dataclasses import dataclass

from app.core.command_specs import (
    CommandSpec,
    get_spec,
    find_similar,
    commands_for_context,
)

log = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    valid: bool
    error_response: str = ""
    cleaned_cmd: str = ""
    cmd_args: str = ""
    cmd_subcommand: str = ""
    context_text: str = ""
    spec: CommandSpec | None = None


def validate_command(
    body: str,
    raw_cmd: str,
    is_pr: bool,
    author: str = "",
    author_is_maintainer: bool = False,
    code_context: str = "",
    text_context: str = "",
) -> ValidationResult:
    """
    Full validation pipeline for a slash command.
    Returns ValidationResult with valid=True if OK to proceed.
    """
    cmd = raw_cmd.lower().strip()
    context_type = "pr" if is_pr else "issue"

    # Step 1: Command recognized?
    spec = get_spec(cmd)
    if spec is None:
        similar = find_similar(cmd)
        available = commands_for_context(context_type)[:8]
        suggestion = f"\n\n💡 Did you mean **{similar}**?" if similar else ""
        return ValidationResult(
            valid=False,
            error_response=_fmt_error(
                title=f"Unknown Command: `{cmd}`",
                body=(
                    f"This command isn't recognized.{suggestion}\n\n"
                    f"**Available commands for this {context_type}:**\n"
                    + " ".join(f"`{c}`" for c in available)
                    # FIXED (F541): Removed f prefix — no placeholders
                    + "\n\nSee all commands in [README](https://github.com/Shweta-Mishra-ai/github-autopilot#commands)."
                ),
            ),
        )

    # Step 2: Context check (PR vs Issue)
    if context_type not in spec.contexts:
        allowed = " and ".join(spec.contexts)
        return ValidationResult(
            valid=False,
            error_response=_fmt_error(
                title=f"Wrong Context: `{cmd}`",
                body=(
                    f"`{cmd}` only works on **{allowed}s**, not {context_type}s.\n\n"
                    f"**Example:** Open a {allowed} and comment `{spec.example.split(chr(10))[0]}`"
                ),
            ),
        )

    # Step 3: Maintainer-only check
    if spec.maintainer_only and not author_is_maintainer:
        return ValidationResult(
            valid=False,
            error_response=_fmt_error(
                title=f"Permission Denied: `{cmd}`",
                body=(
                    f"`{cmd}` is restricted to repo maintainers and owners.\n\n"
                    f"@{author} — if you think this is an error, ask a maintainer to run it."
                ),
            ),
        )

    # Step 4: Args validation
    cmd_args, cmd_subcommand = _extract_args(body, cmd)

    if spec.requires_args and not cmd_args:
        return ValidationResult(
            valid=False,
            error_response=_fmt_error(
                title=f"Missing Argument: `{cmd}`",
                body=(
                    f"`{cmd}` requires an argument.\n\n"
                    f"**Valid options:** {', '.join(f'`{a}`' for a in spec.valid_args)}\n\n"
                    f"**Example:**\n```\n{spec.example}\n```\n\n"
                    f"**What it does:** {spec.hint}"
                ),
            ),
        )

    if spec.valid_args and cmd_args and cmd_args not in spec.valid_args:
        return ValidationResult(
            valid=False,
            error_response=_fmt_error(
                title=f"Invalid Argument: `{cmd} {cmd_args}`",
                body=(
                    f"`{cmd_args}` is not a valid argument for `{cmd}`.\n\n"
                    f"**Valid options:** {', '.join(f'`{a}`' for a in spec.valid_args)}\n\n"
                    f"**Example:**\n```\n{spec.example}\n```"
                ),
            ),
        )

    if spec.valid_subcommands and cmd_subcommand not in spec.valid_subcommands:
        return ValidationResult(
            valid=False,
            error_response=_fmt_error(
                title=f"Invalid Subcommand: `{cmd} {cmd_subcommand}`",
                body=(
                    "**Valid subcommands:** "
                    + ", ".join(
                        f"`{s}`" if s else "*(none)*" for s in spec.valid_subcommands
                    )
                    + f"\n\n**Example:**\n```\n{spec.example}\n```"
                ),
            ),
        )

    if cmd == "/rollback" and cmd_args:
        try:
            n = int(cmd_args)
            if n < 1 or n > 10:
                return ValidationResult(
                    valid=False,
                    error_response=_fmt_error(
                        title="Invalid Snapshot Number",
                        body=(
                            f"Snapshot `#{n}` is out of range. "
                            "Valid range: 1–10.\n\n"
                            "Use `/rollback` (no number) to see available snapshots."
                        ),
                    ),
                )
        except ValueError:
            return ValidationResult(
                valid=False,
                error_response=_fmt_error(
                    title="Invalid Snapshot Number",
                    body=(
                        f"`{cmd_args}` is not a valid number.\n\n"
                        "Use `/rollback` to list snapshots, then `/rollback 2` to restore."
                    ),
                ),
            )

    # Step 5: Context length check
    combined_context = code_context or text_context or ""
    context_len = len(combined_context.strip())

    if spec.min_context_chars > 0 and context_len < spec.min_context_chars:
        return ValidationResult(
            valid=False,
            error_response=_fmt_error(
                title=f"Need More Context: `{cmd}`",
                body=(
                    f"Not enough context to work with "
                    f"({context_len} chars, minimum {spec.min_context_chars}).\n\n"
                    f"**{spec.hint}**\n\n"
                    f"**Example:**\n```\n{spec.example}\n```"
                ),
            ),
        )

    if spec.max_context_chars > 0 and context_len > spec.max_context_chars:
        combined_context = combined_context[: spec.max_context_chars]

    log.info(f"input_validator.passed cmd={cmd} context={context_type} author={author}")
    return ValidationResult(
        valid=True,
        cleaned_cmd=cmd,
        cmd_args=cmd_args,
        cmd_subcommand=cmd_subcommand,
        context_text=combined_context,
        spec=spec,
    )


def _extract_args(body: str, cmd: str) -> tuple[str, str]:
    pattern = re.escape(cmd) + r"\s*([^\n]*)"
    match = re.search(pattern, body, re.IGNORECASE)
    if not match:
        return "", ""

    rest = match.group(1).strip().lower()

    if cmd in ("/docs", "/rollback", "/release"):
        parts = rest.split()
        arg = parts[0] if parts else ""
        return arg, arg if cmd == "/docs" else ""

    return rest, ""


def _fmt_error(title: str, body: str) -> str:
    return (
        f"## ℹ️ {title}\n\n"
        f"{body}\n\n"
        "---\n"
        # FIXED (F541): Removed f prefix — no placeholders
        "*🤖 AI Repo Manager V4 — "
        "[All Commands](https://github.com/Shweta-Mishra-ai/github-autopilot#commands)*"
    )


def check_maintainer_role(repo: str, username: str, token: str) -> bool:
    try:
        from app.github.client import gh_get

        data = gh_get(f"/repos/{repo}/collaborators/{username}/permission", token)
        role = data.get("permission", "read")
        return role in ("write", "maintain", "admin")
    except Exception:
        return False
