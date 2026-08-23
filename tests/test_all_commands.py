"""
tests/test_all_commands.py

Every registered command, driven through the real dispatcher with plausible
GitHub and LLM responses.

Nothing exercised the commands as a SET. Each had unit tests over its own
helpers, and those passed throughout the period when seven of them
(/autofix, /apply, /merge, /rollback, /release, /ignore, /secfull) were
refusing to run for the repository owner because a 403 from the collaborator
API was cached as "no permission". A per-command unit test cannot see that,
and neither can a test that stops at the registry.

What this asserts is deliberately shallow and broad: for all 27 commands, the
dispatcher reaches a handler, the handler survives realistic payloads, and what
comes back is markdown a maintainer could act on rather than an error banner.
Depth belongs in the per-command suites; this is the sweep that notices when
one of them quietly stops working.

The GitHub double answers by PATH, so it stays correct as handlers change which
endpoints they call — the earlier version of this harness returned a list for
/check-runs and produced a failure that looked exactly like a product bug.
"""

from __future__ import annotations

import base64  # noqa: F401  (used by the path-shaped GitHub double)
import contextlib
import re
from unittest.mock import MagicMock, patch

import pytest

from app.ai.providers.base import LLMResponse
from app.core.commands import ALL_COMMANDS
from app.handlers.comments.service import _dispatch


PATCH = "@@ -1,3 +1,4 @@\n context\n-old\n+new line\n more\n"

def gh_get(path, token=None, *a, **kw):
    p = path.split("?")[0]
    if re.fullmatch(r"/repos/[^/]+/[^/]+", p):
        return {"full_name": "o/r", "default_branch": "main", "language": "Python",
                "archived": False, "description": "d", "license": {"spdx_id": "MIT"},
                "open_issues_count": 3, "stargazers_count": 5, "size": 100,
                "created_at": "2024-01-01T00:00:00Z", "pushed_at": "2026-01-01T00:00:00Z"}
    if p.endswith("/pulls") or "/pulls?" in path:
        return [{"number": 1, "title": "t", "user": {"login": "u"}, "draft": False,
                 "head": {"ref": "f", "sha": "a"*40}, "base": {"ref": "main"}, "created_at": "2026-01-01T00:00:00Z"}]
    if re.search(r"/pulls/\d+/files", p):
        return [{"filename": "app/x.py", "patch": PATCH, "additions": 2, "deletions": 1, "status": "modified"}]
    if re.search(r"/pulls/\d+/reviews", p):
        return [{"state": "APPROVED", "user": {"login": "r"}}]
    if re.search(r"/pulls/\d+/commits", p):
        return [{"sha": "a"*40, "commit": {"message": "feat: x"}}]
    if re.search(r"/pulls/\d+$", p):
        return {"number": 1, "title": "t", "body": "b", "mergeable": True, "merged": False,
                "mergeable_state": "clean", "draft": False, "user": {"login": "u"},
                "head": {"ref": "f", "sha": "a"*40}, "base": {"ref": "main"},
                "additions": 2, "deletions": 1, "changed_files": 1, "commits": 1}
    if "/issues/" in p and p.endswith("/comments"):
        return [{"user": {"login": "u"}, "body": "a comment", "created_at": "2026-01-01T00:00:00Z"}]
    if re.search(r"/issues/\d+$", p):
        return {"number": 1, "title": "t", "body": "b", "user": {"login": "u"},
                "labels": [{"name": "bug"}], "state": "open", "created_at": "2026-01-01T00:00:00Z"}
    if p.endswith("/issues"):
        return [{"number": 1, "title": "t", "user": {"login": "u"}, "state": "open",
                 "labels": [], "created_at": "2026-01-01T00:00:00Z"}]
    if "/contents/" in p:
        import base64
        return {"content": base64.b64encode(b"flask==3.0.0\nrequests==2.32.0\n").decode(),
                "encoding": "base64", "sha": "b"*40, "path": "requirements.txt"}
    if "/check-runs" in p:
        return {"check_runs": [{"name": "test", "conclusion": "success", "status": "completed"}]}
    if "/commits" in p:
        return [{"sha": "a"*40, "commit": {"message": "feat: x", "author": {"date": "2026-01-01T00:00:00Z", "name": "u"}},
                 "author": {"login": "u"}}]
    if "/actions/runs" in p:
        return {"workflow_runs": [{"id": 1, "name": "CI", "status": "completed", "conclusion": "success",
                                   "head_sha": "a"*40, "html_url": "u", "created_at": "2026-01-01T00:00:00Z",
                                   "head_branch": "main"}]}
    if "/actions/workflows" in p:
        return {"workflows": [{"id": 1, "name": "CI", "path": ".github/workflows/ci.yml", "state": "active"}]}
    if "/check-runs" in p:
        return {"check_runs": [{"name": "test", "conclusion": "success", "status": "completed"}]}
    if "/releases" in p:
        return [{"tag_name": "v7.1.0", "name": "v7.1.0", "published_at": "2026-01-01T00:00:00Z"}]
    if "/tags" in p:
        return [{"name": "v7.1.0"}]
    if "/branches" in p:
        return [{"name": "fix/bot-issue-1"}]
    if "/labels" in p:
        return [{"name": "bug"}]
    if "alerts" in p:
        return []
    if "/languages" in p:
        return {"Python": 1000}
    return {}

def gh_write(path, token=None, data=None, *a, **kw):
    return {"number": 2, "html_url": "https://github.com/o/r/pull/2", "sha": "c"*40,
            "merged": True, "id": 1, "tag_name": "v7.2.0"}

JSON = {"summary": "s", "verdict": "s", "score": 8, "issues": [], "confidence": 0.9,
        "suggested_title": "feat: x", "description": "## Summary\n\nx", "risk_level": "low",
        "risk_reason": "small", "review_focus": ["a"], "type": "bug", "priority": "medium",
        "complexity": "simple", "labels": ["bug"], "welcome": "hi", "needs_info": False,
        "questions": [], "root_cause": "rc", "fix_steps": ["s1"], "files_to_change": ["app/x.py"],
        "explanation": "e", "improvements": [{"area": "a", "suggestion": "s", "example": "e"}],
        "tests": [{"name": "t", "type": "unit", "desc": "d", "code": "c"}], "framework": "pytest",
        "has_gaps": False, "coverage_score": 8, "gaps": [], "impact": "low", "affected": ["x"],
        "time_estimate": "1-4 hours", "target_file": "app/x.py", "commit_message": "fix: x",
        "changes": [{"find": "old", "replace": "new"}], "docs": "d", "sections": []}
META = LLMResponse(text="ok", provider="groq_70b", model="m", total_tokens=10)

issue = {"title": "t", "body": "b", "number": 1, "user": {"login": "u"}, "pull_request": {"url": "x"}}
cfg = MagicMock()
cfg.footer = ""
cfg.get.return_value = 4
cfg.auto_merge_enabled.return_value = True
cfg.auto_merge_risk_ok.return_value = True

targets = ["app.github.client.gh_get", "app.handlers.comments.gh_get",
           "app.handlers.comments.publisher.gh_get", "app.handlers.comments.reviewer.gh_get",
           "app.handlers.comments.security.gh_get", "app.handlers.comments.integrations.gh_get",
           "app.handlers.autofix.gh_get", "app.core.snapshot.gh_get"]
writes = ["app.github.client.gh_post", "app.github.client.gh_put", "app.github.client.gh_patch",
          "app.github.client.gh_delete",
          "app.handlers.comments.gh_post", "app.handlers.comments.gh_put",
          "app.handlers.comments.gh_patch", "app.handlers.comments.gh_delete",
          "app.handlers.comments.publisher.gh_post", "app.handlers.comments.publisher.gh_put",
          "app.handlers.comments.integrations.gh_post",
          "app.handlers.autofix.gh_post", "app.handlers.autofix.gh_put"]


READ_TARGETS = targets
WRITE_TARGETS = writes


@contextlib.contextmanager
def _offline():
    """Patch every GitHub and LLM entry point a command might reach."""
    with contextlib.ExitStack() as st:
        for target in READ_TARGETS:
            with contextlib.suppress(AttributeError, ModuleNotFoundError):
                st.enter_context(patch(target, side_effect=gh_get))
        for target in WRITE_TARGETS:
            with contextlib.suppress(AttributeError, ModuleNotFoundError):
                st.enter_context(patch(target, side_effect=gh_write))
        st.enter_context(patch("app.ai.router.router.ask", return_value=(dict(JSON), META)))
        st.enter_context(patch("app.ai.router.router.ask_text", return_value=("text out", META)))
        yield


def _run(cmd: str, **over):
    kwargs = {
        "cmd": cmd, "cmd_args": "", "context": "ctx", "repo": "o/r", "issue_number": 1,
        "issue": issue, "token": "tok", "author": "u", "config": cfg, "log_ctx": MagicMock(),
    }
    kwargs.update(over)
    with _offline():
        return _dispatch(**kwargs)


class TestEveryCommandProducesUsableOutput:
    @pytest.mark.parametrize("cmd", sorted(ALL_COMMANDS))
    def test_command_returns_actionable_markdown(self, cmd):
        out = _run(cmd)

        assert out is not None, f"{cmd} fell through to the unknown-command branch"
        assert not isinstance(out, dict), f"{cmd} returned the providers-down sentinel"
        assert isinstance(out, str) and out.strip(), f"{cmd} returned nothing"
        assert not re.search(r"(failed|error)", out, re.I), (
            f"{cmd} returned an error banner:\n{out[:300]}"
        )

    @pytest.mark.parametrize("cmd", sorted(ALL_COMMANDS))
    def test_command_never_raises(self, cmd):
        """_dispatch converts exceptions into an error string, so a raise here
        would mean something escaped even that."""
        _run(cmd)


class TestTheSweepItselfIsHonest:
    """A harness that silently stops covering things is worse than none."""

    def test_it_covers_the_whole_registry(self):
        assert len(ALL_COMMANDS) == 27, (
            f"registry is {len(ALL_COMMANDS)} commands — update this sweep deliberately"
        )

    def test_the_github_double_is_never_bypassed(self):
        """If a handler reaches the real client the test is measuring the
        network, not the code. An unmocked write is what made /merge look
        broken while it was fine."""
        from app.github import client

        with _offline(), patch.object(client, "_session") as session:
            for cmd in sorted(ALL_COMMANDS):
                _run(cmd)
            assert not session.method_calls, (
                f"real HTTP attempted: {session.method_calls[:3]}"
            )

    def test_an_unknown_command_is_reported_not_guessed(self):
        assert _run("/definitely-not-a-command") is None
