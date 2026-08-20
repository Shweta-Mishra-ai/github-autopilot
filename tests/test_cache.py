"""
tests/test_cache.py

app/core/cache.py had no importers and three defects that would have surfaced
the moment anything used it:

  1. invalidate_repo() and get_stats() called r.keys(). The in-memory fallback
     does not implement keys() at all, so both raised AttributeError into a
     bare except — per-repo invalidation was a silent no-op on any deployment
     without Redis. Against real Redis, KEYS is O(N) over the whole keyspace
     and blocks the server while it runs.
  2. Cache keys carried only 32 bits of token digest. Entries are scoped to an
     installation, and a collision means one tenant reading another's response.
  3. _get() returned None for both "not cached" and "cached a null", so a
     legitimately-null response was re-fetched forever.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from app.core import cache as C
from app.core.redis_client import _FakeRedis


@pytest.fixture
def fake_redis():
    r = _FakeRedis()
    with patch.object(C.redis_client, "get_redis", return_value=r):
        yield r


@pytest.fixture
def gh():
    with patch("app.github.client.gh_get") as m:
        m.return_value = {"default_branch": "main", "archived": False}
        yield m


class TestKeyIsolation:
    def test_same_path_different_tokens_do_not_collide(self):
        """Entries are per-installation; a shared key leaks one tenant's data."""
        assert C._make_key("/repos/o/r", "token-a") != C._make_key("/repos/o/r", "token-b")

    def test_same_inputs_are_stable(self):
        assert C._make_key("/repos/o/r", "t") == C._make_key("/repos/o/r", "t")

    def test_different_paths_do_not_collide(self):
        assert C._make_key("/repos/o/r", "t") != C._make_key("/repos/o/other", "t")

    def test_token_digest_is_wide_enough_to_resist_collisions(self):
        """32 bits makes a birthday collision between installations plausible."""
        digest = C._make_key("/x", "tok").split(":")[2]
        assert len(digest) >= 16, "token digest must be at least 64 bits"

    def test_query_string_is_part_of_the_key(self):
        """?ref=<sha> selects a different file; it must not share an entry."""
        assert C._make_key("/repos/o/r/contents/f", "t") != C._make_key(
            "/repos/o/r/contents/f?ref=abc", "t"
        )


class TestCachedGet:
    def test_first_call_hits_the_api(self, fake_redis, gh):
        assert C.cached_gh_get("/repos/o/r", "tok") == gh.return_value
        assert gh.call_count == 1

    def test_second_call_is_served_from_cache(self, fake_redis, gh):
        C.cached_gh_get("/repos/o/r", "tok")
        C.cached_gh_get("/repos/o/r", "tok")
        assert gh.call_count == 1, "second read should not reach the API"

    def test_different_token_does_not_reuse_the_entry(self, fake_redis, gh):
        C.cached_gh_get("/repos/o/r", "token-a")
        C.cached_gh_get("/repos/o/r", "token-b")
        assert gh.call_count == 2

    def test_cached_null_is_a_hit_not_a_permanent_miss(self, fake_redis, gh):
        """A legitimately-null response was re-fetched on every call, because
        the miss sentinel and a cached None were the same value."""
        gh.return_value = None
        C.cached_gh_get("/repos/o/r", "tok")
        C.cached_gh_get("/repos/o/r", "tok")
        assert gh.call_count == 1

    def test_cached_empty_list_is_a_hit(self, fake_redis, gh):
        gh.return_value = []
        C.cached_gh_get("/repos/o/r/pulls", "tok")
        C.cached_gh_get("/repos/o/r/pulls", "tok")
        assert gh.call_count == 1

    def test_redis_failure_falls_through_to_the_api(self, gh):
        """A cache outage must cost API quota, never functionality."""
        broken = MagicMock()
        broken.get.side_effect = OSError("redis down")
        broken.set.side_effect = OSError("redis down")
        with patch.object(C.redis_client, "get_redis", return_value=broken):
            assert C.cached_gh_get("/repos/o/r", "tok") == gh.return_value

    def test_api_errors_are_not_cached(self, fake_redis, gh):
        gh.side_effect = RuntimeError("502")
        with pytest.raises(RuntimeError):
            C.cached_gh_get("/repos/o/r", "tok")
        gh.side_effect = None
        assert C.cached_gh_get("/repos/o/r", "tok") == gh.return_value


class TestRepoMetadata:
    def test_reads_the_repo_endpoint(self, fake_redis, gh):
        C.get_repo_metadata("o/r", "tok")
        assert gh.call_args[0][0] == "/repos/o/r"

    def test_is_cached_between_calls(self, fake_redis, gh):
        C.get_repo_metadata("o/r", "tok")
        C.get_repo_metadata("o/r", "tok")
        assert gh.call_count == 1

    def test_ttl_is_short_enough_to_follow_a_branch_rename(self):
        """/apply opens PRs against default_branch; an hour of staleness would
        target the wrong base."""
        assert C.REPO_METADATA_TTL <= 900


class TestInvalidation:
    def test_single_path_invalidation(self, fake_redis, gh):
        C.cached_gh_get("/repos/o/r", "tok")
        C.invalidate("/repos/o/r", "tok")
        C.cached_gh_get("/repos/o/r", "tok")
        assert gh.call_count == 2

    def test_repo_invalidation_actually_removes_entries(self, fake_redis, gh):
        """Previously scanned for a substring that cache keys never contain, so
        it matched nothing and reported success."""
        C.cached_gh_get("/repos/o/r", "tok")
        C.cached_gh_get("/repos/o/r/pulls/1", "tok")
        assert C.invalidate_repo("o/r") == 2
        C.cached_gh_get("/repos/o/r", "tok")
        assert gh.call_count == 3

    def test_repo_invalidation_spans_tokens(self, fake_redis, gh):
        """An installation token rotates; stale entries under the old one must
        still be reachable for invalidation."""
        C.cached_gh_get("/repos/o/r", "token-a")
        C.cached_gh_get("/repos/o/r", "token-b")
        assert C.invalidate_repo("o/r") == 2

    def test_repo_invalidation_leaves_other_repos_alone(self, fake_redis, gh):
        C.cached_gh_get("/repos/o/keep", "tok")
        C.cached_gh_get("/repos/o/drop", "tok")
        C.invalidate_repo("o/drop")
        gh.reset_mock()
        C.cached_gh_get("/repos/o/keep", "tok")
        gh.assert_not_called()

    def test_repo_invalidation_on_unknown_repo_is_harmless(self, fake_redis):
        assert C.invalidate_repo("never/cached") == 0

    def test_repo_invalidation_survives_redis_failure(self):
        broken = MagicMock()
        broken.smembers.side_effect = OSError("down")
        with patch.object(C.redis_client, "get_redis", return_value=broken):
            assert C.invalidate_repo("o/r") == 0


class TestPathParsing:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("/repos/owner/name", "owner/name"),
            ("/repos/owner/name/pulls/1", "owner/name"),
            ("/repos/owner/name/contents/f.py?ref=abc", "owner/name"),
            ("/user", ""),
            ("/repos/owner", ""),
            ("", ""),
        ],
    )
    def test_repo_extracted_from_path(self, path, expected):
        assert C._repo_from_path(path) == expected


class TestStats:
    def test_counts_hits_and_misses(self, fake_redis, gh):
        C.cached_gh_get("/repos/o/r", "tok")  # miss
        C.cached_gh_get("/repos/o/r", "tok")  # hit
        s = C.get_stats()
        assert s["hits"] == 1
        assert s["misses"] == 1
        assert s["hit_rate"] == 0.5

    def test_counts_entries_without_blocking_redis(self, fake_redis, gh):
        """Uses SCAN. KEYS walks the entire keyspace and blocks the server —
        on a shared free-tier instance that is a production hazard."""
        C.cached_gh_get("/repos/o/a", "tok")
        C.cached_gh_get("/repos/o/b", "tok")
        assert C.get_stats()["keys"] == 2

    def test_hit_rate_with_no_traffic(self, fake_redis):
        assert C.get_stats()["hit_rate"] == 0.0

    def test_stats_survive_redis_failure(self):
        broken = MagicMock()
        broken.get.side_effect = OSError("down")
        with patch.object(C.redis_client, "get_redis", return_value=broken):
            assert C.get_stats() == {"hits": 0, "misses": 0, "hit_rate": 0.0, "keys": 0}


class TestTtlSelection:
    def test_repo_paths_get_the_metadata_ttl(self):
        assert C._get_ttl("/repos/o/r") == C.REPO_METADATA_TTL

    def test_unknown_paths_get_the_conservative_default(self):
        assert C._get_ttl("/user/orgs") == C.TTL_MAP["default"]

    def test_no_correctness_critical_path_is_cached_by_default(self):
        """PR files, diffs and file contents change under us; a TTL entry for
        them would serve the wrong data to a review or a security scan."""
        for risky in ("/pulls/", "/contents/", "/issues/", "/commits/"):
            assert risky not in C.TTL_MAP, (
                f"{risky} must not have a default TTL — a stale answer there "
                f"changes what the bot reports"
            )


class TestFakeRedisScan:
    def test_scan_iter_matches_glob(self):
        r = _FakeRedis()
        r.set("a:1", "x")
        r.set("a:2", "x")
        r.set("b:1", "x")
        assert sorted(r.scan_iter(match="a:*")) == ["a:1", "a:2"]

    def test_keys_is_available_for_compatibility(self):
        r = _FakeRedis()
        r.set("k", "v")
        assert r.keys("*") == ["k"]

    def test_scan_iter_skips_expired_entries(self):
        r = _FakeRedis()
        r.set("gone", "v", ex=-1)
        assert list(r.scan_iter(match="*")) == []

    def test_scan_iter_on_empty_store(self):
        assert list(_FakeRedis().scan_iter(match="*")) == []


class TestSerialisation:
    def test_values_round_trip(self, fake_redis, gh):
        gh.return_value = {"n": 1, "list": [1, 2], "nested": {"k": "v"}}
        C.cached_gh_get("/repos/o/r", "tok")
        assert C.cached_gh_get("/repos/o/r", "tok") == gh.return_value

    def test_stored_value_is_json(self, fake_redis, gh):
        C.cached_gh_get("/repos/o/r", "tok")
        raw = fake_redis.get(C._make_key("/repos/o/r", "tok"))
        assert json.loads(raw) == gh.return_value
