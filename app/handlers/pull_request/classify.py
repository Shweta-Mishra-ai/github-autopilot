"""
app/handlers/pull_request/classify.py
File classification and ordering — pure functions, no I/O, no LLM.

Kept separate because these are the parts most often read in isolation ("why
was this file skipped?", "why was that one reviewed first?") and the parts most
worth testing directly.
"""

from __future__ import annotations

import re

CODE_EXTENSIONS = (
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".cs",
    ".php",
    ".rb",
    ".sh",
    ".sql",
)

CONFIG_EXTENSIONS = (
    ".yml",
    ".yaml",
    ".json",
    ".toml",
    ".ini",
    ".cfg",
    "dockerfile",
    "procfile",
)

GENERATED_EXTENSIONS = frozenset(
    {
        ".lock",
        ".sum",
        ".min.js",
        ".min.css",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".pdf",
        ".zip",
        ".tar",
        ".whl",
        ".snap",
    }
)

# Lockfiles whose extension says nothing. `package-lock.json` matched none of
# the extensions above and scored as *config*, so a lockfile refresh could take
# a slot in the four-file review budget and be reviewed — 3,000 characters of a
# machine-written diff, which produces nothing but false findings.
GENERATED_BASENAMES = frozenset(
    {
        "package-lock.json",
        "pnpm-lock.yaml",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "poetry.lock",
        "composer.lock",
        "cargo.lock",
        "pipfile.lock",
        "gemfile.lock",
        "go.sum",
        "flake.lock",
        "uv.lock",
    }
)

# Paths and infixes that are generated whatever the extension. `.pb.go`,
# `_pb2.py` and `dist/bundle.js` all end in a CODE extension, so they scored
# **3** — outranking hand-written source for the review budget.
#
# Migrations are deliberately absent: they are generated but routinely
# hand-edited, and a destructive migration is exactly the thing worth reviewing.
GENERATED_MARKERS = (
    "/dist/",
    "/build/",
    "/vendor/",
    "/node_modules/",
    "/__generated__/",
    "/.next/",
    ".generated.",
    ".pb.go",
    "_pb2.py",
    "_pb2_grpc.py",
    ".g.dart",
    ".min.",
)

# Substring matching read `app/core/latest_run.py` as a test, because
# "latest_" contains "test_". So did contest_, greatest_, fastest_, protest_,
# and every path under testimonials/ via startswith("test"). Each one was then
# dropped down the review budget, excluded from gap detection's source files,
# AND counted as a test file — so a PR touching only such a file reported
# "tests changed in this PR" and found no gaps in code nothing tests.
# Anchored to path segments instead.
_TEST_PATH = re.compile(
    r"(?:^|/)tests?/"  # a test/ or tests/ directory
    r"|(?:^|/)__tests?__/"  # the JS/TS convention
    r"|(?:^|/)test_[^/]*$"  # test_foo.py
    r"|(?:^|/)[^/]*_test\.[^/.]+$"  # foo_test.go
    r"|(?:^|/)tests?\.[^/.]+$"  # tests.py
)


def _is_test_file(filename: str) -> bool:
    return bool(_TEST_PATH.search((filename or "").replace("\\", "/")))


def _is_generated(filename: str) -> bool:
    # Leading "/" so a marker anchored on a path separator also matches a
    # top-level directory: "dist/bundle.js" has no slash before `dist`.
    lower = "/" + (filename or "").replace("\\", "/").lower().lstrip("/")
    return (
        lower.endswith(tuple(GENERATED_EXTENSIONS))
        or lower.rsplit("/", 1)[-1] in GENERATED_BASENAMES
        or any(m in lower for m in GENERATED_MARKERS)
    )


def _file_review_priority(filename: str) -> int:
    """
    Return priority weight for code review ordering (higher is more important).
    Code files take precedence over documentation and config files.
    """
    lower = filename.lower()
    if lower.endswith(CODE_EXTENSIONS) and not _is_test_file(filename):
        return 3
    if _is_test_file(filename):
        return 2
    if lower.endswith(CONFIG_EXTENSIONS):
        return 1
    return 0


def _review_sort_key(f: dict) -> tuple:
    """
    Ordering for the limited review budget: file kind first, then size of
    change.

    Kind alone is not enough — within a tier, GitHub returns files
    alphabetically, so `app/a.py` with a two-line tweak would be reviewed
    ahead of `app/z.py` with two hundred changed lines. Size is the best
    available proxy for where the risk is.
    """
    filename = f.get("filename", "")
    churn = f.get("additions", 0) + f.get("deletions", 0)
    return (_file_review_priority(filename), churn)


def _blast_radius(files: list) -> str:
    """
    Categorize changed files into system layers for blast radius display.
    Used by the /impact command in handlers/comments/reviewer.py.
    Returns a markdown string summarizing which layers are affected.
    """
    categories: dict[str, list[str]] = {
        "Handlers (API layer)": [],
        "Core (foundation)": [],
        "AI (LLM layer)": [],
        "Security": [],
        "Tests": [],
        "Config / Deploy": [],
        "Documentation": [],
        "Other": [],
    }

    for f in files:
        name = f.get("filename", "")
        if name.startswith("tests/") or name.startswith("test_"):
            categories["Tests"].append(name)
        elif name.startswith("app/handlers/"):
            categories["Handlers (API layer)"].append(name)
        elif name.startswith("app/core/"):
            categories["Core (foundation)"].append(name)
        elif name.startswith("app/ai/"):
            categories["AI (LLM layer)"].append(name)
        elif name.startswith("app/security/"):
            categories["Security"].append(name)
        elif name.endswith(
            (".yml", ".yaml", ".toml", "Procfile", "Dockerfile", "requirements.txt", "render.yaml")
        ):
            categories["Config / Deploy"].append(name)
        elif name.endswith((".md", ".rst", ".txt")):
            categories["Documentation"].append(name)
        else:
            categories["Other"].append(name)

    lines = []
    for layer, layer_files in categories.items():
        if layer_files:
            sample = ", ".join(f"`{f.split('/')[-1]}`" for f in layer_files[:3])
            more = f" +{len(layer_files) - 3} more" if len(layer_files) > 3 else ""
            lines.append(f"- **{layer}** — {sample}{more}")

    return "\n".join(lines) if lines else "- No categorized files found"
