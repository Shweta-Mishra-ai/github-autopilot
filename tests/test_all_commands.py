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
    if "/git/matching-refs/heads/fix/bot-issue-" in p:
        return [{"ref": "refs/heads/fix/bot-issue-1-1700000000"}]
    if "/git/ref/" in p or "/git/refs/" in p:
        return {"ref": "refs/heads/main", "object": {"sha": "a" * 40, "type": "commit"}}
    if "/commits" in p:
        return [{"sha": "a"*40, "commit": {"message": "feat: export orders", "author": {"date": "2026-01-01T00:00:00Z", "name": "u"}},
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
        "changes": [{"find": "flask==3.0.0", "replace": "flask==3.0.3"}],
        "edits": [{"find": "flask==3.0.0", "replace": "flask==3.0.3"}], "docs": "d", "sections": []}
META = LLMResponse(text="ok", provider="groq_70b", model="m", total_tokens=10)
# A plain-text answer that is also a well-formed changelog entry, so /changelog
# can reach its success path; the other text commands render it as prose.
TEXT = "## [v7.2.0] - 2026-01-01\n\n### Added\n- Export orders"

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
        # /notify has a channel to deliver to, and delivery is answered locally.
        st.enter_context(patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": "https://discord.example/api/webhooks/1/x"}))
        st.enter_context(
            patch("app.github.notifications.requests.post", return_value=MagicMock(status_code=204, text=""))
        )
        st.enter_context(patch("app.ai.router.router.ask", return_value=(dict(JSON), META)))
        st.enter_context(patch("app.ai.router.router.ask_text", return_value=(TEXT, META)))
        yield


def _run(cmd: str, **over):
    kwargs = {
        "cmd": cmd, "cmd_args": "", "context": "ctx", "repo": "o/r", "issue_number": 1,
        "issue": issue, "token": "tok", "author": "u", "config": cfg, "log_ctx": MagicMock(),
    }
    kwargs.update(over)
    with _offline():
        return _dispatch(**kwargs)


# The heading each command answers with under these fixtures. The sweep
# checked only that an answer was non-empty and did not contain "failed" or
# "error" — so "Autofix Skipped", "No Changelog Entry Produced" and "No
# Autofix Branches Found" all passed as usable output, and they were what the
# fixtures actually produced. Pinning the heading means a command that drops
# to a refusal, a degraded reply or an error banner fails here by name.
#
# /report and /rollback answer "nothing recorded yet": their stores are empty
# in a fresh test process, and that is the honest answer for them.
EXPECTED_HEADINGS = {
    "/apply": "## 🌱 Available Autofix Branches",
    "/arch": "## 🏗️ Architecture Review",
    "/autofix": "## 🤖 Autofix Ready",
    "/budget": "## 💰 LLM Budget",
    "/changelog": "## 📋 CHANGELOG Entry",
    "/ci": "## 🔴 CI Failure Analysis",
    "/docs": "## 📚 Documentation",
    "/explain": "## 💡 Explanation",
    "/fix": "## 🔧 Fix",
    "/gaps": "## 🔍 Test Coverage Gaps",
    "/health": "## 🏥 Repo Health",
    "/ignore": "## 🙈 Ignored Rule",
    "/impact": "## 💥 Blast Radius",
    "/improve": "## ✨ Improvements",
    "/merge": "## ✅ Merged!",
    "/notify": "## 🔔",
    "/perf": "## ⚡ Performance Analysis",
    "/refactor": "## ♻️ Refactor",
    "/release": "## 🚀 Draft Release Created",
    "/report": "## 📊 No Data Yet",
    "/rollback": "## 📸 No Snapshots Available",
    "/runtests": "## 🧪 Tests Triggered",
    "/secfull": "## 🔒 Security Report",
    "/security": "## 🔒 Security Scan Results",
    "/summarize": "## 📝 Thread Summary",
    "/test": "## 🧪 Tests",
    "/version": "## 🎛️ Version Status",
}


class TestEveryCommandProducesUsableOutput:
    @pytest.mark.parametrize("cmd", sorted(ALL_COMMANDS))
    def test_command_answers_with_its_own_heading(self, cmd):
        out = _run(cmd)

        assert out is not None, f"{cmd} fell through to the unknown-command branch"
        assert not isinstance(out, dict), f"{cmd} returned the providers-down sentinel"
        assert isinstance(out, str), f"{cmd} returned {type(out).__name__}"
        assert out.strip().startswith(EXPECTED_HEADINGS[cmd]), (
            f"{cmd} answered with a different outcome:\n{out[:300]}"
        )

    # test_command_never_raises was removed: _dispatch converts every
    # exception into a reply, so it could not fail; an exception now shows up
    # above as the wrong heading.


class TestTheSweepItselfIsHonest:
    """A harness that silently stops covering things is worse than none."""

    def test_it_covers_the_whole_registry(self):
        assert set(EXPECTED_HEADINGS) == set(ALL_COMMANDS), (
            "a command was added or removed — give it an expected heading here"
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


class TestEveryRestrictedCommandIsGatedEndToEnd:
    """
    The sweep above calls _dispatch directly, below the permission check, so
    it says nothing about who may run what. This drives each restricted
    command through the real comment handler as a commenter GitHub reports
    as "read", and requires a refusal with no command run.
    """

    @pytest.mark.parametrize("cmd", sorted(__import__("app.core.commands", fromlist=["x"]).RESTRICTED_COMMANDS))
    def test_a_read_only_commenter_is_refused(self, cmd):
        from app.core import authorization
        from app.core.config import Config
        from app.handlers.comments import service

        authorization._perm_cache.clear()
        posted = []
        payload = {
            "action": "created",
            "comment": {"body": f"{cmd} 1", "user": {"login": "stranger"}},
            "issue": {"number": 1, "title": "t", "body": "b", "user": {"login": "u"}},
            "repository": {"full_name": "o/r"},
            "installation": {"id": 1},
            "sender": {"login": "stranger", "type": "User"},
        }
        with patch.object(service, "get_installation_token", return_value="tok"), \
             patch.object(service, "load_config", return_value=Config({})), \
             patch.object(service, "check_user_rate_limit", return_value=True), \
             patch.object(authorization, "gh_get", return_value={"permission": "read"}), \
             patch.object(service, "_dispatch") as dispatch, \
             patch.object(service, "_post_comment", side_effect=lambda r, n, t, body, *a, **k: posted.append(body)):
            service.handle_comment_event(payload)

        dispatch.assert_not_called()
        assert len(posted) == 1 and "Not Run" in posted[0], posted
