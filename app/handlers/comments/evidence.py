"""
app/handlers/comments/evidence.py
Checks a command's answer against what the model was actually shown.

The commands publish model prose about code, logs and commits. Prompts said
"only describe what you see", and the answers still named tests that were not
in the log ("tests/test_billing.py::test_refund_partial fails"), functions
that were not in the diff ("OrderExporter.to_csv has no tests"), coverage
percentages nobody measured ("Coverage is 42%") and speedups nobody ran
("~50x faster"). These helpers let a command check a claim against its own
input instead of trusting the wording of the prompt.
"""

from __future__ import annotations

import re

# Numbers the bot never measures: coverage and other percentages, speedup
# factors, latencies and throughputs.
_UNMEASURED = re.compile(
    r"\d+(?:\.\d+)?\s*%"
    r"|\b\d+(?:\.\d+)?\s*[x×]\s*(?:faster|slower|speed|improvement|fewer|more|less)"
    r"|~\s*\d+(?:\.\d+)?\s*[x×]"
    r"|\b\d+(?:\.\d+)?\s*(?:ms|milliseconds|µs|seconds|secs|rps|qps|req/s|requests per second)\b",
    re.I,
)

_PATH = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*[\w-]+\.(?:py|js|jsx|ts|tsx|go|rb|java|kt|rs|php|cs|cpp|cc|c|h|"
    r"hpp|swift|scala|sh|sql|ya?ml|toml|json|cfg|ini))(?![\w/])"
)
_TEST_ID = re.compile(r"::(\w{3,})")
_BACKTICK = re.compile(r"`([^`\n]{3,80})`")
_CALL = re.compile(r"\b([A-Za-z_]\w{2,})\(")
_DOTTED = re.compile(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)\b")
_SNAKE = re.compile(r"\b([a-z]\w*_\w+)\b")
_CAMEL = re.compile(r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+)\b")


def split_sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if s]


def drop_unmeasured(text: str) -> tuple[str, int]:
    """`text` without the sentences that state an unmeasured number; and how
    many were dropped."""
    sentences = split_sentences(text)
    kept = [s for s in sentences if not _UNMEASURED.search(s)]
    return " ".join(kept), len(sentences) - len(kept)


def has_unmeasured(text: str) -> bool:
    return bool(_UNMEASURED.search(text or ""))


def code_names(text: str) -> list[str]:
    """
    Things in `text` that name code: file paths, pytest ids, `backticked`
    spans, call()s, dotted names, snake_case and CamelCase identifiers.
    A name that is part of a file path is reported once, as the path.
    """
    text = str(text or "")
    paths = _PATH.findall(text)
    names: list[str] = list(paths)
    names += _TEST_ID.findall(text)
    for span in _BACKTICK.findall(text):
        if " " not in span.strip():
            names.append(span.strip().rstrip("()"))
    names += _CALL.findall(text)
    names += _DOTTED.findall(text)
    names += _SNAKE.findall(text)
    names += _CAMEL.findall(text)
    seen, out = set(), []
    for n in names:
        # "test_billing" is part of "tests/test_billing.py", not a second name.
        if n not in paths and any(n in p for p in paths):
            continue
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _present(name: str, source: str) -> bool:
    if name in source:
        return True
    # "app/orders.py" for a source that says "orders.py", or the reverse.
    if "/" in name:
        return name.rsplit("/", 1)[-1] in source
    # "OrderService.get" when the source has both parts but not joined.
    if "." in name:
        return all(part in source for part in name.split("."))
    return False


def missing_from(text: str, source: str) -> list[str]:
    """The code names in `text` that `source` does not contain."""
    source = str(source or "")
    return [n for n in code_names(text) if not _present(n, source)]


_CHANGELOG_STOP = frozenset(
    "the and for with from into this that are was not all via now new use uses add adds "
    "added fix fixes fixed update updates updated change changes changed remove removes "
    "removed support improve improves improved bump bumps feat chore docs refactor".split()
)


def _content_words(text: str) -> set:
    return {
        w
        for w in re.findall(r"[a-z0-9]+", str(text).lower())
        if len(w) >= 3 and w not in _CHANGELOG_STOP
    }


def changelog_entry(model_text: str, subjects: list, today: str) -> tuple[str, int]:
    """
    (entry, dropped): the model's sections under a heading this code writes,
    keeping only bullets most of whose words come from the commit subjects.
    """
    per_commit = [_content_words(s) for s in subjects]
    vocabulary = set().union(*per_commit) if per_commit else set()
    sections: list[tuple[str, list[str]]] = []
    dropped = 0
    for line in str(model_text).splitlines():
        stripped = line.strip()
        if re.match(r"^###\s+\w", stripped):
            sections.append((stripped, []))
        elif re.match(r"^[-*]\s+\S", stripped):
            words = _content_words(stripped)
            backed = (
                words
                and any(words & c for c in per_commit)
                and 2 * len(words & vocabulary) >= len(words)
            )
            if not backed:
                dropped += 1
            elif sections:
                sections[-1][1].append("- " + stripped[1:].strip())
            else:
                sections.append(("### Changed", ["- " + stripped[1:].strip()]))
    body = "\n".join("\n".join([head, *bullets]) for head, bullets in sections if bullets)
    return (f"## [Unreleased] - {today}\n{body}" if body else ""), dropped
