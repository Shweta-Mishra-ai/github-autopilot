"""
tests/test_v7_noise.py — V7 Phase 2.

The bot's comment volume, not its accuracy, is what made it annoying to live
with. These tests pin the volume down: one comment per PR, one per failing
commit, one secret alert per repo per window, and silence when there is
genuinely nothing to say.
"""

from unittest.mock import MagicMock, patch

from app.handlers import pull_request as pr_mod


def _cfg():
    cfg = MagicMock()
    cfg.footer = ""
    cfg.pr_enabled.return_value = True
    cfg.get.side_effect = lambda *a, **kw: kw.get("default", True)
    return cfg


def _payload(action="opened"):
    return {
        "action": action,
        "pull_request": {
            "number": 1,
            "title": "t",
            "body": "b",
            "user": {"login": "dev"},
            "head": {"ref": "f", "sha": "s"},
            "base": {"ref": "main"},
        },
        "repository": {"full_name": "o/r"},
        "installation": {"id": 1},
    }


class TestSinglePRComment:
    def test_pr_open_posts_exactly_one_comment(self):
        """Was four: analysis, summary, code review, test gaps."""
        with (
            patch.object(pr_mod, "get_installation_token", return_value="tok"),
            patch.object(pr_mod, "load_config", return_value=_cfg()),
            patch.object(pr_mod, "gh_get", return_value=[]),
            patch.object(
                pr_mod.router,
                "ask",
                return_value=({"risk_level": "low", "confidence": 0.9}, MagicMock()),
            ),
            patch.object(pr_mod.router, "ask_text", return_value=("summary", MagicMock())),
            patch.object(pr_mod, "upsert_sticky") as sticky,
            patch.object(pr_mod, "gh_post") as post,
        ):
            pr_mod.handle(_payload("opened"))

        assert sticky.call_count == 1
        assert post.call_count == 0

    def test_second_event_edits_rather_than_appends(self):
        from app.github.sticky import MARKER_PR_REPORT, upsert_sticky

        with (
            patch("app.github.sticky.find_sticky", return_value=77),
            patch("app.github.sticky.gh_patch") as patch_fn,
            patch("app.github.sticky.gh_post") as post,
        ):
            upsert_sticky("o/r", 1, "t", MARKER_PR_REPORT, "second run")
        patch_fn.assert_called_once()
        post.assert_not_called()


class TestSilenceWhenNothingToSay:
    def test_synchronize_with_no_findings_posts_nothing(self):
        with (
            patch.object(pr_mod, "get_installation_token", return_value="tok"),
            patch.object(pr_mod, "load_config", return_value=_cfg()),
            patch.object(pr_mod, "gh_get", return_value=[]),
            patch.object(
                pr_mod.router,
                "ask",
                return_value=(
                    {"score": 10, "issues": [], "summary": "clean", "has_gaps": False},
                    MagicMock(),
                ),
            ),
            patch.object(pr_mod, "upsert_sticky") as sticky,
        ):
            pr_mod.handle(_payload("synchronize"))

        assert sticky.call_count == 0


class TestReportAssembly:
    def test_report_includes_every_non_empty_section(self):
        body = pr_mod._build_pr_report(
            "ANALYSIS_TEXT",
            "SUMMARY_TEXT",
            "REVIEW_TEXT",
            "GAPS_TEXT",
            {"number": 7},
            [{"additions": 10, "deletions": 2}],
        )
        for chunk in ("ANALYSIS_TEXT", "SUMMARY_TEXT", "REVIEW_TEXT", "GAPS_TEXT"):
            assert chunk in body
        assert "PR #7" in body
        assert "+10 −2" in body

    def test_empty_sections_are_omitted(self):
        body = pr_mod._build_pr_report("", "SUMMARY_ONLY", "", "", {"number": 7}, [])
        assert "SUMMARY_ONLY" in body
        assert "Code review" not in body
        assert "Test coverage" not in body


# ── Secret scanning ───────────────────────────────────────────────────────────

from app.handlers import push as push_mod  # noqa: E402
from app.security.enhanced_secrets import SecretFinding  # noqa: E402


class TestSecretSeverityFloor:
    def test_only_critical_and_high_are_actionable(self):
        findings = [
            SecretFinding("AWS Access Key ID", 3, "critical", "AKIA...1234"),
            SecretFinding("Stripe Publishable Key", 9, "medium", "pk_li...cdef"),
            SecretFinding("GCP API Key", 4, "high", "AIza...wxyz"),
        ]
        out = push_mod._actionable_secrets(findings)
        assert {f.severity for f in out} == {"critical", "high"}

    def test_medium_only_findings_open_no_issue(self):
        findings = [SecretFinding("Stripe Publishable Key", 9, "medium", "pk_li...cdef")]
        assert push_mod._actionable_secrets(findings) == []

    def test_push_uses_the_enhanced_scanner(self):
        """The legacy scanner has no per-path suppression and drove the FP noise."""
        import inspect

        src = inspect.getsource(push_mod)
        assert "enhanced_secrets" in src
        assert "from app.security.secrets import" not in src


class TestDedupFailsClosed:
    def test_redis_error_suppresses_rather_than_duplicates(self):
        """Seven duplicate secret issues in 73s came from failing open."""
        with patch("app.core.redis_client.get_redis", side_effect=Exception("redis down")):
            assert push_mod._already_reported("o/r", "secret_alert") is True

    def test_first_report_in_window_is_allowed(self):
        fake = MagicMock()
        fake.set.return_value = True  # NX succeeded — key was absent
        with patch("app.core.redis_client.get_redis", return_value=fake):
            assert push_mod._already_reported("o/r", "secret_alert") is False

    def test_second_report_in_window_is_suppressed(self):
        fake = MagicMock()
        fake.set.return_value = None  # NX failed — key present
        with patch("app.core.redis_client.get_redis", return_value=fake):
            assert push_mod._already_reported("o/r", "secret_alert") is True


class TestSecretAlertReuse:
    def test_second_finding_set_comments_on_the_open_issue(self):
        fake = MagicMock()
        fake.get.return_value = "123"  # existing alert issue
        finding = SecretFinding("AWS Access Key ID", 3, "critical", "AKIA...1234")
        with (
            patch("app.core.redis_client.get_redis", return_value=fake),
            patch.object(push_mod, "gh_get", return_value={"state": "open"}),
            patch.object(push_mod, "gh_post") as post,
            patch.object(push_mod, "notify_secret_detected"),
        ):
            push_mod._open_secret_issue("o/r", "tok", [finding], MagicMock())
        assert post.call_args[0][0] == "/repos/o/r/issues/123/comments"

    def test_no_open_alert_creates_a_new_issue(self):
        fake = MagicMock()
        fake.get.return_value = None
        finding = SecretFinding("AWS Access Key ID", 3, "critical", "AKIA...1234")
        with (
            patch("app.core.redis_client.get_redis", return_value=fake),
            patch.object(push_mod, "gh_post", return_value={"number": 55}) as post,
            patch.object(push_mod, "notify_secret_detected"),
        ):
            push_mod._open_secret_issue("o/r", "tok", [finding], MagicMock())
        assert post.call_args[0][0] == "/repos/o/r/issues"

    def test_closed_alert_issue_opens_a_fresh_one(self):
        fake = MagicMock()
        fake.get.return_value = "123"
        finding = SecretFinding("AWS Access Key ID", 3, "critical", "AKIA...1234")
        with (
            patch("app.core.redis_client.get_redis", return_value=fake),
            patch.object(push_mod, "gh_get", return_value={"state": "closed"}),
            patch.object(push_mod, "gh_post", return_value={"number": 56}) as post,
            patch.object(push_mod, "notify_secret_detected"),
        ):
            push_mod._open_secret_issue("o/r", "tok", [finding], MagicMock())
        assert post.call_args[0][0] == "/repos/o/r/issues"


# ── CI ────────────────────────────────────────────────────────────────────────

from app.handlers import ci as ci_mod  # noqa: E402


class TestCIDedup:
    def test_matrix_of_failures_on_one_sha_alerts_once(self):
        """A 5-job matrix failing on one commit fired five separate analyses."""
        fake = MagicMock()
        fake.set.side_effect = [True, None, None, None, None]  # NX: first wins only
        with patch("app.core.redis_client.get_redis", return_value=fake):
            results = [ci_mod._ci_already_alerted("o/r", 1, "sha1") for _ in range(5)]
        assert results == [False, True, True, True, True]

    def test_a_new_commit_alerts_again(self):
        fake = MagicMock()
        fake.set.return_value = True
        with patch("app.core.redis_client.get_redis", return_value=fake):
            assert ci_mod._ci_already_alerted("o/r", 1, "sha2") is False

    def test_redis_error_fails_closed(self):
        with patch("app.core.redis_client.get_redis", side_effect=Exception("down")):
            assert ci_mod._ci_already_alerted("o/r", 1, "sha1") is True


class TestPatternAlert:
    def test_fires_at_threshold_not_only_on_exactly_three(self):
        fake = MagicMock()
        fake.incr.return_value = 7  # counter jumped past 3
        fake.set.return_value = True  # alerted-flag NX succeeds
        with patch("app.core.redis_client.get_redis", return_value=fake):
            assert ci_mod._track_failure_pattern("o/r", "build", "flaky") is True

    def test_does_not_refire_inside_the_window(self):
        fake = MagicMock()
        fake.incr.return_value = 9
        fake.set.return_value = None  # alerted flag already set
        with patch("app.core.redis_client.get_redis", return_value=fake):
            assert ci_mod._track_failure_pattern("o/r", "build", "flaky") is False

    def test_below_threshold_does_not_fire(self):
        fake = MagicMock()
        fake.incr.return_value = 2
        with patch("app.core.redis_client.get_redis", return_value=fake):
            assert ci_mod._track_failure_pattern("o/r", "build", "flaky") is False

    def test_window_starts_on_first_failure_only(self):
        """expire() on every incr meant the 24h window never actually rolled."""
        fake = MagicMock()
        fake.incr.return_value = 1
        with patch("app.core.redis_client.get_redis", return_value=fake):
            ci_mod._track_failure_pattern("o/r", "build", "x")
        fake.expire.assert_called_once()

        fake2 = MagicMock()
        fake2.incr.return_value = 2
        with patch("app.core.redis_client.get_redis", return_value=fake2):
            ci_mod._track_failure_pattern("o/r", "build", "x")
        fake2.expire.assert_not_called()


# ── Review batching ───────────────────────────────────────────────────────────


class TestBatchedReview:
    def test_four_files_cost_one_llm_call(self):
        """Was one call per file: a 4-file PR open cost ~7 LLM calls total."""
        files = [
            {"filename": f"app/m{i}.py", "patch": f"@@ -1,1 +1,1 @@\n-x = 0\n+x = {i}\n"}
            for i in range(4)
        ]
        payload = {
            "files": [
                {"file": f"app/m{i}.py", "score": 8, "issues": [], "summary": f"file {i} fine"}
                for i in range(4)
            ]
        }
        cfg = MagicMock()
        cfg.footer = ""
        cfg.get.return_value = 4

        with patch.object(pr_mod.router, "ask", return_value=(payload, MagicMock())) as ask:
            md, _inline = pr_mod._review_code(
                {"head": {"sha": "s"}}, "o/r", 1, files, "t", cfg, MagicMock(), "", MagicMock()
            )

        assert ask.call_count == 1
        assert "file 0 fine" in md
        assert "file 3 fine" in md

    def test_unknown_filename_in_response_is_ignored(self):
        """The model can hallucinate a filename — don't render a review for it."""
        files = [{"filename": "app/real.py", "patch": "@@ -1,1 +1,1 @@\n-x = 0\n+x = 1\n"}]
        payload = {
            "files": [
                {"file": "app/real.py", "score": 9, "issues": [], "summary": "real file"},
                {"file": "app/invented.py", "score": 2, "issues": [], "summary": "not a file"},
            ]
        }
        cfg = MagicMock()
        cfg.footer = ""
        cfg.get.return_value = 4

        with patch.object(pr_mod.router, "ask", return_value=(payload, MagicMock())):
            md, _inline = pr_mod._review_code(
                {"head": {"sha": "s"}}, "o/r", 1, files, "t", cfg, MagicMock(), "", MagicMock()
            )

        assert "real file" in md
        assert "invented.py" not in md

    def test_unusable_batch_produces_no_review(self):
        files = [{"filename": "app/a.py", "patch": "@@ -1,1 +1,1 @@\n-x = 0\n+x = 1\n"}]
        cfg = MagicMock()
        cfg.footer = ""
        cfg.get.return_value = 4

        with patch.object(pr_mod.router, "ask", return_value=({"raw": "nope"}, MagicMock())):
            md, inline = pr_mod._review_code(
                {"head": {"sha": "s"}}, "o/r", 1, files, "t", cfg, MagicMock(), "", MagicMock()
            )

        assert md == ""
        assert inline == []
