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

    def test_it_never_raises(self):
        """A config read that raises takes the whole webhook with it."""
        with patch("app.github.client.gh_get", side_effect=RuntimeError("boom")):
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
    requests for the same file. Only the first fetches now; the rest take
    defaults and let the cache warm."""

    def test_a_second_thread_does_not_pile_on(self):
        started = threading.Event()
        release = threading.Event()
        paths: list[str] = []

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
            second = load_config("o/r", "tok")

            release.set()
            first.join(timeout=5)

        assert len(paths) == 1, f"{len(paths)} concurrent fetches for one repo"
        assert isinstance(second, Config)

    def test_the_sentinel_is_released_even_when_the_fetch_fails(self):
        """A repo left in the in-flight set would never be fetched again — it
        would serve defaults forever, silently."""
        with patch("app.github.client.gh_get", side_effect=RuntimeError("boom")):
            load_config("o/r", "tok")
        assert "o/r" not in config_mod._config_fetching

    def test_the_sentinel_is_released_on_success(self):
        ctx, _ = _serving("{}")
        with ctx:
            load_config("o/r", "tok")
        assert "o/r" not in config_mod._config_fetching
