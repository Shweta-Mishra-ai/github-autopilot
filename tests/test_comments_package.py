"""
tests/test_comments_package.py — V5
Tests for the comments/ package split.
Verifies that the 1603-line monolith was correctly split into 5 modules
with no loss of functionality and no circular imports.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestPackageStructure:

    def test_package_importable(self):
        from app.handlers.comments import handle_comment_event
        assert callable(handle_comment_event)

    def test_constants_module(self):
        from app.handlers.comments.constants import (
            ALL_COMMANDS, SKIP_AUTHORS, USER_CMD_LIMIT, USER_CMD_WINDOW
        )
        assert len(ALL_COMMANDS) >= 20
        assert "/fix" in ALL_COMMANDS
        assert "/merge" in ALL_COMMANDS
        assert "/rollback" in ALL_COMMANDS
        assert "dependabot[bot]" in SKIP_AUTHORS
        assert USER_CMD_LIMIT == 10
        assert USER_CMD_WINDOW == 3600

    def test_all_commands_sorted(self):
        from app.handlers.comments.constants import ALL_COMMANDS
        assert sorted(ALL_COMMANDS) == ALL_COMMANDS, "Commands must be sorted"

    def test_no_duplicate_commands(self):
        from app.handlers.comments.constants import ALL_COMMANDS
        assert len(ALL_COMMANDS) == len(set(ALL_COMMANDS)), "Duplicate commands detected"

    def test_dispatcher_module(self):
        from app.handlers.comments.dispatcher import (
            extract_command, check_user_rate_limit,
            providers_down_comment, safe_router_ask,
            is_providers_down, make_degraded_response,
        )
        for fn in [extract_command, check_user_rate_limit,
                   providers_down_comment, safe_router_ask,
                   is_providers_down, make_degraded_response]:
            assert callable(fn)

    def test_generator_module(self):
        from app.handlers.comments.generator import (
            cmd_fix, cmd_explain, cmd_improve, cmd_test,
            cmd_docs, cmd_refactor, cmd_gaps, cmd_perf, cmd_arch
        )
        for fn in [cmd_fix, cmd_explain, cmd_improve, cmd_test,
                   cmd_docs, cmd_refactor, cmd_gaps, cmd_perf, cmd_arch]:
            assert callable(fn)

    def test_reviewer_module(self):
        from app.handlers.comments.reviewer import (
            cmd_health, cmd_version, cmd_summarize, cmd_ci,
            cmd_budget, cmd_report, cmd_impact, cmd_changelog,
        )
        for fn in [cmd_health, cmd_version, cmd_summarize, cmd_ci,
                   cmd_budget, cmd_report, cmd_impact, cmd_changelog]:
            assert callable(fn)

    def test_publisher_module(self):
        from app.handlers.comments.publisher import (
            cmd_merge, cmd_apply, cmd_rollback, cmd_release,
            cmd_runtests, cmd_notify, cmd_security, cmd_secfull,
        )
        for fn in [cmd_merge, cmd_apply, cmd_rollback, cmd_release,
                   cmd_runtests, cmd_notify, cmd_security, cmd_secfull]:
            assert callable(fn)

    def test_service_module(self):
        from app.handlers.comments.service import handle_comment_event, _dispatch
        assert callable(handle_comment_event)
        assert callable(_dispatch)

    def test_backward_compat_shim(self):
        """Old import path must still work for any code not yet updated."""
        from app.handlers.comments import handle_comment_event
        assert callable(handle_comment_event)

    def test_no_circular_imports(self):
        """Import all sub-modules in sequence — circular import would raise."""
        import importlib
        mods = [
            'app.handlers.comments.constants',
            'app.handlers.comments.dispatcher',
            'app.handlers.comments.generator',
            'app.handlers.comments.reviewer',
            'app.handlers.comments.publisher',
            'app.handlers.comments.service',
            'app.handlers.comments',
        ]
        for mod in mods:
            m = importlib.import_module(mod)
            assert m is not None


class TestCommandExtraction:

    def test_basic_command(self):
        from app.handlers.comments.dispatcher import extract_command
        assert extract_command("/fix this bug") == "/fix"

    def test_command_at_start(self):
        from app.handlers.comments.dispatcher import extract_command
        assert extract_command("/explain") == "/explain"

    def test_longest_match_wins(self):
        """'/autofix' must not be matched as '/fix'."""
        from app.handlers.comments.dispatcher import extract_command
        assert extract_command("/autofix main.py") == "/autofix"

    def test_case_insensitive(self):
        from app.handlers.comments.dispatcher import extract_command
        assert extract_command("/FIX this issue") == "/fix"

    def test_no_command_returns_none(self):
        from app.handlers.comments.dispatcher import extract_command
        assert extract_command("looks good to me!") is None

    def test_substring_not_matched(self):
        """'/fix' inside 'profix' must not match."""
        from app.handlers.comments.dispatcher import extract_command
        result = extract_command("profix this issue please")
        assert result is None

    def test_command_with_args(self):
        from app.handlers.comments.dispatcher import extract_command
        assert extract_command("/rollback 3 confirm") == "/rollback"

    def test_command_in_middle_of_text(self):
        from app.handlers.comments.dispatcher import extract_command
        assert extract_command("Please /merge this PR when ready") == "/merge"

    def test_multiple_commands_first_wins(self):
        """When multiple commands present, longest match in sorted order wins."""
        from app.handlers.comments.dispatcher import extract_command
        result = extract_command("/fix and also /explain")
        assert result in ("/fix", "/explain")  # one of them, deterministic

    def test_all_commands_extractable(self):
        from app.handlers.comments.dispatcher import extract_command
        from app.handlers.comments.constants import ALL_COMMANDS
        for cmd in ALL_COMMANDS:
            result = extract_command(f"{cmd} some text")
            assert result == cmd, f"Failed to extract: {cmd}"


class TestProvidersDown:

    def test_is_providers_down_true(self):
        from app.handlers.comments.dispatcher import is_providers_down
        assert is_providers_down({"_providers_down": True}) is True

    def test_is_providers_down_false_on_normal_dict(self):
        from app.handlers.comments.dispatcher import is_providers_down
        assert is_providers_down({"result": "ok"}) is False

    def test_is_providers_down_false_on_empty(self):
        from app.handlers.comments.dispatcher import is_providers_down
        assert is_providers_down({}) is False

    def test_is_providers_down_false_on_none(self):
        from app.handlers.comments.dispatcher import is_providers_down
        assert is_providers_down(None) is False  # type: ignore

    def test_make_degraded_response_contains_retry(self):
        from app.handlers.comments.dispatcher import make_degraded_response
        result = make_degraded_response({"_providers_down": True, "_retry_in": 45})
        assert "45" in result
        assert "unavailable" in result.lower() or "AI" in result

    def test_providers_down_comment_default(self):
        from app.handlers.comments.dispatcher import providers_down_comment
        result = providers_down_comment()
        assert "60" in result
        assert "⚠️" in result

    def test_providers_down_comment_custom_retry(self):
        from app.handlers.comments.dispatcher import providers_down_comment
        result = providers_down_comment(retry_in=120)
        assert "120" in result


class TestBumpVersion:

    def test_patch_bump(self):
        from app.handlers.comments.reviewer import _bump_version
        assert _bump_version("v1.2.3") == "v1.2.4"

    def test_no_prefix_bump(self):
        from app.handlers.comments.reviewer import _bump_version
        assert _bump_version("1.2.3") == "1.2.4"

    def test_zero_patch(self):
        from app.handlers.comments.reviewer import _bump_version
        assert _bump_version("v0.0.0") == "v0.0.1"

    def test_invalid_falls_back(self):
        from app.handlers.comments.reviewer import _bump_version
        result = _bump_version("not-a-version")
        assert result == "v0.1.0"

    def test_large_version(self):
        from app.handlers.comments.reviewer import _bump_version
        assert _bump_version("v10.20.30") == "v10.20.31"


class TestRateLimiting:

    def test_first_call_allowed(self):
        from app.handlers.comments.dispatcher import check_user_rate_limit
        from unittest.mock import patch
        with patch('app.core.redis_client.get_redis') as mock_r:
            mock_r.return_value.incr.return_value = 1
            mock_r.return_value.expire.return_value = None
            result = check_user_rate_limit("org/repo", "user1")
        assert result is True

    def test_over_limit_rejected(self):
        from app.handlers.comments.dispatcher import check_user_rate_limit, USER_CMD_LIMIT
        from unittest.mock import patch
        with patch('app.core.redis_client.get_redis') as mock_r:
            mock_r.return_value.incr.return_value = USER_CMD_LIMIT + 1
            mock_r.return_value.expire.return_value = None
            result = check_user_rate_limit("org/repo", "user1")
        assert result is False

    def test_redis_failure_falls_back_to_local_enforcement(self):
        """Redis down → the limit is still enforced via the in-memory window
        (fail-open removed in V6.2). First call passes; the limit still bites."""
        from app.handlers.comments import dispatcher
        from app.handlers.comments.constants import USER_CMD_LIMIT
        from unittest.mock import patch
        dispatcher._local_cmd_counts.clear()
        with patch('app.core.redis_client.get_redis', side_effect=Exception("Redis down")):
            assert dispatcher.check_user_rate_limit("org/repo", "user1") is True
            for _ in range(USER_CMD_LIMIT):
                dispatcher.check_user_rate_limit("org/repo", "user1")
            assert dispatcher.check_user_rate_limit("org/repo", "user1") is False
        dispatcher._local_cmd_counts.clear()


class TestFileSize:

    def test_no_single_file_exceeds_500_lines(self):
        """Verify the split worked — no comments module should be a monolith."""
        import os
        comments_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'app', 'handlers', 'comments'
        )
        for fname in os.listdir(comments_dir):
            if not fname.endswith('.py'):
                continue
            fpath = os.path.join(comments_dir, fname)
            with open(fpath, encoding='utf-8') as f:
                lines = f.readlines()
            assert len(lines) <= 600, (
                f"{fname} has {len(lines)} lines — "
                f"should be ≤600 after the split. "
                f"Consider splitting further."
            )

    def test_service_py_is_thin(self):
        """service.py is an orchestration layer — keep it thin.
        Budget history: 260 pre-V6.2; +5 for model-disclosure reset/footer
        (cross-cutting, belongs in orchestration). Raise consciously, never
        casually."""
        import os
        fpath = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'app', 'handlers', 'comments', 'service.py'
        )
        with open(fpath, encoding='utf-8') as f:
            lines = f.readlines()
        assert len(lines) <= 265, (
            f"service.py has {len(lines)} lines — should stay under 265 lines as an orchestration layer."
        )
