"""
Command Specs - app/core/command_specs.py
V4: Single source of truth for all slash command rules.
Used by input_validator.py before any AI call.

FIXED (ruff F401): Removed unused `Optional` from typing import.
"""

from dataclasses import dataclass, field


@dataclass
class CommandSpec:
    name: str
    contexts: list[str]
    description: str
    hint: str
    example: str
    min_context_chars: int = 0
    max_context_chars: int = 5000
    requires_args: bool = False
    valid_args: list[str] = field(default_factory=list)
    valid_subcommands: list[str] = field(default_factory=list)
    maintainer_only: bool = False


COMMAND_SPECS: dict[str, CommandSpec] = {
    "/fix": CommandSpec(
        name="/fix",
        contexts=["pr", "issue"],
        description="AI suggests a fix for the issue or code",
        hint="Include the code or error message you want fixed.",
        example="/fix\n```python\ndef calc(x, y):\n    retrun x + y  # typo here\n```",
        min_context_chars=20,
        max_context_chars=4000,
    ),
    "/apply": CommandSpec(
        name="/apply",
        contexts=["issue"],
        description="Automatically fix non-conventional commit messages (creates branch + PR)",
        hint="Works on issues about commit convention violations. No code needed.",
        example="/apply",
        min_context_chars=0,
    ),
    "/explain": CommandSpec(
        name="/explain",
        contexts=["pr", "issue"],
        description="Explain code or a concept in plain English",
        hint="Paste the code or describe what you want explained.",
        example="/explain\n```python\n@lru_cache(maxsize=128)\ndef fib(n): ...\n```",
        min_context_chars=10,
        max_context_chars=4000,
    ),
    "/improve": CommandSpec(
        name="/improve",
        contexts=["pr", "issue"],
        description="Get concrete improvement suggestions for code",
        hint="Paste the code you want to improve.",
        example="/improve\n```python\ndef process(items):\n    result = []\n    for i in items:\n        result.append(i * 2)\n    return result\n```",
        min_context_chars=20,
        max_context_chars=4000,
    ),
    "/test": CommandSpec(
        name="/test",
        contexts=["pr", "issue"],
        description="Generate tests for your code (creates a PR with test file)",
        hint="Paste the function or class you want tested.",
        example="/test\n```python\ndef calculate_discount(price, percent):\n    return price * (1 - percent / 100)\n```",
        min_context_chars=20,
        max_context_chars=4000,
    ),
    "/docs": CommandSpec(
        name="/docs",
        contexts=["pr", "issue"],
        description="Generate documentation. /docs readme updates README via PR",
        hint="No args = docstring for pasted code. Use /docs readme or /docs api for repo-level docs.",
        example="/docs\n\nOR\n\n/docs readme\n\nOR\n\n/docs api",
        min_context_chars=0,
        valid_subcommands=["readme", "api", ""],
    ),
    "/refactor": CommandSpec(
        name="/refactor",
        contexts=["pr", "issue"],
        description="Get refactoring suggestions with before/after examples",
        hint="Paste the code you want refactored.",
        example="/refactor\n```python\n# paste your code here\n```",
        min_context_chars=30,
        max_context_chars=4000,
    ),
    "/health": CommandSpec(
        name="/health",
        contexts=["pr", "issue"],
        description="Repository health score and grade",
        hint="No arguments needed.",
        example="/health",
    ),
    "/version": CommandSpec(
        name="/version",
        contexts=["pr", "issue"],
        description="Show tags, releases, and recent commits",
        hint="No arguments needed.",
        example="/version",
    ),
    "/merge": CommandSpec(
        name="/merge",
        contexts=["pr"],
        description="Merge this PR if all guardrails pass",
        hint="This command only works on Pull Requests, not Issues.",
        example="/merge",
        maintainer_only=True,
    ),
    "/summarize": CommandSpec(
        name="/summarize",
        contexts=["pr", "issue"],
        description="Summarize the entire discussion thread",
        hint="No arguments needed. Works best on long threads.",
        example="/summarize",
    ),
    "/ci": CommandSpec(
        name="/ci",
        contexts=["pr", "issue"],
        description="Analyze CI failure logs",
        hint="Paste your CI failure output after the command.",
        example="/ci\n```\nFAILED tests/test_auth.py::test_login - AssertionError\n```",
        min_context_chars=20,
        max_context_chars=8000,
    ),
    "/security": CommandSpec(
        name="/security",
        contexts=["pr"],
        description="Security scan of changed files using GitHub Security APIs",
        hint="Works on Pull Requests. Reads Dependabot, CodeQL, and Secret Scanning.",
        example="/security",
    ),
    "/gaps": CommandSpec(
        name="/gaps",
        contexts=["pr", "issue"],
        description="Find test coverage gaps in code",
        hint="Paste the code you want gap analysis for.",
        example="/gaps\n```python\nclass PaymentProcessor:\n    def charge(self, amount): ...\n```",
        min_context_chars=20,
        max_context_chars=4000,
    ),
    "/changelog": CommandSpec(
        name="/changelog",
        contexts=["pr", "issue"],
        description="Generate CHANGELOG entry from recent commits",
        hint="No arguments needed.",
        example="/changelog",
    ),
    "/rollback": CommandSpec(
        name="/rollback",
        contexts=["pr", "issue"],
        description="Show snapshot history or restore a previous state",
        hint="Without number = show history. With number = restore that snapshot.",
        example="/rollback\n\nOR to restore:\n\n/rollback 2",
        valid_args=["1", "2", "3", "4", "5", "6", "7", "8", "9", "10"],
        maintainer_only=True,
    ),
    "/autofix": CommandSpec(
        name="/autofix",
        contexts=["issue"],
        description="AI creates a fix branch and opens a PR with actual code changes",
        hint="Works on bug issues. Describe the bug clearly in the issue body.",
        example="/autofix",
        min_context_chars=20,
    ),
    "/impact": CommandSpec(
        name="/impact",
        contexts=["pr"],
        description="Show blast radius — which parts of codebase this PR affects",
        hint="Works on Pull Requests only.",
        example="/impact",
    ),
    "/perf": CommandSpec(
        name="/perf",
        contexts=["pr", "issue"],
        description="Performance analysis — find bottlenecks",
        hint="Paste the code you want performance analysis for.",
        example="/perf\n```python\n# paste code here\n```",
        min_context_chars=20,
        max_context_chars=5000,
    ),
    "/arch": CommandSpec(
        name="/arch",
        contexts=["pr", "issue"],
        description="Architecture analysis of code structure and patterns",
        hint="Paste the code or describe the architecture you want reviewed.",
        example="/arch\n```python\n# paste your module/class here\n```",
        min_context_chars=20,
        max_context_chars=5000,
    ),
    "/release": CommandSpec(
        name="/release",
        contexts=["pr", "issue"],
        description="Bump version and create a GitHub Release",
        hint="Specify: patch (bug fix), minor (new feature), or major (breaking change).",
        example="/release patch\n\nOR\n\n/release minor\n\nOR\n\n/release major",
        requires_args=True,
        valid_args=["patch", "minor", "major"],
        maintainer_only=True,
    ),
    "/runtests": CommandSpec(
        name="/runtests",
        contexts=["pr", "issue"],
        description="Trigger the CI test workflow via GitHub Actions API",
        hint="No arguments needed. Requires GitHub Actions CI to be configured.",
        example="/runtests",
    ),
    "/secfull": CommandSpec(
        name="/secfull",
        contexts=["pr", "issue"],
        description="Full security report: Dependabot + CodeQL + Secrets + License + Behavioral",
        hint="No arguments needed. Reads all GitHub Security APIs.",
        example="/secfull",
    ),
    "/budget": CommandSpec(
        name="/budget",
        contexts=["pr", "issue"],
        description="Show today's LLM usage per provider with % of daily limit used",
        hint="No arguments needed.",
        example="/budget",
    ),
}

ALL_COMMANDS: list[str] = list(COMMAND_SPECS.keys())


def get_spec(cmd: str) -> CommandSpec | None:
    return COMMAND_SPECS.get(cmd.lower())


def find_similar(cmd: str) -> str | None:
    cmd_lower = cmd.lower().lstrip("/")
    for known in ALL_COMMANDS:
        known_bare = known.lstrip("/")
        if known_bare.startswith(cmd_lower[:3]):
            return known
    for known in ALL_COMMANDS:
        known_bare = known.lstrip("/")
        if abs(len(known_bare) - len(cmd_lower)) <= 1:
            diffs = sum(a != b for a, b in zip(known_bare, cmd_lower))
            if diffs <= 1:
                return known
    return None


def commands_for_context(context: str) -> list[str]:
    return [name for name, spec in COMMAND_SPECS.items() if context in spec.contexts]
