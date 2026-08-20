"""
app/handlers/pull_request/classify.py
File classification and ordering — pure functions, no I/O, no LLM.

Kept separate because these are the parts most often read in isolation ("why
was this file skipped?", "why was that one reviewed first?") and the parts most
worth testing directly.
"""

from __future__ import annotations

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
    }
)


def _is_test_file(filename: str) -> bool:
    return (
        "test_" in filename
        or "_test." in filename
        or "/tests/" in filename
        or filename.startswith("test")
    )


def _is_generated(filename: str) -> bool:
    return any(filename.endswith(ext) for ext in GENERATED_EXTENSIONS)


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
