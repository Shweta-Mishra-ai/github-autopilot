"""
tests/test_commands_fixed.py
Comprehensive tests for fixed commands:
  - Command matching (word-boundary fix)
  - /autofix (expanded blocked paths, human confirmation, diff preview)
  - /release (draft creation, error handling)
  - /runtests (workflow dispatch, no-workflow case)
  - /notify (discord success/failure)
  - /report (analytics integration)

All external I/O mocked. No network calls.
"""
import sys
import os
import re
import base64
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The real requests: it is a hard dependency, always installed where tests
# run. A MagicMock installed here with setdefault became `requests` for the
# whole session whenever this file was collected first, and every later
# test that touched HTTP failed with "MagicMock has no attribute 'request'".
import requests  # noqa: E402,F401

# Mock ONLY genuinely-missing deps. This list used to blanket-mock flask,
# which (as the alphabetically-first offender at collection time) poisoned
# sys.modules for every later test file and permanently skipped 21
# real-Flask tests. Real module available → use it; missing → mock it.
import importlib

for _mod in ["structlog", "redis", "groq", "google", "google.generativeai",
             "flask_limiter", "flask_limiter.util", "apscheduler",
             "apscheduler.schedulers", "apscheduler.schedulers.background",
             "sentence_transformers", "qdrant_client", "scipy",
             "flask", "flask.logging"]:
    try:
        importlib.import_module(_mod)
    except ImportError:
        sys.modules.setdefault(_mod, MagicMock())

# ─── Helpers ─────────────────────────────────────────────────────────────────

ALL_COMMANDS = sorted({
    "/apply", "/arch", "/autofix", "/budget", "/changelog",
    "/ci", "/docs", "/explain", "/fix", "/gaps",
    "/health", "/impact", "/improve", "/merge", "/notify",
    "/perf", "/refactor", "/release", "/report", "/rollback",
    "/runtests", "/secfull", "/security", "/summarize", "/test",
    "/version",
})


# These tests used to exercise a COPY of the extractor defined here in the
# test file, so they passed whatever the application did. They now test the
# real one.
from app.handlers.comments.dispatcher import extract_command as _extract_command  # noqa: E402


def _mock_llm(resp=None, tokens=50):
    from app.ai.providers.base import LLMResponse
    meta = LLMResponse(text="ok", provider="groq", model="llama3", total_tokens=tokens)
    return (resp or {}, meta)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. COMMAND MATCHING TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestCommandMatching:
    """Tests for _extract_command word-boundary fix."""

    def test_autofix_matches_correctly(self):
        assert _extract_command("/autofix this bug") == "/autofix"

    def test_fix_matches_correctly(self):
        assert _extract_command("/fix the crash") == "/fix"

    def test_release_matches_correctly(self):
        assert _extract_command("/release now") == "/release"

    def test_release_mentioned_in_prose_does_not_run(self):
        assert _extract_command("please /release now") is None

    def test_runtests_matches_correctly(self):
        assert _extract_command("/runtests please") == "/runtests"

    def test_acts_email_not_matched(self):
        """Email address with 'acts' should NOT trigger any command."""
        assert _extract_command("contact us at acts@company.com") is None

    def test_proactive_not_matched(self):
        """'proactive' contains 'act' — should NOT match."""
        assert _extract_command("proactive approach needed") is None

    def test_inline_text_after_command(self):
        assert _extract_command("/health check now") == "/health"

    def test_command_in_middle_of_sentence_does_not_run(self):
        """Prose that mentions a command is not an instruction."""
        assert _extract_command("please run /runtests on this branch") is None

    def test_command_after_a_mention_runs(self):
        assert _extract_command("@github-autopilot /runtests") == "/runtests"

    def test_command_on_a_later_line_runs(self):
        assert _extract_command("Looks good.\n/runtests") == "/runtests"

    def test_no_command_returns_none(self):
        assert _extract_command("just a normal comment") is None

    def test_case_insensitive(self):
        assert _extract_command("/AUTOFIX this") == "/autofix"

    def test_empty_body(self):
        assert _extract_command("") is None

    def test_multiple_commands_first_wins(self):
        # Sorted order: /autofix comes before /fix alphabetically
        result = _extract_command("/autofix and /fix")
        assert result in ("/autofix", "/fix")  # either is acceptable

    # ── Regression: old bug would have failed these ──────────────────────

    def test_autofix_not_matched_as_apply_OLD_BUG(self):
        """OLD bug: '/autofix' matched '/apply' first due to ALL_COMMANDS sort."""
        # Verify old code would have given different result in edge cases
        # The new extractor must correctly get /autofix
        assert _extract_command("/autofix the null pointer") == "/autofix"

    def test_a_reference_to_a_command_does_not_run_it(self):
        """This used to cut a release."""
        assert _extract_command("See /release for details") is None

    def test_fixture_is_not_fix(self):
        assert _extract_command("/fixture update") is None


# ═══════════════════════════════════════════════════════════════════════════════
# 2. AUTOFIX TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestAutofixIsAllowed:
    """Tests for expanded _is_allowed blocked paths."""

    def setup_method(self, method=None):
        from app.handlers.autofix import _is_allowed
        self._fn = _is_allowed

    def test_normal_handler_allowed(self):
        assert self._fn("app/handlers/comments.py") is True

    def test_tests_allowed(self):
        assert self._fn("tests/test_foo.py") is True

    def test_docs_allowed(self):
        assert self._fn("docs/guide.md") is True

    def test_server_blocked(self):
        assert self._fn("server.py") is False

    def test_env_blocked(self):
        assert self._fn(".env") is False

    def test_env_local_blocked(self):
        assert self._fn(".env.local") is False

    def test_env_production_blocked(self):
        assert self._fn(".env.production") is False

    def test_requirements_blocked(self):
        assert self._fn("requirements.txt") is False

    def test_requirements_dev_blocked(self):
        assert self._fn("requirements-dev.txt") is False

    def test_dockerfile_blocked(self):
        assert self._fn("Dockerfile") is False

    def test_docker_compose_blocked(self):
        assert self._fn("docker-compose.yml") is False

    def test_ci_workflow_blocked(self):
        assert self._fn(".github/workflows/ci.yml") is False

    def test_any_workflow_blocked(self):
        assert self._fn(".github/workflows/deploy.yml") is False

    def test_config_py_blocked(self):
        assert self._fn("app/core/config.py") is False

    def test_authorization_blocked(self):
        assert self._fn("app/core/authorization.py") is False

    def test_webhook_security_blocked(self):
        assert self._fn("app/core/webhook_security.py") is False

    def test_auth_py_blocked(self):
        assert self._fn("app/github/auth.py") is False

    def test_empty_path_blocked(self):
        assert self._fn("") is False

    def test_no_extension_blocked(self):
        assert self._fn("Makefile") is False

    def test_path_traversal_blocked(self):
        assert self._fn("../../etc/passwd") is False

    def test_absolute_path_blocked(self):
        assert self._fn("/etc/passwd") is False

    def test_pyproject_blocked(self):
        assert self._fn("pyproject.toml") is False


class TestAutofixDiffPreview:
    """Tests for _make_diff_preview function."""

    def test_diff_shows_changes(self):
        from app.handlers.autofix import _make_diff_preview
        orig  = "def foo():\n    return None\n"
        fixed = "def foo():\n    return 42\n"
        diff  = _make_diff_preview(orig, fixed, "app/foo.py")
        assert "```diff" in diff
        assert "- " in diff or "+ " in diff

    def test_diff_shows_filename(self):
        from app.handlers.autofix import _make_diff_preview
        diff = _make_diff_preview("a", "b", "app/foo.py")
        assert "app/foo.py" in diff

    def test_diff_added_removed_counts(self):
        from app.handlers.autofix import _make_diff_preview
        orig  = "line1\nline2\n"
        fixed = "line1\nline2\nline3\n"
        diff  = _make_diff_preview(orig, fixed, "foo.py")
        assert "+1" in diff or "+ " in diff


class TestRunAutofix:
    """Integration tests for run_autofix flow."""

    def _issue(self):
        return {"title": "Fix null crash", "body": "crashes when None passed"}

    def _plan(self, target="app/foo.py"):
        return {
            "target_file": target,
            "pr_title": "fix null",
            "commit_message": "fix: null check",
            "problem": "null pointer",
            "fix_description": "add null guard",
            "explanation": "prevents crash",
            "patch": "if x is None: return",
            "confidence": 0.9,
        }

    def test_no_fix_plan_returns_helpful_message(self):
        from app.handlers.autofix import run_autofix
        from app.ai.providers.base import LLMResponse
        meta = LLMResponse(text="", provider="groq", model="llama3", total_tokens=10)
        with patch("app.handlers.autofix.router.ask",
                   return_value=({"confidence": 0.1}, meta)):
            result = run_autofix("test/repo", 1, self._issue(), "token")
        assert "Failed" in result
        assert "/fix" in result  # Should suggest alternative

    def test_blocked_file_returns_skipped_with_reason(self):
        from app.handlers.autofix import run_autofix
        from app.ai.providers.base import LLMResponse
        meta = LLMResponse(text="", provider="groq", model="llama3", total_tokens=10)
        plan = self._plan(target="requirements.txt")
        with patch("app.handlers.autofix.router.ask", return_value=(plan, meta)):
            result = run_autofix("test/repo", 1, self._issue(), "token")
        assert "Skipped" in result or "Blocked" in result

    def test_ci_workflow_blocked(self):
        from app.handlers.autofix import run_autofix
        from app.ai.providers.base import LLMResponse
        meta = LLMResponse(text="", provider="groq", model="llama3", total_tokens=10)
        plan = self._plan(target=".github/workflows/ci.yml")
        with patch("app.handlers.autofix.router.ask", return_value=(plan, meta)):
            result = run_autofix("test/repo", 1, self._issue(), "token")
        assert "Skipped" in result or "Blocked" in result

    def test_path_traversal_rejected(self):
        from app.handlers.autofix import run_autofix
        from app.ai.providers.base import LLMResponse
        meta = LLMResponse(text="", provider="groq", model="llama3", total_tokens=10)
        plan = self._plan(target="../../etc/passwd")
        with patch("app.handlers.autofix.router.ask", return_value=(plan, meta)):
            result = run_autofix("test/repo", 1, self._issue(), "token")
        assert "Blocked" in result or "Skipped" in result or "traversal" in result.lower()

    def test_full_flow_posts_diff_not_pr(self):
        """After fix: should post diff for review, NOT auto-create PR."""
        from app.handlers.autofix import run_autofix
        from app.ai.providers.base import LLMResponse
        meta = LLMResponse(text="ok", provider="groq", model="llama3", total_tokens=50)
        plan = self._plan(target="app/foo.py")
        fix_resp = {
            "edits": [
                {"find": "    return x.strip()", "replace": "    if x is None: return\n    return x.strip()"}
            ],
        }
        orig_content = b"def foo():\n    return x.strip()\n"

        with patch("app.handlers.autofix.router.ask",
                   side_effect=[(plan, meta), (fix_resp, meta)]), \
             patch("app.handlers.autofix.gh_get") as mock_get:
                mock_get.side_effect = [
                    {
                        "content": base64.b64encode(orig_content).decode(),
                        "sha": "abc123",
                    },
                    {"default_branch": "main"},
                    {"object": {"sha": "def456"}},
                ]
                with patch("app.handlers.autofix.gh_put", return_value={}), \
                     patch("app.handlers.autofix.gh_post", return_value={}):
                        result = run_autofix("test/repo", 1, self._issue(), "token")

        # Should contain diff preview and confirmation instructions
        assert "diff" in result.lower() or "Diff" in result
        assert "/apply" in result  # confirmation instruction
        # Should NOT say "PR created" or "✅ Autofix Complete" (that's the old behavior)
        assert "PR #" not in result

    def test_no_change_returns_skipped(self):
        from app.handlers.autofix import run_autofix
        from app.ai.providers.base import LLMResponse
        meta = LLMResponse(text="ok", provider="groq", model="llama3", total_tokens=50)
        plan = self._plan(target="app/foo.py")
        orig_content = b"def foo(): return 42\n"
        # LLM returns same content — no change
        fix_resp = {"fixed_content": "def foo(): return 42\n", "changed_lines": 0}

        with patch("app.handlers.autofix.router.ask",
                   side_effect=[(plan, meta), (fix_resp, meta)]), \
             patch("app.handlers.autofix.gh_get", return_value={
                 "content": base64.b64encode(orig_content).decode(),
                 "sha": "abc123",
             }):
                result = run_autofix("test/repo", 1, self._issue(), "token")

        assert "Skipped" in result


# ═══════════════════════════════════════════════════════════════════════════════
# 3. /release TESTS
# ═══════════════════════════════════════════════════════════════════════════════

def _release_gh(tags, since_tag, commits=()):
    """GitHub, by route: the tags, the compare against the latest tag, the
    branch commits (used only when there is no tag)."""
    seen = []

    def get(path, token):
        seen.append(path)
        if "/tags" in path:
            return tags
        if "/releases" in path:
            return []  # no drafts yet
        if "/compare/" in path:
            return {"commits": list(since_tag), "total_commits": len(since_tag)}
        if "/commits" in path:
            return list(commits)
        raise AssertionError(f"unexpected GET {path}")

    get.seen = seen
    return get


class TestCmdRelease:
    """Tests for _cmd_release."""

    def _mock_router_text(self, resp):
        from app.ai.providers.base import LLMResponse
        meta = LLMResponse(text="ok", provider="groq", model="llama3", total_tokens=50)
        return (resp, meta)

    def test_creates_draft_release(self):
        from app.handlers.comments import _cmd_release
        plan = {
            "version": "v1.2.0",
            "title": "Feature release",
            "highlights": ["Added /release command", "Fixed /autofix"],
            "breaking_changes": [],
            "release_notes": "## What's New\n- Added stuff",
        }
        with patch("app.handlers.comments.router.ask",
                   return_value=self._mock_router_text(plan)), \
             patch("app.handlers.comments.gh_get", side_effect=_release_gh(
                 [{"name": "v1.1.0"}],
                 [{"commit": {"message": "feat: add thing"}}] * 5,  # since v1.1.0
             )), \
             patch("app.handlers.comments.gh_post", return_value={
                 "html_url": "https://github.com/test/repo/releases/1",
                 "number": 1,
             }) as mock_post:
                result = _cmd_release("test/repo", "token", "author")

        assert "Draft" in result or "draft" in result
        assert "v1.2.0" in result
        # Confirm it creates a DRAFT (not published)
        call_kwargs = mock_post.call_args[0][2]  # third positional arg is the body dict
        assert call_kwargs.get("draft") is True

    def test_release_with_no_tags_uses_v000(self):
        from app.handlers.comments import _cmd_release
        plan = {
            "version": "v0.0.1",
            "title": "First release",
            "highlights": [],
            "release_notes": "Initial release",
        }
        with patch("app.handlers.comments.router.ask",
                   return_value=self._mock_router_text(plan)), \
             patch("app.handlers.comments.gh_get", side_effect=_release_gh(
                 [],  # no tags
                 [],
                 commits=[{"commit": {"message": "init"}}],
             )), \
             patch("app.handlers.comments.gh_post", return_value={
                 "html_url": "https://github.com/test/repo/releases/1",
             }):
                result = _cmd_release("test/repo", "token", "author")

        # Not `"Release" in result`: "Release failed" contains it too.
        assert result.startswith("## 🚀 Draft Release Created"), result
        assert "`v0.0.1`" in result

    def test_a_second_release_does_not_draft_the_same_version_again(self):
        """Round-4 audit: a draft creates no tag, so /release twice drafted
        the same version twice."""
        from app.handlers.comments import _cmd_release

        def gh(path, token):
            if "/tags" in path:
                return [{"name": "v1.2.3"}]
            if "/compare/" in path:
                return {"commits": [{"commit": {"message": "fix: x"}}], "total_commits": 1}
            if "/releases" in path:
                return [{"tag_name": "v1.2.4", "draft": True, "html_url": "https://r/1"}]
            raise AssertionError(path)

        with patch("app.handlers.comments.gh_get", side_effect=gh), \
             patch("app.handlers.comments.gh_post") as post:
            result = _cmd_release("test/repo", "token", "author")
        post.assert_not_called()
        assert "Draft Already Exists" in result and "https://r/1" in result

    def test_release_github_api_failure(self):
        from app.handlers.comments import _cmd_release
        with patch("app.handlers.comments.gh_get", side_effect=Exception("API error")):
            result = _cmd_release("test/repo", "token", "author")
        assert "⚠️" in result or "failed" in result.lower()

    def test_release_shows_view_link(self):
        from app.handlers.comments import _cmd_release
        plan = {
            "version": "v2.0.0",
            "title": "Major release",
            "highlights": ["Big feature"],
            "release_notes": "## Major changes",
        }
        with patch("app.handlers.comments.router.ask",
                   return_value=self._mock_router_text(plan)), \
             patch("app.handlers.comments.gh_get", side_effect=_release_gh(
                 [{"name": "v1.9.0"}],
                 [{"commit": {"message": "feat: big thing"}}],
             )), \
             patch("app.handlers.comments.gh_post", return_value={
                 "html_url": "https://github.com/test/repo/releases/42",
             }):
                result = _cmd_release("test/repo", "token", "author")

        assert "https://github.com/test/repo/releases/42" in result


# ═══════════════════════════════════════════════════════════════════════════════
# 4. /runtests TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdRuntests:
    """Tests for _cmd_runtests."""

    def _workflows(self, names):
        return {
            "workflows": [
                {"id": i + 1, "name": n, "path": f".github/workflows/{n.lower()}.yml"}
                for i, n in enumerate(names)
            ]
        }

    def test_triggers_ci_workflow(self):
        from app.handlers.comments import _cmd_runtests
        with patch("app.handlers.comments.gh_get", side_effect=[
            {"default_branch": "main"},
            self._workflows(["CI", "Deploy"]),
        ]), patch("app.handlers.comments.gh_post", return_value={}) as mock_post:
            result = _cmd_runtests("test/repo", 1, "token")

        assert "Triggered" in result or "triggered" in result.lower()
        assert mock_post.called

    def test_triggers_test_workflow(self):
        from app.handlers.comments import _cmd_runtests
        with patch("app.handlers.comments.gh_get", side_effect=[
            {"default_branch": "main"},
            self._workflows(["Tests", "Lint"]),
        ]), patch("app.handlers.comments.gh_post", return_value={}):
            result = _cmd_runtests("test/repo", 1, "token")

        assert "Triggered" in result or "🧪" in result

    def test_no_workflow_gives_helpful_message(self):
        from app.handlers.comments import _cmd_runtests
        with patch("app.handlers.comments.gh_get", side_effect=[
            {"default_branch": "main"},
            {"workflows": []},  # no workflows
        ]):
            result = _cmd_runtests("test/repo", 1, "token")

        assert "No Test Workflow" in result or "not found" in result.lower()
        assert "test.yml" in result or "ci.yml" in result  # Helpful hint

    def test_github_api_failure(self):
        from app.handlers.comments import _cmd_runtests
        with patch("app.handlers.comments.gh_get", side_effect=Exception("403 Forbidden")):
            result = _cmd_runtests("test/repo", 1, "token")

        assert "⚠️" in result or "Could not" in result

    def test_shows_workflow_name_in_result(self):
        from app.handlers.comments import _cmd_runtests
        with patch("app.handlers.comments.gh_get", side_effect=[
            {"default_branch": "develop"},
            self._workflows(["CI Checks"]),
        ]), patch("app.handlers.comments.gh_post", return_value={}):
            result = _cmd_runtests("test/repo", 1, "token")

        # Should mention the workflow name or branch
        assert "CI" in result or "develop" in result

    def test_workflow_dispatch_uses_default_branch(self):
        """Dispatches on default_branch, not hardcoded 'main'."""
        from app.handlers.comments import _cmd_runtests
        with patch("app.handlers.comments.gh_get", side_effect=[
            {"default_branch": "master"},  # older repo using master
            self._workflows(["pytest"]),
        ]), \
             patch("app.handlers.comments.gh_post", return_value={}) as mock_post:
                result = _cmd_runtests("test/repo", 1, "token")

        # The dispatch call should use 'master'
        if mock_post.called:
            dispatch_body = mock_post.call_args[0][2]
            assert dispatch_body.get("ref") == "master"


# ═══════════════════════════════════════════════════════════════════════════════
# 5. /notify TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdNotify:

    def _issue(self, is_pr=False, labels=None):
        d = {
            "title": "Test issue",
            "html_url": "https://github.com/test/repo/issues/1",
            "labels": [{"name": lbl} for lbl in (labels or [])],
        }
        if is_pr:
            d["pull_request"] = {}
        return d

    def test_discord_success(self):
        from app.handlers.comments import _cmd_notify
        with patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/x"}), \
             patch("app.github.notifications.send_rich_discord",
                        return_value=(True, "ok")):
                result = _cmd_notify("test/repo", 1, self._issue(), "token", "")
        assert "Sent" in result or "sent" in result.lower()

    def test_discord_failure_shows_env_hint(self):
        from app.handlers.comments import _cmd_notify
        with patch("app.github.notifications.send_rich_discord",
                   return_value=(False, "webhook not configured")):
            result = _cmd_notify("test/repo", 1, self._issue(), "token", "")
        assert "DISCORD_WEBHOOK_URL" in result or "⚠️" in result

    def test_bug_label_uses_red_color(self):
        from app.handlers.comments import _cmd_notify
        captured = {}
        def capture_discord(**kwargs):
            captured.update(kwargs)
            return (True, "ok")
        with patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/x"}), \
             patch("app.github.notifications.send_rich_discord", side_effect=capture_discord):
                _cmd_notify("test/repo", 1, self._issue(labels=["bug"]), "token", "")
        # Red color for bugs
        assert captured.get("color") == 0xE74C3C


# ═══════════════════════════════════════════════════════════════════════════════
# 6. /report TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdReport:

    def test_report_returns_analytics(self):
        from app.handlers.comments import _cmd_report
        with patch("app.core.analytics.format_report_comment",
                   return_value="## 📊 Report\n\nAll good."), \
             patch("app.core.analytics.record_command_used"):
                result = _cmd_report("test/repo")
        assert "Report" in result

    def test_report_failure_handled(self):
        from app.handlers.comments import _cmd_report
        with patch("app.core.analytics.format_report_comment",
                   side_effect=Exception("Redis down")), \
             patch("app.core.analytics.record_command_used"):
                result = _cmd_report("test/repo")
        assert "⚠️" in result


# ═══════════════════════════════════════════════════════════════════════════════
# 7. GENERAL SAFETY TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestGeneralCommandSafety:

    # Both tests below passed for the wrong reason: the payload had no
    # `sender`, so the handler returned at "missing required fields" before
    # the rule under test, and the mint they patched was the package's name,
    # not service.py's. And `edited` is handled now — an edit runs a command
    # when the edit added it — so "non-created action skipped" was false.

    @staticmethod
    def _payload(action="created", body="/fix stuff", sender="user", sender_type="User", before=None):
        p = {
            "action": action,
            "comment": {"body": body, "user": {"login": sender}},
            "repository": {"full_name": "test/repo"},
            "issue": {"number": 1, "title": "t"},
            "installation": {"id": 123},
            "sender": {"login": sender, "type": sender_type},
        }
        if before is not None:
            p["changes"] = {"body": {"from": before}}
        return p

    def _reached_auth(self, payload) -> bool:
        """True when the handler got as far as minting a token."""
        from app.handlers.comments import handle

        with patch(
            "app.handlers.comments.service.get_installation_token",
            side_effect=RuntimeError("reached auth"),
        ) as mint:
            try:
                handle(payload)
            except RuntimeError as e:
                if "reached auth" not in str(e):
                    raise
        return mint.called

    def test_the_payload_reaches_auth_from_a_person(self):
        """The control: without it, every 'skipped' below could be a payload
        the handler rejects for some other reason."""
        assert self._reached_auth(self._payload())

    def test_bot_author_skipped(self):
        """Bot comments must be silently ignored."""
        assert not self._reached_auth(self._payload(sender="dependabot[bot]", sender_type="Bot"))

    def test_an_edit_that_kept_the_command_is_skipped(self):
        assert not self._reached_auth(self._payload(action="edited", body="/fix stuffs", before="/fix stuff"))

    def test_an_edit_that_added_the_command_runs(self):
        assert self._reached_auth(self._payload(action="edited", body="/fix stuff", before="/fxi stuff"))

    def test_a_deleted_comment_is_skipped(self):
        assert not self._reached_auth(self._payload(action="deleted"))


# ─── Runner ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Run with: python -m pytest tests/test_commands_fixed.py -v")



def test_breaking_change_counts_only_as_a_footer_token():
    """Round-4 audit: "This is not a BREAKING CHANGE." in a body bumped the
    major version."""
    from app.handlers.comments.publisher import next_version

    assert next_version("v1.4.2", ["fix: typo\n\nThis is not a BREAKING CHANGE."]) == "v1.4.3"
    assert next_version("v1.4.2", ["fix: api\n\nBREAKING CHANGE: drops /v1"]) == "v2.0.0"
    assert next_version("v1.4.2", ["feat!: new auth"]) == "v2.0.0"


def test_a_merge_commit_is_not_a_feature():
    """Round-4 audit: /merge titled its commit "feat: merge …", so the next
    /release bumped the minor version for a typo fix."""
    from app.handlers.comments.publisher import next_version

    title = "Merge pull request #7 from fix-typo (/merge by maint)"
    assert next_version("v1.4.2", [title, "fix: typo"]) == "v1.4.3"


def test_version_shows_the_highest_tag_and_a_published_release():
    """Round-4 audit: tags[0] is name-ordered (v1.9.0 before v1.10.0) and the
    latest "release" could be a draft or have no name."""
    from app.handlers.comments.reviewer import cmd_version

    def gh(path, token):
        if "/tags" in path:
            return [{"name": "v1.9.0"}, {"name": "v1.10.0"}, {"name": "nightly"}]
        if "/releases" in path:
            return [{"name": "Next", "draft": True, "tag_name": "v1.11.0"},
                    {"name": None, "draft": False, "tag_name": "v1.10.0"}]
        return [{"sha": "a" * 40, "commit": {"message": "fix: x"}}]

    with patch("app.handlers.comments.gh_get", side_effect=gh):
        out = cmd_version("o/r", "t")
    assert "| **Latest Tag** | `v1.10.0` |" in out
    assert "| **Latest Release** | `v1.10.0` |" in out
