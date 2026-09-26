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
            # Sentences whose punctuation fooled the word-list heuristic this
            # replaced: the `=` inside `>=` and `==` read as a code marker.
            "Should be >= not >",
            "Compare with is None rather than ==",
            "Note: this value should be validated before use",
            "Break this into two functions",
            # Prose WITH code in it is still prose — committing it literally
            # would be a syntax error.
            "use params.get('uid')",
            # A model declining to answer. `n/a` parses as one name divided by
            # another, so a parser alone calls it code.
            "n/a",
            "N/A",
            "TBD",
            "no fix",
        ],
    )
    def test_prose_is_not_committable(self, fix):
        assert make_suggestion_block(fix, 1, self.LINES) == ""

    @pytest.mark.parametrize(
        "fix",
        [
            "y = 2",
            "x = sanitize(x)",
            "params.get('uid')",
            "return user.name if user else None",
            "if user is None: raise ValueError(uid)",
            "await flush",
            "None",
            # Fragments that need context to parse — a decorator needs a def
            # under it, a block header needs a body, `except` needs a `try`.
            "@property",
            "def f(self) -> int:",
            "except (TypeError, ValueError):",
            "elif amount > 0:",
            "timeout=self.timeout",
            # The common JS/TS single-line forms.
            "const limit = 10;",
            "obj.close();",
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


class TestReviewFalsePositiveSources:
    """Four ways the review said something untrue about the code, none of which
    involved the model being wrong."""

    def test_a_moved_anchor_gets_no_commit_button(self):
        """
        `nearest_commentable` may move a finding up to five lines to reach a
        line GitHub accepts. A ```suggestion REPLACES the line it sits on, so on
        a moved anchor the button commits the fix over a different statement —
        presented by GitHub as a reviewed patch.
        """
        # Only line 1 is commentable; the model reports line 4.
        md, inline = _review(
            {
                "files": [
                    _entry(
                        issues=[
                            {
                                "severity": "major",
                                "line": "4",
                                "issue": "unbounded read",
                                "fix": "size = min(size, MAX)",
                            }
                        ]
                    )
                ]
            }
        )
        assert len(inline) == 1
        body = inline[0]["body"]
        assert inline[0]["line"] == 1, "expected the anchor to have moved"
        assert "```suggestion" not in body, "committable suggestion built on a moved anchor"
        assert "size = min(size, MAX)" in body, "the fix must still be shown"
        assert "Reported at line 4" in body, "the drift must be stated, not hidden"

    def test_an_exact_anchor_still_gets_a_commit_button(self):
        _md, inline = _review(
            {
                "files": [
                    _entry(
                        issues=[
                            {
                                "severity": "major",
                                "line": "1",
                                "issue": "off by one",
                                "fix": "x = 2",
                            }
                        ]
                    )
                ]
            }
        )
        assert "```suggestion" in inline[0]["body"]
        assert "Reported at line" not in inline[0]["body"]

    def test_the_per_file_cap_keeps_the_worst_findings(self):
        """`issues[:4]` in model order dropped a critical listed fifth and kept
        four nits above it."""
        issues = [
            {"severity": "nit", "line": "1", "issue": f"nit {n}", "fix": ""} for n in range(4)
        ] + [
            {
                "severity": "critical",
                "line": "1",
                "issue": "remote code execution",
                "fix": "",
            }
        ]
        md, inline = _review({"files": [_entry(issues=issues)]})
        everything = md + "\n".join(c["body"] for c in inline)
        assert "remote code execution" in everything, "the critical finding was dropped for nits"

    def test_a_truncated_diff_says_so(self):
        """The model treated the end of the 3,000-character slice as the end of
        the code and reported what was missing past the cut."""
        big = "@@ -1,1 +1,900 @@\n" + "\n".join(f"+line_{n} = {n}" for n in range(900))
        assert len(big) > 3000
        sent = []
        with patch.object(
            pr_mod.router,
            "ask",
            side_effect=lambda s, u, **k: (sent.append(u), ({"files": []}, MagicMock()))[1],
        ):
            pr_mod._review_code(
                {"head": {"sha": "s"}},
                "o/r",
                1,
                [{"filename": "app/a.py", "patch": big}],
                "t",
                _cfg(),
                ConfidenceGate(None),
                "",
                MagicMock(),
            )
        assert "Do not report anything as missing" in sent[0]
        # The note must sit in the prompt's own voice: the diff is wrapped as
        # UNTRUSTED content the model is told never to obey, so an instruction
        # placed inside the delimiters is one it is being told to ignore.
        before_diff = sent[0].split("BEGIN")[0]
        assert "Do not report anything as missing" in before_diff

    def test_a_short_diff_gets_no_truncation_marker(self):
        sent = []
        with patch.object(
            pr_mod.router,
            "ask",
            side_effect=lambda s, u, **k: (sent.append(u), ({"files": []}, MagicMock()))[1],
        ):
            pr_mod._review_code(
                {"head": {"sha": "s"}},
                "o/r",
                1,
                FILES,
                "t",
                _cfg(),
                ConfidenceGate(None),
                "",
                MagicMock(),
            )
        assert "Only the first" not in sent[0]

    def test_the_prompt_does_not_offer_a_severity_it_forbids(self):
        """The prompt says "Do NOT generate ... style nitpicks" and offered
        `nit` as a severity in the same breath."""
        sent = []
        with patch.object(
            pr_mod.router,
            "ask",
            side_effect=lambda s, u, **k: (sent.append(u), ({"files": []}, MagicMock()))[1],
        ):
            pr_mod._review_code(
                {"head": {"sha": "s"}},
                "o/r",
                1,
                FILES,
                "t",
                _cfg(),
                ConfidenceGate(None),
                "",
                MagicMock(),
            )
        assert "critical|major|minor" in sent[0]
        assert "|nit" not in sent[0]


class TestFileClassification:
    """Substring matching on filenames, in the two functions that decide what
    gets reviewed at all."""

    @pytest.mark.parametrize(
        "name",
        [
            "app/core/latest_run.py",
            "app/latest_config.py",
            "app/contest_entry.py",
            "src/greatest_hits.ts",
            "app/fastest_path.py",
            "app/protest_form.py",
            "testimonials/page.tsx",
            "app/latest/run.py",
        ],
    )
    def test_test_in_the_middle_of_a_word_is_not_a_test_file(self, name):
        """ "latest_" contains "test_". Such a file was pushed down the review
        budget, dropped from gap detection's SOURCE files, and counted as a TEST
        file — so a PR touching only one reported "tests changed in this PR"."""
        from app.handlers.pull_request.classify import _is_test_file

        assert not _is_test_file(name)

    @pytest.mark.parametrize(
        "name",
        [
            "tests/test_a.py",
            "app/a_test.py",
            "test_top.py",
            "pkg/tests/helpers.py",
            "app/test/x.py",
            "tests.py",
            "api/handlers_test.go",
            "src/__tests__/a.ts",
        ],
    )
    def test_real_test_files_are_still_caught(self, name):
        from app.handlers.pull_request.classify import _is_test_file

        assert _is_test_file(name)

    @pytest.mark.parametrize(
        "name",
        [
            "package-lock.json",
            "pnpm-lock.yaml",
            "npm-shrinkwrap.json",
            "api/schema.pb.go",
            "src/api_pb2.py",
            "dist/bundle.js",
            "build/out.js",
            "node_modules/x/index.js",
            "a.min.js",
            "__snapshots__/a.snap",
        ],
    )
    def test_generated_files_are_not_reviewed(self, name):
        """`package-lock.json` scored as CONFIG and `dist/bundle.js` and
        `schema.pb.go` scored as SOURCE — priority 3, ahead of hand-written
        code — so a machine-written diff could take a slot in the four-file
        budget and be reviewed."""
        from app.handlers.pull_request.classify import _is_generated

        assert _is_generated(name)

    @pytest.mark.parametrize(
        "name",
        [
            "app/main.py",
            "src/dist_helper.py",
            "app/distance.py",
            "app/migrations/0003_auto.py",
            "docs/guide.md",
        ],
    )
    def test_hand_written_files_are_still_reviewed(self, name):
        """Migrations are deliberately reviewable: generated, but routinely
        hand-edited, and a destructive one is exactly what needs a reader."""
        from app.handlers.pull_request.classify import _is_generated

        assert not _is_generated(name)


class TestJunkFieldDoesNotDiscardTheAnalysis:
    """
    A string method called straight on a model field — `.upper()`, `.replace()`,
    `.strip()` — raises when the key is present holding a number, a list or
    null. Every one of these sites sits inside a blanket `except Exception` that
    returns an error string, so a single odd field threw away an analysis that
    had already been produced and paid for.
    """

    def test_arch_review_survives_junk_in_every_field(self):
        from app.ai import guarded
        from app.handlers.comments import generator as gen

        payload = {
            "health": 3,  # not a string
            "refactoring_priority": None,
            "summary": "Layering is mostly respected.",
            "positive_patterns": "not a list",
            "violations": [
                {
                    "type": ["layer_violation"],  # not a string
                    "severity": 1,
                    "location": None,
                    "description": "handlers import from core internals",
                    "recommendation": 42,
                },
                "not a dict",
            ],
        }
        with (
            patch.object(gen, "guarded_ask", return_value=(payload, None)),
            patch.object(gen, "is_degraded", return_value=False),
            patch.object(guarded, "is_degraded", return_value=False),
        ):
            out = gen.cmd_arch("o/r", 1, {"title": "t", "body": "b"}, "tok")

        assert "Architecture Review" in out
        assert "handlers import from core internals" in out, "the finding was discarded"
        assert "Error" not in out

    def test_mcp_security_review_survives_junk_in_every_field(self):
        from app.mcp import handlers as mh

        payload = {
            "risk_level": None,
            "findings": [
                {"issue": "hardcoded token", "severity": 9, "fix": None},
                "not a dict",
            ],
            "cve_risks": {"not": "a list"},
            "summary": "",
        }
        with (
            patch.object(mh, "_installation_allowed", return_value=True),
            patch("app.ai.router.router.ask", return_value=(payload, MagicMock())),
        ):
            out = mh._handle_security_review({"content": "token = 'abc'"})

        assert "hardcoded token" in out, "the finding was discarded"
        assert not out.startswith("Error")

    def test_release_survives_a_numeric_version(self):
        from app.handlers.comments import _cmd_release

        plan = {
            "version": 1.2,  # a number, not "v1.2.0"
            "title": "t",
            "highlights": [],
            "breaking_changes": [],
            "release_notes": "n",
        }
        with (
            patch("app.handlers.comments.router.ask", return_value=(plan, MagicMock())),
            patch(
                "app.handlers.comments.gh_get",
                side_effect=[
                    [{"name": "v1.1.0"}],
                    [{"sha": "a", "commit": {"message": "feat: x"}}],
                ],
            ),
            patch(
                "app.handlers.comments.gh_post", return_value={"html_url": "u", "number": 1}
            ) as post,
        ):
            out = _cmd_release("o/r", "t", "a")

        post.assert_called_once()
        # A bad version falls back to a patch bump rather than losing the draft.
        assert post.call_args[0][2]["tag_name"] == "v1.1.1"
        assert "Error" not in out and "Failed" not in out

    def test_as_text_coerces_without_raising(self):
        from app.ai.validator import as_text

        assert as_text(None, "low") == "low"
        assert as_text("", "low") == "low"
        assert as_text("high") == "high"
        assert as_text(3) == "3"
        assert as_text(["a"]) == "['a']"
        assert as_text(None) == ""
        # The point of it: these must not raise.
        for v in (None, "", 3, 3.5, ["a"], {"a": 1}, True):
            as_text(v).upper().replace("_", " ").strip()


class TestATimeoutDoesNotLoseTheWholeReport:
    """
    `_request` wrapped only `ConnectionError`. A read timeout is not one — it is
    a sibling under `RequestException` — so it escaped the GitHub client
    entirely, and every caller in this codebase is written against a single
    failure type.
    """

    def test_a_read_timeout_is_a_github_error(self):
        import requests

        from app.github import client

        with (
            patch.object(client, "check_and_wait"),
            patch.object(
                client._session, "request", side_effect=requests.exceptions.ReadTimeout("slow")
            ),
        ):
            with pytest.raises(client.GitHubError) as exc:
                client.gh_get("/repos/o/r", "tok")
        assert exc.value.status_code == 0
        assert "ReadTimeout" in str(exc.value)

    def test_the_report_survives_a_failed_inline_post(self):
        """_post_inline_review runs BEFORE the sticky report is built, so
        anything escaping it discards the analysis, summary and gaps too."""
        import requests

        from app.handlers.pull_request.review import _post_inline_review

        comments = [
            {
                "path": "app/a.py",
                "line": 1,
                "side": "RIGHT",
                "body": "**CRITICAL** — SQL injection",
                "_fallback_md": "- **CRITICAL** `app/a.py:1`: SQL injection",
            }
        ]
        cfg = MagicMock()
        cfg.footer = ""
        with patch(
            "app.handlers.pull_request.review.gh_post",
            side_effect=requests.exceptions.ReadTimeout("slow"),
        ):
            recovered = _post_inline_review(
                {"head": {"sha": "s"}}, "o/r", 1, "t", cfg, comments, MagicMock()
            )
        assert "SQL injection" in recovered, "the finding was lost with the failed post"

    def test_a_successful_post_recovers_nothing(self):
        from app.handlers.pull_request.review import _post_inline_review

        cfg = MagicMock()
        cfg.footer = ""
        comments = [{"path": "a.py", "line": 1, "side": "RIGHT", "body": "b", "_fallback_md": "m"}]
        with patch("app.handlers.pull_request.review.gh_post", return_value={}):
            assert (
                _post_inline_review(
                    {"head": {"sha": "s"}}, "o/r", 1, "t", cfg, comments, MagicMock()
                )
                == ""
            )
