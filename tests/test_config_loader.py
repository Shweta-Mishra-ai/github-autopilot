"""
tests/test_config_loader.py

load_config() — read on every webhook event, and entirely uncovered.

It decides whether the bot runs at all, whether auto-merge is on, whether
secrets are scanned, and who may merge. It also caches, guards against a
thundering herd, and reads deliberately from the default branch rather than a
pull request head, which is a trust boundary rather than a detail.

The default-branch rule was pinned by a test that greps this module's source
for "ref=". That fires at review time, which is worth keeping, but it would
pass just as happily if the fetch never happened at all, and it breaks on a
refactor that builds the path in a variable. The property is tested here by
calling the function and reading the path it actually requested.
"""

from __future__ import annotations

import base64
import threading
import time
from unittest.mock import patch

import pytest

import app.core.config as config_mod
from app.core.config import Config, load_config


@pytest.fixture(autouse=True)
def clean_cache():
    config_mod.invalidate_config_cache()
    config_mod._config_fetching.clear()
    config_mod._last_config_prune = 0.0
    yield
    config_mod.invalidate_config_cache()
    config_mod._config_fetching.clear()
    config_mod._last_config_prune = 0.0


def _contents(yaml_text: str) -> dict:
    return {"content": base64.b64encode(yaml_text.encode()).decode()}


def _serving(yaml_text: str):
    """Patch the GitHub read, and record every path it was asked for."""
    paths: list[str] = []

    def fake_get(path, token):
        paths.append(path)
        return _contents(yaml_text)

    return patch("app.github.client.gh_get", side_effect=fake_get), paths


class TestItReadsTheDefaultBranch:
    """A contributor must not be able to grant themselves merge rights by
    editing the YAML inside their own pull request. Config therefore takes
    effect only once merged, which means the fetch must not be ref-pinned."""

    def test_the_request_carries_no_ref(self):
        ctx, paths = _serving("pull_requests:\n  code_review: true\n")
        with ctx:
            load_config("o/r", "tok")
        assert paths, "no request was made at all"
        assert "ref=" not in paths[0], paths[0]

    def test_the_request_carries_no_sha_either(self):
        """The same escape, spelled differently."""
        ctx, paths = _serving("{}")
        with ctx:
            load_config("o/r", "tok")
        assert "?" not in paths[0], f"the fetch is parameterised: {paths[0]}"

    def test_it_reads_the_expected_file(self):
        ctx, paths = _serving("{}")
        with ctx:
            load_config("acme/widgets", "tok")
        assert paths[0] == "/repos/acme/widgets/contents/.ai-repo-manager.yml"


class TestParsing:
    def test_valid_yaml_becomes_config(self):
        ctx, _ = _serving("pull_requests:\n  code_review: false\n")
        with ctx:
            cfg = load_config("o/r", "tok")
        assert cfg.get("pull_requests", "code_review", default=True) is False

    def test_a_missing_file_falls_back_to_defaults(self):
        """Most repositories have no config file. That is the normal path, not
        an error one."""
        from app.github.client import GitHubError

        with patch("app.github.client.gh_get", side_effect=GitHubError("Not found", 404)):
            cfg = load_config("o/r", "tok")
        assert isinstance(cfg, Config)
        assert cfg.get("pull_requests", "code_review", default=True) is True

    def test_invalid_yaml_falls_back_to_defaults(self):
        ctx, _ = _serving("pull_requests:\n  - [unclosed\n")
        with ctx:
            cfg = load_config("o/r", "tok")
        assert isinstance(cfg, Config)

    def test_yaml_that_is_not_a_mapping_falls_back_to_defaults(self):
        """`- a\\n- b` parses cleanly and is a list. Treating it as config
        would raise on the first .get() deep inside a handler."""
        ctx, _ = _serving("- one\n- two\n")
        with ctx:
            cfg = load_config("o/r", "tok")
        assert cfg.get("pull_requests", "code_review", default=True) is True

    def test_an_empty_file_falls_back_to_defaults(self):
        ctx, _ = _serving("")
        with ctx:
            assert isinstance(load_config("o/r", "tok"), Config)

    def test_undecodable_content_falls_back_to_defaults(self):
        with patch("app.github.client.gh_get", return_value={"content": "!!!not base64!!!"}):
            assert isinstance(load_config("o/r", "tok"), Config)

    def test_a_response_with_no_content_key_falls_back_to_defaults(self):
        with patch("app.github.client.gh_get", return_value={}):
            assert isinstance(load_config("o/r", "tok"), Config)

    def test_an_unreadable_config_is_not_replaced_by_defaults(self):
        """This was "it never raises": a failed read returned defaults, and
        the defaults ignore the repo's kill switch, command allow-list and
        /merge restrictions. With no earlier copy the event is refused (and
        retried) instead."""
        from app.core.config import ConfigUnavailable

        with patch("app.github.client.gh_get", side_effect=RuntimeError("boom")), \
             pytest.raises(ConfigUnavailable, match="boom"):
            load_config("o/r", "tok")

    def test_a_file_that_is_not_valid_config_still_uses_defaults(self):
        """Read but unparseable is the maintainer's to fix; retrying cannot."""
        with patch("app.github.client.gh_get", return_value={"content": "%%% not base64"}):
            assert isinstance(load_config("o/r", "tok"), Config)


class TestCaching:
    def test_a_second_call_within_the_ttl_does_not_refetch(self):
        ctx, paths = _serving("{}")
        with ctx:
            load_config("o/r", "tok")
            load_config("o/r", "tok")
            load_config("o/r", "tok")
        assert len(paths) == 1, f"config was fetched {len(paths)} times"

    def test_different_repos_are_cached_separately(self):
        ctx, paths = _serving("{}")
        with ctx:
            load_config("o/one", "tok")
            load_config("o/two", "tok")
        assert len(paths) == 2

    def test_an_expired_entry_is_refetched(self):
        ctx, paths = _serving("{}")
        with ctx:
            load_config("o/r", "tok")
            cfg, _ = config_mod._config_cache["o/r"]
            # Age the entry rather than sleeping five minutes.
            config_mod._config_cache["o/r"] = (cfg, time.time() - config_mod._CONFIG_TTL - 1)
            load_config("o/r", "tok")
        assert len(paths) == 2

    def test_invalidate_clears_one_repo(self):
        ctx, paths = _serving("{}")
        with ctx:
            load_config("o/one", "tok")
            load_config("o/two", "tok")
            config_mod.invalidate_config_cache("o/one")
            load_config("o/one", "tok")
            load_config("o/two", "tok")
        assert len(paths) == 3, "invalidating one repo refetched the other too"

    def test_invalidate_with_no_argument_clears_everything(self):
        ctx, paths = _serving("{}")
        with ctx:
            load_config("o/one", "tok")
            config_mod.invalidate_config_cache()
            load_config("o/one", "tok")
        assert len(paths) == 2

    def test_expired_entries_are_pruned_rather_than_accumulating(self):
        """The TTL was read on lookup but nothing ever deleted, so a
        multi-tenant instance grew a Config per repo it had ever seen."""
        ctx, _ = _serving("{}")
        with ctx:
            for i in range(5):
                load_config(f"o/repo{i}", "tok")

            aged = time.time() - config_mod._CONFIG_TTL - 1
            for key, (cfg, _ts) in list(config_mod._config_cache.items()):
                config_mod._config_cache[key] = (cfg, aged)

            config_mod._last_config_prune = 0.0  # make the next prune eligible
            load_config("o/fresh", "tok")

        assert "o/repo0" not in config_mod._config_cache
        assert "o/fresh" in config_mod._config_cache


class TestThunderingHerd:
    """N threads missing the cache at once used to make N identical GitHub
    requests for the same file. Only the first fetches now, and the others
    WAIT for it — they used to take the defaults, which ignored the repo's
    kill switch and maintainer_only restrictions."""

    def test_a_second_thread_does_not_pile_on_and_gets_the_real_config(self):
        started = threading.Event()
        release = threading.Event()
        paths: list[str] = []
        result: dict = {}

        def slow_get(path, token):
            paths.append(path)
            started.set()
            release.wait(timeout=5)
            return _contents("pull_requests:\n  code_review: false\n")

        with patch("app.github.client.gh_get", side_effect=slow_get):
            first = threading.Thread(target=lambda: load_config("o/r", "tok"))
            first.start()
            assert started.wait(timeout=5), "the first fetch never started"

            # Second caller arrives mid-fetch.
            second = threading.Thread(target=lambda: result.update(cfg=load_config("o/r", "tok")))
            second.start()
            time.sleep(0.05)
            release.set()
            first.join(timeout=5)
            second.join(timeout=5)

        assert len(paths) == 1, f"{len(paths)} concurrent fetches for one repo"
        assert result["cfg"].get("pull_requests", "code_review") is False, (
            "the concurrent caller must see the repo's config, not the defaults"
        )

    def test_an_empty_section_does_not_discard_the_kill_switch(self):
        ctx, _ = _serving("bot:\n  enabled: false\npush:\n")
        with ctx:
            cfg = load_config("o/r", "tok")
        assert cfg.bot_enabled() is False

    def test_a_transient_error_keeps_the_last_good_config(self):
        ctx, _ = _serving("bot:\n  enabled: false\n")
        with ctx:
            load_config("o/r", "tok")
        # Expire it, then fail the refetch with a 502.
        cfg_obj, _ts = config_mod._config_cache["o/r"]
        config_mod._config_cache["o/r"] = (cfg_obj, 0)
        from app.github.client import GitHubError

        with patch("app.github.client.gh_get", side_effect=GitHubError("bad gateway", 502)):
            cfg = load_config("o/r", "tok")
        assert cfg.bot_enabled() is False

    def test_a_transient_error_with_no_history_caches_nothing(self):
        """Defaults were cached for 30s; nothing is cached now, so the retry
        reads the file again."""
        from app.core.config import ConfigUnavailable
        from app.github.client import GitHubError

        with patch("app.github.client.gh_get", side_effect=GitHubError("bad gateway", 502)), \
             pytest.raises(ConfigUnavailable):
            load_config("o/r", "tok")
        assert "o/r" not in config_mod._config_cache

    def test_the_kill_switch_holds_through_a_failed_read(self):
        from app.core.config import ConfigUnavailable
        from app.github.client import GitHubError

        with patch("app.github.client.gh_get", side_effect=GitHubError("bad gateway", 502)):
            try:
                cfg = load_config("o/r", "tok")
            except ConfigUnavailable:
                cfg = None
        assert cfg is None, "a failed read yielded a Config that runs the bot on defaults"

    def test_a_failed_read_is_transient(self):
        from app.core.config import ConfigUnavailable
        from app.core.transient import is_transient

        assert is_transient(ConfigUnavailable("x"))

    def test_a_quoted_number_is_stored_as_a_number(self):
        cfg = Config({"push": {"create_issue_threshold": "5"}})
        assert cfg.get("push", "create_issue_threshold") == 5

    def test_maintainer_only_entries_may_carry_a_slash(self):
        cfg = Config({"commands": {"permissions": {"maintainer_only": ["/fix"]}}})
        assert cfg.is_maintainer_only("/fix") and cfg.is_maintainer_only("fix")

    def test_the_sentinel_is_released_even_when_the_fetch_fails(self):
        """A repo left in the in-flight set would never be fetched again — it
        would serve defaults forever, silently."""
        from app.core.config import ConfigUnavailable

        with patch("app.github.client.gh_get", side_effect=RuntimeError("boom")), \
             pytest.raises(ConfigUnavailable):
            load_config("o/r", "tok")
        assert "o/r" not in config_mod._config_fetching

    def test_the_sentinel_is_released_on_success(self):
        ctx, _ = _serving("{}")
        with ctx:
            load_config("o/r", "tok")
        assert "o/r" not in config_mod._config_fetching


class TestASectionWrittenAsAScalar:
    """Round-3 audit C4. `bot: false` left the bot running, and one section
    of the wrong type made the whole repo's events retry forever."""

    def test_bot_false_is_the_kill_switch(self):
        c = Config({"bot": False})
        assert not c.bot_enabled() and not c.pr_enabled() and not c.command_enabled("fix")

    @pytest.mark.parametrize("value", [False, "false", "off", "no", "FALSE"])
    def test_section_false_disables_it(self, value):
        assert not Config({"pull_requests": value}).pr_enabled()
        assert not Config({"push": {"enabled": value}}).push_enabled()

    def test_a_quoted_false_switch_is_off(self):
        c = Config({"pull_requests": {"code_review": "false"}, "bot": {"enabled": "false"}})
        assert c.get("pull_requests", "code_review") is False
        assert not c.bot_enabled()

    def test_an_unreadable_switch_keeps_its_default(self):
        c = Config({"pull_requests": {"code_review": "maybe"}})
        assert c.get("pull_requests", "code_review") is True

    def test_a_wrong_typed_section_costs_only_that_section(self):
        y = (
            "bot: {enabled: false}\n"
            "commands: {permissions: {maintainer_only: [autofix]}}\n"
            "confidence: high\n"
        )
        cm, _ = _serving(y)
        with cm:
            c = load_config("o/r", "t")
        assert not c.bot_enabled()
        assert c.is_maintainer_only("autofix")

    @pytest.mark.parametrize("value", [True, False, "x", 3, 2.5, [1], None, {"enabled": "nonsense"}])
    def test_no_section_value_raises(self, value):
        from app.core.config import DEFAULTS

        for key in list(DEFAULTS) + ["ci", "confidence"]:
            c = Config({key: value})
            c.bot_enabled(), c.pr_enabled(), c.ci_enabled(), c.command_enabled("fix")
            c.is_maintainer_only("merge"), c.footer

    def test_the_commands_allow_list_is_not_a_switch(self):
        c = Config({"commands": {"enabled": ["fix"]}})
        assert c.command_enabled("fix") and not c.command_enabled("autofix")
