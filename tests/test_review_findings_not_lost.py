"""
Regressions for the review pipeline, all of which shipped and all of which made
the bot's output wrong rather than merely absent.

Each class names the defect it pins. Reproduced first against the unfixed code —
none of them was caught by the 2,609 tests that were passing at the time.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.core.confidence import ConfidenceGate
from app.github.patch_parser import commentable_lines, make_suggestion_block
from app.handlers.comments import reviewer as rv
from app.handlers.pull_request import gaps as gaps_mod
from app.handlers import pull_request as pr_mod

PATCH = "@@ -1,2 +1,2 @@\n-x = 0\n+x = 1\n"
FILES = [{"filename": "app/a.py", "patch": PATCH, "status": "modified"}]


def _cfg():
    c = MagicMock()
    c.footer = ""
    c.get.return_value = 4
    return c


def _entry(**over):
    e = {
        "file": "app/a.py",
        "score": 3,
        "summary": "Concatenates request input straight into the SQL string.",
        "issues": [
            {
                "severity": "critical",
                "line": "1",
                "issue": "SQL injection via string concatenation",
                "fix": "cur.execute(q, (uid,))",
            }
        ],
    }
    e.update(over)
    return e


def _review(batch, gate=None):
    with patch.object(pr_mod.router, "ask", return_value=(batch, MagicMock())):
        return pr_mod._review_code(
            {"head": {"sha": "s"}},
            "o/r",
            1,
            FILES,
            "t",
            _cfg(),
            gate or ConfidenceGate(None),
            "",
            MagicMock(),
        )


class TestDemotionNeverDeletesAFinding:
    """
    A low-confidence file had its inline comments dropped AFTER the per-file
    markdown was built. The markdown renders "All findings posted as inline
    comments" whenever every finding anchored, so a demoted file lost its
    findings from the diff and from the report at once — and the report then
    asserted they were on the diff. A critical finding could be published
    nowhere at all.
    """

    def test_demoted_finding_still_appears_in_the_report(self):
        # A summary under _MIN_FIELD_CHARS is enough to demote the file.
        md, inline = _review({"files": [_entry(summary="Bad")]})
        assert inline == [], "expected the inline comments to be suppressed"
        assert "SQL injection" in md, "the finding was deleted, not demoted"

    def test_demoted_file_does_not_claim_its_findings_were_posted(self):
        md, inline = _review({"files": [_entry(summary="Bad")]})
        assert not inline
        assert "All findings posted as inline comments" not in md

    def test_confident_file_still_anchors_and_stays_out_of_the_body(self):
        md, inline = _review({"files": [_entry()]})
        assert len(inline) == 1
        assert inline[0]["path"] == "app/a.py"
        assert "All findings posted as inline comments" in md

    def test_every_finding_reaches_the_reader_either_way(self):
        """The invariant behind both branches, stated once."""
        for summary in ("Bad", "Concatenates request input straight into SQL."):
            md, inline = _review({"files": [_entry(summary=summary)]})
            in_body = "SQL injection" in md
            in_diff = any("SQL injection" in c["body"] for c in inline)
            assert in_body or in_diff, f"finding lost entirely for summary={summary!r}"


class TestBatchConfidenceIsRead:
    """
    The batch prompt asks for ONE confidence for the whole response. Each
    per-file entry therefore reached validate_code_review() without the key and
    took the 0.5 default, so the self-reported term the gate weights was a
    constant for every file of every PR ever reviewed.
    """

    def test_batch_confidence_reaches_the_gate(self):
        seen = []
        gate = MagicMock()
        gate.evaluate.side_effect = lambda action, r, **kw: (
            seen.append(r.get("confidence")),
            {"auto_apply": True, "confidence_score": 0.9},
        )[1]
        _review({"files": [_entry()], "confidence": 0.93}, gate=gate)
        assert seen == [0.93]

    def test_a_per_file_confidence_still_wins(self):
        seen = []
        gate = MagicMock()
        gate.evaluate.side_effect = lambda action, r, **kw: (
            seen.append(r.get("confidence")),
            {"auto_apply": True, "confidence_score": 0.9},
        )[1]
        _review({"files": [_entry(confidence=0.4)], "confidence": 0.93}, gate=gate)
        assert seen == [0.4]


class TestScoreZeroSurvives:
    """`r.get("score") or 8` turned the one score that means "do not merge"
    into a passing grade."""

    @pytest.mark.parametrize("given,shown", [(0, "0/10"), (2, "2/10"), (7.5, "7.5/10")])
    def test_score_is_rendered_as_given(self, given, shown):
        md, _ = _review({"files": [_entry(score=given, issues=[])]})
        assert f"Score: {shown}" in md

    def test_a_missing_score_still_falls_back(self):
        entry = _entry(issues=[])
        entry.pop("score")
        md, _ = _review({"files": [entry]})
        assert "Score: 7/10" in md  # validator's documented default


class TestNoCommittableProse:
    """
    GitHub renders a ```suggestion block as a one-click Commit button. The
    review prompt asks for an "exact fix" and models answer with an instruction
    about as often as with code, so this shipped buttons that replace working
    code with an English sentence.
    """

    LINES = commentable_lines(PATCH)

    @pytest.mark.parametrize(
        "fix",
        [
            "Add a null check before dereferencing user",
            "Use a parameterised query",
            "Remove this line",
            "Consider extracting this into a helper.",
            "The variable is never used.",
            "Validate the input first",
        ],
    )
    def test_prose_is_not_committable(self, fix):
        assert make_suggestion_block(fix, 1, self.LINES) == ""

    @pytest.mark.parametrize(
        "fix",
        [
            "y = 2",
            "x = sanitize(x)",
            "use params.get('uid')",
            "return user.name if user else None",
            "if user is None: raise ValueError(uid)",
            "await flush",
            "None",
        ],
    )
    def test_code_is_still_committable(self, fix):
        assert make_suggestion_block(fix, 1, self.LINES).startswith("```suggestion")

    def test_a_rejected_suggestion_still_shows_the_fix(self):
        md, inline = _review(
            {
                "files": [
                    _entry(
                        issues=[
                            {
                                "severity": "major",
                                "line": "1",
                                "issue": "unchecked deref",
                                "fix": "Add a null check before dereferencing user",
                            }
                        ]
                    )
                ]
            }
        )
        body = "\n".join(c["body"] for c in inline) + md
        assert "Add a null check" in body, "the fix must still be shown, just not as a button"
        assert "```suggestion" not in body


class TestGapSectionSurvivesItsOwnInput:
    def _gaps(self, payload, files=None, log=None):
        with patch.object(gaps_mod.router, "ask", return_value=(payload, MagicMock())):
            return gaps_mod._detect_test_gaps(
                {},
                "o/r",
                1,
                files if files is not None else FILES,
                "t",
                _cfg(),
                log or MagicMock(),
            )

    BASE = {
        "has_gaps": True,
        "summary": "s",
        "gaps": [
            {
                "file": "app/a.py",
                "function": "f",
                "risk": "high",
                "suggested_test": "cover the raise",
            }
        ],
    }

    @pytest.mark.parametrize("score", ["6", None, "high", 12, -3, 7.5])
    def test_any_coverage_score_still_renders_the_gaps(self, score):
        """`score >= 8` on a string raised inside the try, and the whole
        section — every gap it had just found — vanished behind a log line."""
        log = MagicMock()
        out = self._gaps({**self.BASE, "coverage_score": score}, log=log)
        assert "cover the raise" in out, f"section lost for coverage_score={score!r}"
        assert log.error.call_count == 0

    def test_gap_against_a_file_not_in_the_pr_is_dropped(self):
        out = self._gaps(
            {
                **self.BASE,
                "coverage_score": 4,
                "gaps": [
                    {
                        "file": "app/invented.py",
                        "function": "ghost",
                        "risk": "high",
                        "suggested_test": "t",
                    }
                ],
            }
        )
        assert out == "", "a gap naming a file the PR never touched was rendered"

    def test_a_real_gap_alongside_an_invented_one_still_renders(self):
        out = self._gaps(
            {
                **self.BASE,
                "coverage_score": 4,
                "gaps": [
                    {
                        "file": "app/invented.py",
                        "function": "ghost",
                        "risk": "high",
                        "suggested_test": "invented",
                    },
                    {
                        "file": "app/a.py",
                        "function": "f",
                        "risk": "high",
                        "suggested_test": "genuine",
                    },
                ],
            }
        )
        assert "genuine" in out
        assert "invented" not in out

    def test_risk_outside_the_enum_is_normalised(self):
        out = self._gaps(
            {
                **self.BASE,
                "coverage_score": 4,
                "gaps": [
                    {
                        "file": "app/a.py",
                        "function": "f",
                        "risk": "CATASTROPHIC",
                        "suggested_test": "t",
                    }
                ],
            }
        )
        assert "CATASTROPHIC" not in out
        assert "`medium`" in out


class TestChangelogStopsAtTheTag:
    """`_fetch_commits_since_tag` fetched the last N commits and used the tag
    only in the prompt, so every /changelog re-described released work."""

    @staticmethod
    def _gh(tag_sha, commits):
        def gh(path, token, *a, **kw):
            if "/tags" in path:
                return [{"name": "v9.9.9", "commit": {"sha": tag_sha}}] if tag_sha else []
            return commits

        return gh

    COMMITS = [
        {"sha": "NEW", "commit": {"message": "unreleased work"}},
        {"sha": "TAGGED", "commit": {"message": "shipped in v9.9.9"}},
        {"sha": "OLDER", "commit": {"message": "shipped even earlier"}},
    ]

    def _changelog(self, gh):
        prompts = []
        with (
            patch.object(rv, "gh_get", side_effect=gh),
            patch.object(
                rv.router,
                "ask_text",
                side_effect=lambda s, u, **k: (prompts.append(u), ("## [9.9.10]", "m"))[1],
            ),
        ):
            out = rv.cmd_changelog("o/r", "t")
        return out, prompts

    def test_released_commits_are_excluded(self):
        _out, prompts = self._changelog(self._gh("TAGGED", self.COMMITS))
        assert prompts, "expected the model to be asked"
        assert "unreleased work" in prompts[0]
        assert "shipped in v9.9.9" not in prompts[0]
        assert "shipped even earlier" not in prompts[0]

    def test_nothing_new_asks_the_model_nothing(self):
        out, prompts = self._changelog(
            self._gh("NEW", [{"sha": "NEW", "commit": {"message": "the tagged commit"}}])
        )
        assert prompts == [], "spent an LLM call on an empty changelog"
        assert "v9.9.9" in out and "No New Commits" in out

    def test_a_tag_outside_the_window_keeps_every_commit(self):
        """A tag older than the fetched page is invisible, not empty."""
        _out, prompts = self._changelog(self._gh("ANCIENT", self.COMMITS))
        assert "unreleased work" in prompts[0]
        assert "shipped even earlier" in prompts[0]

    def test_an_untagged_repo_keeps_every_commit(self):
        _out, prompts = self._changelog(self._gh(None, self.COMMITS))
        assert "unreleased work" in prompts[0]


class TestNonNumericConfidenceKeepsTheAnalysis:
    """A model answering `"confidence": "high"` raised inside cmd_ci's try —
    after a good root cause and fix had been produced — and the blanket handler
    replaced all of it with "CI Analysis Failed"."""

    def _ci(self, confidence):
        from app.ai import guarded

        payload = {
            "root_cause": "the lockfile pins a yanked release",
            "fix": "pip install --upgrade x",
            "prevention": "pin by hash",
            "confidence": confidence,
        }
        with (
            patch.object(guarded, "guarded_ask", return_value=(payload, None)),
            patch.object(guarded, "is_degraded", return_value=False),
        ):
            return rv.cmd_ci("a failing log", "o/r", "t")

    @pytest.mark.parametrize("confidence", ["high", None, "", {}, "0.9x"])
    def test_analysis_survives_a_junk_confidence(self, confidence):
        out = self._ci(confidence)
        assert "yanked release" in out
        assert "Failed" not in out
        assert "Confidence:" not in out, "invented a percentage the model never gave"

    def test_a_real_confidence_is_still_shown(self):
        assert "Confidence: 90%" in self._ci(0.9)


class TestReleaseStopsAtTheTag:
    """`/release` said "commits since last tag" in its docstring and listed the
    last 20 commits on the branch regardless, so every draft re-announced work
    already shipped in that tag. It also duplicated the fetch instead of using
    the shared helper, which is how the two drifted apart."""

    COMMITS = [
        {"sha": "NEW", "commit": {"message": "feat: unreleased thing"}},
        {"sha": "TAGGED", "commit": {"message": "feat: shipped in v1.1.0"}},
    ]

    def _release(self, tags, commits):
        from app.handlers.comments import _cmd_release

        plan = {
            "version": "v1.2.0",
            "title": "t",
            "highlights": [],
            "breaking_changes": [],
            "release_notes": "n",
        }
        prompts = []
        with (
            patch(
                "app.handlers.comments.router.ask",
                side_effect=lambda s, u, **k: (prompts.append(u), (plan, MagicMock()))[1],
            ),
            patch("app.handlers.comments.gh_get", side_effect=[tags, commits]),
            patch(
                "app.handlers.comments.gh_post",
                return_value={"html_url": "u", "number": 1},
            ) as post,
        ):
            out = _cmd_release("o/r", "t", "a")
        return out, prompts, post

    def test_released_commits_are_not_re_announced(self):
        _out, prompts, _post = self._release(
            [{"name": "v1.1.0", "commit": {"sha": "TAGGED"}}], self.COMMITS
        )
        assert "unreleased thing" in prompts[0]
        assert "shipped in v1.1.0" not in prompts[0]

    def test_nothing_new_drafts_nothing(self):
        out, prompts, post = self._release(
            [{"name": "v1.1.0", "commit": {"sha": "TAGGED"}}],
            [{"sha": "TAGGED", "commit": {"message": "feat: shipped in v1.1.0"}}],
        )
        assert prompts == [], "spent an LLM call with nothing to release"
        post.assert_not_called()
        assert "Nothing to Release" in out and "v1.1.0" in out

    def test_a_tag_payload_without_a_sha_still_works(self):
        """The shape the existing suite mocks — no `commit` key at all."""
        _out, prompts, _post = self._release([{"name": "v1.1.0"}], self.COMMITS)
        assert "unreleased thing" in prompts[0]
        assert "shipped in v1.1.0" in prompts[0]
