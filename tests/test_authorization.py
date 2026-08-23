"""
tests/test_authorization.py

Regression cover for the fail-closed permission path.

The bug these guard: get_user_permission() collapsed every failure mode into
the literal permission level "none". A GitHub App that could not read
collaborator permissions (403) therefore produced the same answer as a genuine
non-collaborator, and every RESTRICTED_COMMAND — /autofix, /apply, /merge,
/rollback, /release, /secfull, /ignore — was denied for everyone, including
repo owners, with a message stating their access level was "none".
"""

from unittest.mock import patch

import pytest

from app.core.authorization import (
    MAINTAINER_PERMISSIONS,
    PERMISSION_UNKNOWN,
    RESTRICTED_COMMANDS,
    check_command_permission,
    get_user_permission,
    invalidate_permission_cache,
)
from app.github.client import GitHubError


class _Config:
    """Minimal stand-in — restricted commands never consult it."""

    def is_maintainer_only(self, cmd_key):
        return False


@pytest.fixture(autouse=True)
def _clear_cache():
    invalidate_permission_cache()
    yield
    invalidate_permission_cache()


class TestPermissionResolution:
    def test_404_is_a_real_answer_and_means_none(self):
        """GitHub answered: the user genuinely is not a collaborator."""
        with patch(
            "app.core.authorization.gh_get",
            side_effect=GitHubError("Not found", 404),
        ):
            assert get_user_permission("o/r", "stranger", "tok") == "none"

    @pytest.mark.parametrize("status", [403, 429, 500, 502, 503])
    def test_failed_lookup_is_unknown_not_none(self, status):
        """A failed check is not a fact about the user."""
        with patch(
            "app.core.authorization.gh_get",
            side_effect=GitHubError("boom", status),
        ):
            assert get_user_permission("o/r", "owner", "tok") == PERMISSION_UNKNOWN

    def test_non_github_exception_is_unknown(self):
        with patch(
            "app.core.authorization.gh_get",
            side_effect=ConnectionError("network down"),
        ):
            assert get_user_permission("o/r", "owner", "tok") == PERMISSION_UNKNOWN

    def test_unknown_is_not_cached(self):
        """A transient failure, or a permission an operator just granted, must
        not stay broken for the full 5-minute TTL."""
        calls = []

        def _flaky(path, token):
            calls.append(path)
            if len(calls) == 1:
                raise GitHubError("temporary", 503)
            return {"permission": "admin"}

        with patch("app.core.authorization.gh_get", side_effect=_flaky):
            assert get_user_permission("o/r", "owner", "tok") == PERMISSION_UNKNOWN
            assert get_user_permission("o/r", "owner", "tok") == "admin"
        assert len(calls) == 2, "the failed lookup was cached and never retried"

    def test_real_permission_is_cached(self):
        with patch(
            "app.core.authorization.gh_get",
            return_value={"permission": "write"},
        ) as gh:
            get_user_permission("o/r", "dev", "tok")
            get_user_permission("o/r", "dev", "tok")
        assert gh.call_count == 1

    def test_unknown_is_not_a_maintainer_permission(self):
        """Fail closed: 'unknown' must never satisfy an access check."""
        assert PERMISSION_UNKNOWN not in MAINTAINER_PERMISSIONS


class TestDenialMessaging:
    def test_failed_check_denies_but_does_not_blame_the_user(self):
        with patch(
            "app.core.authorization.get_user_permission",
            return_value=PERMISSION_UNKNOWN,
        ):
            allowed, reason = check_command_permission(
                "/autofix", "o/r", "owner", "tok", _Config()
            )

        assert allowed is False, "must still fail closed"
        assert "not a statement about your access" in reason
        assert "GitHub App" in reason
        # The misleading claim from the old message must be gone.
        assert "Your current access level: `none`" not in reason

    def test_genuine_non_collaborator_still_gets_the_access_message(self):
        with patch("app.core.authorization.get_user_permission", return_value="none"):
            allowed, reason = check_command_permission(
                "/merge", "o/r", "stranger", "tok", _Config()
            )

        assert allowed is False
        assert "write/maintain/admin" in reason
        assert "not a statement about your access" not in reason

    def test_read_only_collaborator_is_denied_with_their_real_level(self):
        with patch("app.core.authorization.get_user_permission", return_value="read"):
            allowed, reason = check_command_permission(
                "/merge", "o/r", "reader", "tok", _Config()
            )

        assert allowed is False
        assert "`read`" in reason

    @pytest.mark.parametrize("perm", sorted(MAINTAINER_PERMISSIONS))
    def test_maintainers_are_allowed(self, perm):
        with patch("app.core.authorization.get_user_permission", return_value=perm):
            allowed, reason = check_command_permission(
                "/apply", "o/r", "maintainer", "tok", _Config()
            )
        assert allowed is True
        assert reason == ""

    def test_denial_resolves_permission_with_a_single_lookup(self):
        """The old path called is_maintainer() and then get_user_permission(),
        which for an uncached result meant two live API calls per denial."""
        with patch(
            "app.core.authorization.gh_get",
            side_effect=GitHubError("Not found", 404),
        ) as gh:
            check_command_permission("/merge", "o/r", "stranger", "tok", _Config())
        assert gh.call_count == 1

    @pytest.mark.parametrize("cmd", sorted(RESTRICTED_COMMANDS))
    def test_every_restricted_command_is_blocked_by_a_failed_check(self, cmd):
        """This is the blast radius of the original bug."""
        with patch(
            "app.core.authorization.get_user_permission",
            return_value=PERMISSION_UNKNOWN,
        ):
            allowed, _ = check_command_permission(cmd, "o/r", "owner", "tok", _Config())
        assert allowed is False

    def test_unrestricted_command_skips_the_permission_api(self):
        with patch("app.core.authorization.gh_get") as gh:
            allowed, _ = check_command_permission(
                "/explain", "o/r", "anyone", "tok", _Config()
            )
        assert allowed is True
        gh.assert_not_called()


class TestObservability:
    def test_failed_check_increments_a_metric(self):
        from app.core.metrics import metrics

        before = metrics.get("auth.permission_check_failed", 0)
        with patch(
            "app.core.authorization.gh_get",
            side_effect=GitHubError("forbidden", 403),
        ):
            get_user_permission("o/r", "owner", "tok")
        assert metrics.get("auth.permission_check_failed", 0) == before + 1

    def test_genuine_404_does_not_increment_the_failure_metric(self):
        from app.core.metrics import metrics

        before = metrics.get("auth.permission_check_failed", 0)
        with patch(
            "app.core.authorization.gh_get",
            side_effect=GitHubError("Not found", 404),
        ):
            get_user_permission("o/r", "stranger", "tok")
        assert metrics.get("auth.permission_check_failed", 0) == before


class TestResourceSpendingCommandsAreGated:
    """
    /runtests and /notify were documented as maintainer-only — the README
    listed them that way — and had no gate at all.

    Neither reads or writes code, so the exposure was never disclosure. It was
    that a stranger could spend the maintainer's resources: dispatch CI runs
    against their Actions minutes, or push messages into their team's Slack and
    Discord, as often as they cared to comment. On a public repository that is
    anyone at all.
    """

    @pytest.mark.parametrize("cmd", ["/runtests", "/notify"])
    def test_a_stranger_is_denied(self, cmd):
        with patch("app.core.authorization.get_user_permission", return_value="none"):
            allowed, reason = check_command_permission(cmd, "o/r", "stranger", "tok", _Config())
        assert allowed is False, f"{cmd} must not run for a non-collaborator"
        assert "write/maintain/admin" in reason

    @pytest.mark.parametrize("cmd", ["/runtests", "/notify"])
    def test_a_read_only_collaborator_is_denied(self, cmd):
        with patch("app.core.authorization.get_user_permission", return_value="read"):
            allowed, _ = check_command_permission(cmd, "o/r", "reader", "tok", _Config())
        assert allowed is False, f"read access must not be enough for {cmd}"

    @pytest.mark.parametrize("cmd", ["/runtests", "/notify"])
    def test_a_maintainer_is_allowed(self, cmd):
        with patch("app.core.authorization.get_user_permission", return_value="write"):
            allowed, reason = check_command_permission(cmd, "o/r", "owner", "tok", _Config())
        assert allowed is True, f"{cmd} must still work for a maintainer: {reason}"

    @pytest.mark.parametrize("cmd", ["/runtests", "/notify"])
    def test_a_failed_permission_check_denies_rather_than_assuming(self, cmd):
        with patch(
            "app.core.authorization.get_user_permission", return_value=PERMISSION_UNKNOWN
        ):
            allowed, reason = check_command_permission(cmd, "o/r", "owner", "tok", _Config())
        assert allowed is False, f"{cmd} must fail closed like every other gated command"
        assert "not a statement about your access" in reason
