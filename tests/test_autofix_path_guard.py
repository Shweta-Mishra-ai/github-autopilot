"""
tests/test_autofix_path_guard.py

`/autofix` opens a pull request that rewrites a file. The path it writes comes
from the model's `target_file`, and BLOCKED_PATHS / BLOCKED_PREFIXES are the
only thing keeping it away from `server.py`, `requirements.txt`, the CI
workflows and `app/core/authorization.py` — the module that decides who may run
destructive commands at all.

Those lists are exact strings and prefixes, so they only work if the path being
tested is spelled the way they are. It was not. Seven of fifteen probe paths
walked straight through:

    ./server.py                    ./requirements.txt
    ./pyproject.toml               ./setup.py
    app/core/./config.py           app//core//config.py
    ./app/core/authorization.py

GitHub's Contents API resolves every one of them to the protected file, and
`target` went verbatim into the PUT — so the guard checked one spelling and the
write used another.

An issue body is attacker-controlled on a public repo, and it is the input the
model derives `target_file` from.
"""

from __future__ import annotations

import posixpath

import pytest

from app.handlers.autofix import (
    ALLOWED_EXTENSIONS,
    BLOCKED_PATHS,
    BLOCKED_PREFIXES,
    _block_reason,
    _is_allowed,
    normalise_path,
)


class TestTheOriginalBypasses:
    @pytest.mark.parametrize(
        "path",
        [
            "./server.py",
            "./requirements.txt",
            "./pyproject.toml",
            "./setup.py",
            "app/core/./config.py",
            "app//core//config.py",
            "./app/core/authorization.py",
            "./app/core/webhook_security.py",
            "./app/github/auth.py",
            "app/core//authorization.py",
            " app/core/authorization.py",
            "app/core/authorization.py ",
        ],
    )
    def test_a_respelled_protected_path_is_still_blocked(self, path):
        assert _is_allowed(path) is False, f"{path!r} bypasses the guard"


class TestEveryProtectedPathResistsRespelling:
    """Generated from the blocklists themselves, so a path added to
    BLOCKED_PATHS tomorrow is covered by these variants automatically."""

    @staticmethod
    def _variants(path: str) -> list[str]:
        head, _, tail = path.rpartition("/")
        out = [f"./{path}", f"  {path}", f"{path}  ", f".//{path}"]
        if head:
            out += [f"{head}/./{tail}", f"{head}//{tail}", f"./{head}/{tail}"]
        return out

    @pytest.mark.parametrize("protected", sorted(BLOCKED_PATHS))
    def test_exact_blocklist_entries(self, protected):
        for variant in self._variants(protected):
            assert _is_allowed(variant) is False, f"{variant!r} reaches {protected}"

    @pytest.mark.parametrize("prefix", BLOCKED_PREFIXES)
    def test_prefix_blocklist_entries(self, prefix):
        target = f"{prefix.rstrip('/')}/thing.py" if prefix.endswith("/") else f"{prefix}_x.py"
        for variant in self._variants(target):
            assert _is_allowed(variant) is False, f"{variant!r} reaches {prefix}"


class TestNormalisationIsTheSingleSpelling:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("./server.py", "server.py"),
            ("app//core//config.py", "app/core/config.py"),
            ("app/core/./config.py", "app/core/config.py"),
            ("  README.md  ", "README.md"),
            ("docs/../server.py", "server.py"),
            ("a/b/../../c.py", "c.py"),
        ],
    )
    def test_equivalent_spellings_collapse(self, raw, expected):
        assert normalise_path(raw) == expected

    @pytest.mark.parametrize(
        "escape", ["/etc/passwd", "../secrets.py", "../../x.py", "a/../../x.py", "..", ".", ""]
    )
    def test_anything_leaving_the_repository_is_rejected(self, escape):
        assert normalise_path(escape) == ""
        assert _is_allowed(escape) is False

    def test_it_agrees_with_posixpath(self):
        """The normaliser must not invent its own rules — GitHub resolves these
        the way posixpath does, and any divergence is a new bypass."""
        for raw in ["./a.py", "a//b.py", "a/./b.py", "a/b/../c.py"]:
            assert normalise_path(raw) == posixpath.normpath(raw)

    def test_backslashes_are_folded_not_trusted(self):
        """A Windows-style separator must not create a path that misses a
        prefix check and then resolves on GitHub."""
        assert _is_allowed(".github\\\\workflows\\\\ci.yml") is False


class TestLegitimateTargetsStillWork:
    """A guard tightened until nothing passes has been broken, not fixed."""

    @pytest.mark.parametrize(
        "path",
        [
            "app/handlers/foo.py",
            "README.md",
            "docs/guide.md",
            "data.json",
            "notes.txt",
            "mkdocs.yml",
            "./app/handlers/foo.py",
            "app/handlers/../handlers/foo.py",
        ],
    )
    def test_ordinary_files_are_editable(self, path):
        assert _is_allowed(path) is True, f"{path!r} was wrongly blocked"

    def test_the_allowed_extension_set_is_unchanged(self):
        assert {".py", ".md", ".txt", ".json", ".toml"} == ALLOWED_EXTENSIONS


class TestTheUserAlwaysGetsAReason:
    """_block_reason is interpolated straight into the comment, so a None
    renders as 'Cannot auto-modify `x` — None.'"""

    @pytest.mark.parametrize(
        "path",
        [
            "./server.py",
            "app//core//config.py",
            "../escape.py",
            "/etc/passwd",
            "",
            "   ",
            "binary.exe",
            ".github/workflows/ci.yml",
        ],
    )
    def test_every_rejection_has_a_sentence(self, path):
        assert _is_allowed(path) is False
        reason = _block_reason(path)
        assert isinstance(reason, str) and reason.strip(), f"no reason for {path!r}"

    def test_the_two_functions_never_disagree(self):
        """If _block_reason normalised differently from _is_allowed, a blocked
        path could report the reason for a different path entirely."""
        for path in [
            "./server.py",
            "app/core/./config.py",
            "app/handlers/ok.py",
            "./README.md",
            "../nope.py",
        ]:
            blocked = not _is_allowed(path)
            has_reason = isinstance(_block_reason(path), str)
            assert blocked == has_reason, f"disagreement on {path!r}"
