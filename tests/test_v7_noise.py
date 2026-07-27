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
