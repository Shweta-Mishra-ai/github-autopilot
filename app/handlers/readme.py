"""
app/handlers/readme.py
Keep the README's derived facts true, automatically.

The problem this solves is visible in this repository's own README: the
architecture diagram says the MCP endpoint exposes "8 tools". It exposed 8 when
someone typed it. Adding a ninth did not update it, and nothing complained. The
same rot applies to module counts, the command list, and the layer diagram —
every fact in a README that restates something the code already knows.

Approach: managed regions, not regeneration.
─────────────────────────────────────────────
An LLM rewriting a good README makes it worse. Instead the bot owns only the
blocks between its own markers:

    <!-- autopilot:commands:start -->
    ...regenerated...
    <!-- autopilot:commands:end -->

Everything outside a marker pair is hand-written prose and is never touched.
Everything inside is derived from the code, so it cannot drift. A repository
with no markers gets nothing done to it — adoption is opt-in, per region, by
pasting a marker pair where you want the content.

Delivery is always a pull request, never a direct commit. A README is the most
read file in a repository; a bot silently editing it on main is not acceptable
even when the edit is correct.
"""

from __future__ import annotations

import base64
import logging
import re

from app.github.client import gh_get, gh_post, gh_put, GitHubError

log = logging.getLogger(__name__)

MARKER_RE_TEMPLATE = (
    r"(?P<start><!--\s*autopilot:{name}:start\s*-->)"
    r"(?P<body>.*?)"
    r"(?P<end><!--\s*autopilot:{name}:end\s*-->)"
)

BRANCH = "chore/autopilot-readme"

# A README PR is low urgency and high visibility. Re-proposing one while the
# last is still open is noise, so the flow is: at most one open PR at a time.
_DEDUP_TTL_SECONDS = 24 * 3600


# ── Region generators ────────────────────────────────────────────────────────
# Each returns the markdown that belongs between its markers. Pure functions of
# the codebase, so they are testable without GitHub or an LLM.


def render_stats() -> str:
    """Counts that are true by construction rather than by memory."""
    from app.handlers.comments.constants import ALL_COMMANDS
    from app.intelligence.codegraph import build_graph
    from app.mcp.tools import MCP_TOOLS

    graph = build_graph("app", "server.py", "worker.py", root=".")
    stats = graph.to_dict()["stats"]

    return "\n".join(
        [
            "| | |",
            "|---|---|",
            f"| Modules | {stats['modules']} |",
            f"| Lines of code | {stats['total_loc']:,} |",
            f"| Slash commands | {len(ALL_COMMANDS)} |",
            f"| MCP tools | {len(MCP_TOOLS)} |",
            f"| Internal imports | {stats['edges']} |",
        ]
    )


def render_commands() -> str:
    """The command list, from the registry the dispatcher actually reads."""
    from app.core.authorization import RESTRICTED_COMMANDS
    from app.handlers.comments.constants import ALL_COMMANDS

    rows = [
        "| Command | Access |",
        "|---|---|",
    ]
    for cmd in ALL_COMMANDS:
        access = "maintainer" if cmd in RESTRICTED_COMMANDS else "anyone"
        rows.append(f"| `{cmd}` | {access} |")
    return "\n".join(rows)


def render_architecture() -> str:
    """
    Layer diagram generated from the import graph.

    Hand-drawn diagrams are correct the day they are drawn. This one is derived
    from the AST, so a package that stops depending on another stops being
    drawn that way.
    """
    from app.intelligence.codegraph import build_graph

    graph = build_graph("app", "server.py", "worker.py", root=".")
    return "```mermaid\n" + graph.to_mermaid() + "\n```"


REGION_RENDERERS = {
    "stats": render_stats,
    "commands": render_commands,
    "architecture": render_architecture,
}


# ── Region rewriting ─────────────────────────────────────────────────────────


def find_regions(text: str) -> list[str]:
    """Names of managed regions present in `text`, in document order."""
    found = []
    for name in REGION_RENDERERS:
        if re.search(MARKER_RE_TEMPLATE.format(name=re.escape(name)), text, re.DOTALL):
            found.append(name)
    return found


def apply_regions(text: str, renderers: dict | None = None) -> tuple[str, list[str]]:
    """
    Rewrite every managed region. Returns (new_text, names_that_changed).

    Content outside the markers is untouched, and a region whose rendered
    content is already current is left byte-identical so it produces no diff.
    A renderer that raises leaves its region alone rather than failing the
    whole update — a broken generator should not blank a section of the README.
    """
    renderers = renderers or REGION_RENDERERS
    changed: list[str] = []
    out = text

    for name, render in renderers.items():
        pattern = re.compile(MARKER_RE_TEMPLATE.format(name=re.escape(name)), re.DOTALL)
        match = pattern.search(out)
        if not match:
            continue

        try:
            body = render()
        except Exception as e:
            log.warning(f"readme.render_failed region={name}: {e}")
            continue

        new_body = f"\n{body.strip()}\n"
        if match.group("body") == new_body:
            continue

        # A literal replacement string would interpret backslashes and \\g in
        # generated markdown; build the result by slicing instead.
        out = out[: match.start("body")] + new_body + out[match.end("body") :]
        changed.append(name)

    return out, changed


# ── GitHub delivery ──────────────────────────────────────────────────────────


def _pr_already_open(repo: str, token: str) -> bool:
    owner = repo.split("/")[0]
    try:
        existing = gh_get(f"/repos/{repo}/pulls?head={owner}:{BRANCH}&state=open&per_page=1", token)
        return bool(existing)
    except Exception as e:
        # Fail closed: not knowing whether a PR is open is a reason not to
        # open a second one.
        log.warning(f"readme.pr_lookup_failed repo={repo}: {e}")
        return True


def _recently_proposed(repo: str) -> bool:
    try:
        from app.core.redis_client import get_redis

        r = get_redis()
        return r.set(f"readme_pr:{repo}", "1", nx=True, ex=_DEDUP_TTL_SECONDS) is None
    except Exception as e:
        from app.core.metrics import metrics

        metrics.increment("dedup.redis_unavailable")
        log.warning(f"readme.dedup_unavailable repo={repo}: {e}")
        return True


def _default_branch(repo: str, token: str) -> str:
    try:
        return gh_get(f"/repos/{repo}", token).get("default_branch", "main")
    except Exception:
        return "main"


def maybe_update_readme(repo, commits, token, config, log_ctx) -> bool:
    """
    Refresh the README's managed regions and open a PR if anything changed.

    Returns True when a PR was opened. Never raises — this runs on the push
    path alongside the secret and dependency scans.

    Only the deploying repository's own README is regenerated: the renderers
    read the local source tree, so running this against an arbitrary remote
    repository would describe the bot rather than that repository. Guarded by
    README_SELF_UPDATE_REPO for exactly that reason.
    """
    import os

    try:
        allowed = os.environ.get("README_SELF_UPDATE_REPO", "").strip()
        if not allowed or allowed != repo:
            log_ctx.debug("readme.skipped_not_self")
            return False

        try:
            meta = gh_get(f"/repos/{repo}/contents/README.md", token)
            current = base64.b64decode(meta["content"]).decode("utf-8")
            sha = meta["sha"]
        except GitHubError as e:
            log_ctx.info(f"readme.unreadable: {e}")
            return False

        regions = find_regions(current)
        if not regions:
            log_ctx.debug("readme.no_managed_regions")
            return False

        updated, changed = apply_regions(current)
        if not changed:
            log_ctx.info("readme.already_current")
            return False

        if _pr_already_open(repo, token) or _recently_proposed(repo):
            log_ctx.info("readme.pr_already_pending")
            return False

        base = _default_branch(repo, token)
        head_sha = gh_get(f"/repos/{repo}/git/ref/heads/{base}", token)["object"]["sha"]

        try:
            gh_post(
                f"/repos/{repo}/git/refs",
                token,
                {"ref": f"refs/heads/{BRANCH}", "sha": head_sha},
            )
        except GitHubError as e:
            if "already exists" not in str(e).lower():
                raise
            # Reuse the branch, but commit against the README as it exists
            # there — the blob sha from the default branch would be rejected.
            existing = gh_get(f"/repos/{repo}/contents/README.md?ref={BRANCH}", token)
            sha = existing["sha"]
            updated, changed = apply_regions(base64.b64decode(existing["content"]).decode("utf-8"))
            if not changed:
                log_ctx.info("readme.branch_already_current")
                return False

        gh_put(
            f"/repos/{repo}/contents/README.md",
            token,
            {
                "message": f"docs: refresh generated README sections ({', '.join(changed)})",
                "content": base64.b64encode(updated.encode()).decode(),
                "sha": sha,
                "branch": BRANCH,
            },
        )

        body = (
            "## Generated README sections refreshed\n\n"
            f"Regenerated from the code: **{', '.join(changed)}**.\n\n"
            "These blocks live between `<!-- autopilot:<name>:start -->` markers "
            "and restate facts the code already knows — module counts, the "
            "command registry, the import graph. They are regenerated so they "
            "cannot drift from reality; prose outside the markers is untouched.\n\n"
            "> Review as normal. If a number here looks wrong, the code is the "
            "source of truth, not this file."
        )
        pr = gh_post(
            f"/repos/{repo}/pulls",
            token,
            {
                "title": "docs: refresh generated README sections",
                "head": BRANCH,
                "base": base,
                "body": body + getattr(config, "footer", ""),
            },
        )
        log_ctx.done(f"readme.pr_opened number={pr.get('number')} regions={changed}")
        return True

    except Exception as e:
        log_ctx.error(f"readme.update_failed: {e}")
        return False


# ── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """
    Refresh a local README's managed regions.

    Used by CI and by anyone who wants to regenerate before pushing, so the
    fix for a stale-region CI failure is one command rather than a guess:

        python -m app.handlers.readme            # rewrite README.md
        python -m app.handlers.readme --check    # exit 1 if stale
    """
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(prog="python -m app.handlers.readme")
    parser.add_argument("path", nargs="?", default="README.md")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift and exit 1 without writing",
    )
    args = parser.parse_args(argv)

    target = Path(args.path)
    if not target.exists():
        print(f"{target}: not found")
        return 2

    original = target.read_text(encoding="utf-8")
    regions = find_regions(original)
    if not regions:
        print(f"{target}: no autopilot markers — nothing to do")
        return 0

    updated, changed = apply_regions(original)
    if not changed:
        print(f"{target}: {', '.join(regions)} — up to date")
        return 0

    if args.check:
        print(f"{target}: stale regions: {', '.join(changed)}")
        print("Regenerate with: python -m app.handlers.readme")
        return 1

    target.write_text(updated, encoding="utf-8")
    print(f"{target}: refreshed {', '.join(changed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
