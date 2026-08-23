"""
docs/COMMANDS.md must describe the commands that actually exist.

A reference is only worth reading if it is true, and documentation drifts
silently: a command added to the registry and not documented reads to a user
as a command that does not exist, and a command documented but removed reads
as one that is broken. Neither shows up in any other test.

The registry in app/core/commands.py is the source of truth — the dispatcher
matches against it, so a command absent from there does not exist.
"""

import re
from pathlib import Path

import pytest

from app.core.commands import ALL_COMMANDS, RESTRICTED_COMMANDS

DOC = Path(__file__).resolve().parent.parent / "docs" / "COMMANDS.md"
README = Path(__file__).resolve().parent.parent / "README.md"


@pytest.fixture(scope="module")
def doc_text():
    assert DOC.exists(), f"{DOC} is referenced from the README and must exist"
    return DOC.read_text(encoding="utf-8")


def _documented(text: str) -> set[str]:
    """Commands appearing as `/name` in a table row or heading."""
    return {f"/{m}" for m in re.findall(r"`(/[a-z]+)", text) for m in [m[1:]]}


class TestEveryCommandIsDocumented:
    def test_no_command_is_missing_from_the_reference(self, doc_text):
        missing = sorted(set(ALL_COMMANDS) - _documented(doc_text))
        assert not missing, (
            f"{missing} exist in the registry but are absent from docs/COMMANDS.md. "
            f"To a user, an undocumented command is one that does not exist."
        )

    def test_no_command_is_documented_that_does_not_exist(self, doc_text):
        # Only check things shaped like our commands, so prose like `/health`
        # inside a URL or an endpoint path does not trip this.
        endpoints = {"/setup", "/webhook", "/ping", "/graph", "/metrics", "/repos"}
        documented = _documented(doc_text) - endpoints
        phantom = sorted(documented - set(ALL_COMMANDS))
        assert not phantom, (
            f"{phantom} are documented but are not in the registry, so the "
            f"dispatcher will never match them. Documenting a command that "
            f"does nothing is worse than not documenting it."
        )


class TestAccessLevelsMatchTheCode:
    def test_the_access_table_lists_exactly_the_restricted_commands(self, doc_text):
        # The "Maintainer" row of the access table is the claim users act on.
        match = re.search(r"\| \*\*Maintainer\*\* \|[^|]*\|([^|]*)\|", doc_text)
        assert match, "docs/COMMANDS.md must carry the access table"
        listed = {f"/{m}" for m in re.findall(r"`/([a-z]+)`", match.group(1))}
        assert listed == set(RESTRICTED_COMMANDS), (
            f"The access table says {sorted(listed)} but the code restricts "
            f"{sorted(RESTRICTED_COMMANDS)}. A wrong permission claim sends a "
            f"maintainer looking for a bug that is not there — or worse, tells "
            f"a contributor a gated command is open to them."
        )

    @pytest.mark.parametrize("command", sorted(RESTRICTED_COMMANDS))
    def test_each_restricted_command_is_marked_maintainer_in_its_own_row(
        self, command, doc_text
    ):
        rows = [ln for ln in doc_text.splitlines() if ln.startswith(f"| `{command}`")]
        assert rows, f"{command} has no row of its own in docs/COMMANDS.md"
        assert any("Maintainer" in row for row in rows), (
            f"{command} is restricted in code but its row does not say Maintainer: {rows}"
        )

    @pytest.mark.parametrize(
        "command", sorted(set(ALL_COMMANDS) - set(RESTRICTED_COMMANDS))
    )
    def test_open_commands_are_not_labelled_maintainer(self, command, doc_text):
        rows = [ln for ln in doc_text.splitlines() if ln.startswith(f"| `{command}`")]
        assert rows, f"{command} has no row of its own in docs/COMMANDS.md"
        assert not any("Maintainer" in row for row in rows), (
            f"{command} is open to anyone in code, but docs/COMMANDS.md marks it "
            f"Maintainer. That discourages contributors from using a command "
            f"they are entitled to."
        )


class TestReadmeStaysConsistent:
    def test_readme_links_to_the_reference(self):
        assert "docs/COMMANDS.md" in README.read_text(encoding="utf-8"), (
            "The README's command table is a summary; it must point at the full "
            "reference or the reference will not be found."
        )

    def test_readme_table_does_not_contradict_the_code(self):
        text = README.read_text(encoding="utf-8")
        for command in sorted(RESTRICTED_COMMANDS):
            rows = [ln for ln in text.splitlines() if ln.startswith(f"| `{command}")]
            if not rows:
                continue
            assert any("Maintainer" in r for r in rows), (
                f"README lists {command} without marking it Maintainers-only"
            )
