"""
tests/test_changelog_scope.py

/changelog took "the latest tag" from the first tag GitHub lists — ordered by
NAME, so v1.9.0 came before v1.10.0 — and read only the last 20 commits, of
which it sent 15. A larger release was described from a part of it.
"""

from __future__ import annotations

from unittest.mock import patch

from app.handlers.comments import reviewer as rv


def _gh(n_commits):
    calls = []

    def gh(path, token, *a, **kw):
        calls.append(path)
        if "/tags" in path:
            return [{"name": "v1.9.0", "commit": {"sha": "old"}},
                    {"name": "v1.10.0", "commit": {"sha": "new"}}]
        if "/compare/" in path:
            return {"commits": [{"sha": f"s{i}", "commit": {"message": f"change {i}"}}
                                for i in range(n_commits)], "total_commits": n_commits}
        raise AssertionError(f"unexpected {path}")

    return gh, calls


def _run(n):
    gh, calls = _gh(n)
    prompts = []
    with patch.object(rv, "gh_get", side_effect=gh), \
         patch.object(rv.router, "ask_text",
                      side_effect=lambda s, u, **k: (prompts.append(u), ("## [1.10.1]", "m"))[1]):
        out = rv.cmd_changelog("o/r", "t")
    return out, prompts, calls


def test_the_highest_version_is_the_base_not_the_first_listed():
    _out, prompts, calls = _run(3)
    assert any("/compare/v1.10.0...HEAD" in c for c in calls)
    assert "version after v1.10.0" in prompts[0]


def test_every_commit_since_the_tag_is_sent():
    _out, prompts, _calls = _run(60)
    for i in (0, 30, 59):
        assert f"change {i}" in prompts[0]


def test_beyond_a_hundred_the_rest_are_counted_not_dropped():
    _out, prompts, _calls = _run(130)
    assert "(and 30 older commits not listed)" in prompts[0]


def test_apply_lists_autofix_branches_beyond_the_first_hundred():
    """It filtered the first 100 branches and answered "No Autofix Branches
    Found" on a larger repository."""
    from app.handlers.comments import publisher as P

    def gh(path, token, *a, **kw):
        if path == "/repos/o/r":
            return {"default_branch": "main"}
        if path.endswith("/git/matching-refs/heads/fix/bot-issue-"):
            return [{"ref": f"refs/heads/fix/bot-issue-{i}"} for i in range(12)]
        raise AssertionError(f"read {path}")

    with patch.object(P, "gh_get", side_effect=gh):
        out = P.cmd_apply("o/r", 1, "t", "")
    assert "`fix/bot-issue-0`" in out and "… and 2 more" in out


def test_release_notes_are_told_when_they_see_part_of_the_release():
    """The notes are written from the latest 40 subjects; the model was not
    told there were more, so it described them as the whole release."""
    from app.handlers.comments import publisher as P

    since = [{"sha": f"s{i}", "commit": {"message": f"fix: change {i}"}} for i in range(55)]

    def gh(path, token, *a, **kw):
        if "/tags" in path:
            return [{"name": "v1.0.0", "commit": {"sha": "t"}}]
        if "/compare/" in path:
            return {"commits": since, "total_commits": 55}
        raise AssertionError(path)

    prompts = []
    # The package-level name: publisher and reviewer both delegate to it.
    with patch("app.handlers.comments.gh_get", side_effect=gh), \
         patch("app.handlers.comments.router.ask",
               side_effect=lambda s, u, **k: (prompts.append(u), ({"error": "stop"}, None))[1]), \
         patch.object(P, "gh_post", return_value={"html_url": "u"}):
        P.cmd_release("o/r", "t", "maint")
    assert "latest 40 of 55 commits" in prompts[0]


def test_a_tag_not_reachable_from_head_falls_back_to_the_commit_list():
    """compare/<tag>...HEAD answers 404 for a tag not in HEAD's history; that
    failed the whole command instead of reading the list."""
    from app.github.client import GitHubError

    def gh(path, token, *a, **kw):
        if "/tags" in path:
            return [{"name": "v1.2.0", "commit": {"sha": "TAG"}}]
        if "/compare/" in path:
            raise GitHubError(f"Not found: {path}", 404)
        return [{"sha": "NEW", "commit": {"message": "unreleased"}},
                {"sha": "TAG", "commit": {"message": "shipped"}}]

    prompts = []
    with patch.object(rv, "gh_get", side_effect=gh), \
         patch.object(rv.router, "ask_text",
                      side_effect=lambda s, u, **k: (prompts.append(u), ("## [1.2.1]", "m"))[1]):
        out = rv.cmd_changelog("o/r", "t")
    assert "CHANGELOG Entry" in out
    assert "unreleased" in prompts[0] and "shipped" not in prompts[0]


def test_a_refusal_is_not_published_as_a_changelog():
    gh, _calls = _gh(3)
    with patch.object(rv, "gh_get", side_effect=gh), \
         patch.object(rv.router, "ask_text", return_value=("I'm sorry, I can't help with that.", "m")):
        out = rv.cmd_changelog("o/r", "t")
    assert out.startswith("## ⚠️ No Changelog Entry Produced")
    assert "I'm sorry" not in out


def test_commit_suggestions_that_fail_the_convention_are_not_posted():
    from app.handlers.commit_message import _clean_suggestion

    assert _clean_suggestion("fix: fix: handle null user") == "fix: handle null user"
    assert _clean_suggestion("feat(api): add /ping\n\nbody") == "feat(api): add /ping"
    assert _clean_suggestion("Fixed the thing") == ""
    assert _clean_suggestion("") == ""


def test_mcp_analyze_pr_reports_nothing_from_an_unusable_answer():
    from app.mcp import handlers as H

    with patch("app.ai.router.router.ask", return_value=({"raw": "nope"}, None)), \
         patch("app.github.auth.get_installation_token", return_value="t"), \
         patch("app.github.client.gh_get", return_value={"title": "t"}), \
         patch("app.github.helpers.pr_files", return_value=[]), \
         patch.object(H, "_installation_allowed", return_value=True):
        out = H._handle_analyze_pr({"repo": "o/r", "pr_number": 1, "installation_id": 1})
    assert out.startswith("Error: the analysis of PR #1 could not be used")
