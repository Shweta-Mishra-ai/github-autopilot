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

    # Demotion is forced with a stub gate rather than by picking inputs the
    # real weights happen to score low. What these assert — that demotion never
    # deletes — has to hold whatever triggers it, and it must not start passing
    # vacuously the next time the weights are tuned.
    @staticmethod
    def _demoting_gate():
        g = MagicMock()
        g.evaluate.return_value = {"auto_apply": False, "confidence_score": 0.4}
        return g

    def test_demoted_finding_still_appears_in_the_report(self):
        md, inline = _review({"files": [_entry()]}, gate=self._demoting_gate())
        assert inline == [], "expected the inline comments to be suppressed"
        assert "SQL injection" in md, "the finding was deleted, not demoted"

    def test_demoted_file_does_not_claim_its_findings_were_posted(self):
        md, inline = _review({"files": [_entry()]}, gate=self._demoting_gate())
        assert not inline
        assert "All findings posted as inline comments" not in md

    def test_demoted_file_says_why(self):
        md, _inline = _review({"files": [_entry()]}, gate=self._demoting_gate())
        assert "Confidence 40%" in md

    def test_confident_file_still_anchors_and_stays_out_of_the_body(self):
        md, inline = _review({"files": [_entry()]})
        assert len(inline) == 1
        assert inline[0]["path"] == "app/a.py"
        assert "All findings posted as inline comments" in md

    def test_every_finding_reaches_the_reader_either_way(self):
        """The invariant behind both branches, stated once."""
        accepting = MagicMock()
        accepting.evaluate.return_value = {"auto_apply": True, "confidence_score": 0.95}
        for name, gate in (("demoting", self._demoting_gate()), ("accepting", accepting)):
            md, inline = _review({"files": [_entry()]}, gate=gate)
            in_body = "SQL injection" in md
            in_diff = any("SQL injection" in c["body"] for c in inline)
            assert in_body or in_diff, f"finding lost entirely with a {name} gate"
            assert not (in_body and in_diff), f"finding reported twice with a {name} gate"


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


class TestNoInventedScore:
    """
    The per-file heading was "Score: N/10", where N was a number the prompt's
    own example put at 8 and the validator defaulted to 7 when absent. It now
    states what was actually found.
    """

    @pytest.mark.parametrize("given", [0, 2, 7.5, None, "high"])
    def test_heading_never_carries_a_mark_out_of_ten(self, given):
        md, _ = _review({"files": [_entry(score=given)]})
        assert "/10" not in md
        assert "1 critical" in md

    def test_a_clean_file_says_so(self):
        md, _ = _review({"files": [_entry(issues=[])]})
        assert "no issues found" in md and "/10" not in md

    def test_counts_are_worst_first_and_hidden_findings_are_disclosed(self):
        issues = [
            {"severity": s, "line": "", "issue": f"problem {n}", "fix": ""}
            for n, s in enumerate(["minor", "critical", "minor", "major", "minor", "minor"])
        ]
        md, _ = _review({"files": [_entry(issues=issues)]})
        assert "1 critical, 1 major, 4 minor" in md
        assert "2 less severe finding(s) not shown" in md


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
            pytest.raises(client.GitHubError) as exc,
        ):
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


class TestCompletenessIsNotAWritingStyle:
    """
    The gate scored "did the model answer" as "is the per-file summary at least
    ten characters" — binary, on a term worth 38% of the score. A perfectly
    anchored critical finding beside the summary "Bad" scored 0.500, exactly the
    same as a review none of whose findings anchored, and was demoted.
    """

    def test_a_terse_summary_beside_a_real_finding_is_not_demoted(self):
        md, inline = _review({"files": [_entry(summary="Bad")]})
        assert len(inline) == 1, "a well-anchored finding was demoted over summary length"
        assert "Confidence" not in md

    def test_findings_that_do_not_anchor_are_still_demoted(self):
        """The signal that IS evidence of guessing must keep working."""
        from app.core.confidence import compute_confidence
        from app.handlers.pull_request.review import _review_completeness

        r = _entry()
        score = compute_confidence(
            {**r, "confidence": 0.5}, anchor_rate=0.0, completeness=_review_completeness(r)
        )
        assert score < 0.70

    def test_there_is_no_cliff_at_ten_characters(self):
        from app.core.confidence import compute_confidence

        nine = compute_confidence({"summary": "x" * 9}, required_fields=("summary",))
        ten = compute_confidence({"summary": "x" * 10}, required_fields=("summary",))
        assert ten - nine < 0.1, f"a single character moved the score by {ten - nine:.3f}"

    def test_a_clean_bill_from_a_model_that_said_nothing_is_still_doubted(self):
        from app.handlers.pull_request.review import _review_completeness

        assert _review_completeness({"summary": "", "issues": []}) == 0.0

    def test_the_longer_of_summary_and_findings_counts(self):
        from app.handlers.pull_request.review import _review_completeness

        assert _review_completeness({"summary": "Bad", "issues": [{"issue": "x" * 40}]}) == 1.0
        assert _review_completeness({"summary": "x" * 40, "issues": []}) == 1.0
        assert _review_completeness({"summary": "Bad", "issues": []}) == 0.3

    def test_an_explicit_completeness_overrides_required_fields(self):
        from app.core.confidence import compute_confidence

        a = compute_confidence({"summary": ""}, required_fields=("summary",), completeness=1.0)
        b = compute_confidence({"summary": "x" * 40}, required_fields=("summary",))
        assert a == b


class TestHallucinationCheckerFalsePositives:
    """The checker's own extractors produced most of what it flagged."""

    PROSE = (
        "Calling cur.execute with user.name interpolated is unsafe; use os.path.join "
        "and self.timeout, e.g. via settings.DEBUG. The request.json body and "
        "response.status_code are unchecked; data.get is fine."
    )

    def test_attribute_access_is_not_a_file_reference(self):
        from app.ai.hallucination import _extract_file_refs

        assert _extract_file_refs(self.PROSE) == []

    @pytest.mark.parametrize(
        "ref",
        ["app/core/config.py", "utils.py", "src/index.ts", "README.md", "docs/api.json"],
    )
    def test_real_file_references_are_still_found(self, ref):
        from app.ai.hallucination import _extract_file_refs

        assert _extract_file_refs(f"See {ref} for the details.") == [ref]

    def test_an_ambiguous_extension_needs_a_path(self):
        """`request.json` is an attribute; `config/settings.json` is a file."""
        from app.ai.hallucination import _extract_file_refs

        assert _extract_file_refs("read request.json first") == []
        assert _extract_file_refs("read config/settings.json first") == ["config/settings.json"]

    def test_a_correct_answer_is_not_penalised_for_its_prose(self):
        from app.ai.hallucination import check_response

        r = check_response(
            {"summary": self.PROSE + " See app/core/config.py."},
            context={"files": ["app/core/config.py"]},
        )
        assert r.confidence == 1.0, r.warnings

    @pytest.mark.parametrize("number", ["1048576", "20260823", "9876543"])
    def test_a_number_is_not_a_commit_sha(self, number):
        from app.ai.hallucination import _SHA_REF

        assert _SHA_REF.findall(f"the value was {number} at the time") == []

    @pytest.mark.parametrize("word", ["deadbeef", "defaced", "effaced"])
    def test_a_hex_spelled_word_is_not_a_commit_sha(self, word):
        from app.ai.hallucination import _SHA_REF

        assert _SHA_REF.findall(f"marked {word} here") == []

    @pytest.mark.parametrize(
        "sha", ["2f34f3b", "d0bb6f3", "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"]
    )
    def test_real_shas_are_still_found(self, sha):
        from app.ai.hallucination import _SHA_REF

        assert _SHA_REF.findall(f"fixed in {sha}.") == [sha]

    def test_nested_content_is_scanned(self):
        """Findings live at files[].issues[].issue — the scanner saw ''."""
        from app.ai.hallucination import _extract_text, check_response

        payload = {"files": [{"issues": [{"issue": "I'm not sure, [insert value here]"}]}]}
        assert "insert value here" in _extract_text(payload)
        assert check_response(payload).confidence < 1.0

    def test_extraction_is_depth_capped(self):
        from app.ai.hallucination import _extract_text

        deep: object = "bottom"
        for _ in range(50):
            deep = {"k": [deep]}
        _extract_text(deep)  # must not raise RecursionError


class TestRepoFileContext:
    """Ground truth for the file check is the repository, not the PR."""

    @staticmethod
    def _tree(paths, truncated=False):
        return {
            "truncated": truncated,
            "tree": [{"path": p, "type": "blob"} for p in paths]
            + [{"path": "app", "type": "tree"}],
        }

    def test_returns_every_blob_plus_the_pr_additions(self):
        from app.github.helpers import repo_file_context

        ctx = repo_file_context(
            "o/r",
            "t",
            extra=["app/new.py", None],
            get=lambda path, tok: self._tree(["app/a.py", "README.md"]),
        )
        assert ctx == {"files": ["README.md", "app/a.py", "app/new.py"]}

    def test_a_truncated_tree_disables_the_check(self):
        """It cannot prove a file is absent, so it must not penalise one."""
        from app.github.helpers import repo_file_context

        ctx = repo_file_context(
            "o/r", "t", get=lambda path, tok: self._tree(["a.py"], truncated=True)
        )
        assert ctx == {}

    @pytest.mark.parametrize("response", [RuntimeError("boom"), [], None, {"tree": "x"}])
    def test_anything_unexpected_disables_the_check(self, response):
        from app.github.helpers import repo_file_context

        def get(path, tok):
            if isinstance(response, Exception):
                raise response
            return response

        assert repo_file_context("o/r", "t", get=get) == {}

    def _run_impact(self, answer):
        """Run the real cmd_impact -> guarded_ask -> check_response path, and
        return the verdict guarded_ask actually computed plus the context it
        actually passed. Recording inside check_response, not beside it: an
        earlier version of this test computed its own verdict from a context
        guarded_ask never forwards, and passed vacuously."""
        from app.ai import guarded
        from app.ai import hallucination
        from app.handlers.comments import reviewer as rv

        pr_files = [{"filename": "app/auth.py"}]
        tree = self._tree(["app/auth.py", "app/views/login.py", "app/session.py"])

        def gh(path, tok, *a, **k):
            return tree if "/git/trees/" in path else pr_files

        seen = {}
        real_check = hallucination.check_response

        def recording_check(payload, context=None, response_type="generic"):
            seen["context"] = context
            seen["verdict"] = real_check(payload, context=context, response_type=response_type)
            return seen["verdict"]

        with (
            patch.object(rv, "gh_get", side_effect=gh),
            patch.object(guarded, "safe_router_ask", return_value=(answer, MagicMock())),
            patch.object(guarded, "check_response", side_effect=recording_check),
        ):
            out = rv.cmd_impact("o/r", 5, {"pull_request": {}}, "t")
        return out, seen

    IMPACT = {
        "affected_systems": ["auth"],
        "breaking_change_risk": "medium",
        "requires_migration": False,
        "review_priority": "high",
        "notes": "",
    }

    def test_impact_passes_the_whole_tree_as_context(self):
        _out, seen = self._run_impact({**self.IMPACT, "summary": "Touches auth."})
        assert seen["context"] is not None, "guarded_ask was given no context"
        assert "app/views/login.py" in seen["context"]["files"]

    def test_impact_names_files_outside_the_pr_without_penalty(self):
        """What a blast-radius answer is FOR. Against the PR's own files it
        would be flagged; against the tree it is simply correct."""
        out, seen = self._run_impact(
            {
                **self.IMPACT,
                "summary": "Touches auth; callers in app/views/login.py and app/session.py.",
            }
        )
        assert seen["verdict"].confidence == 1.0, seen["verdict"].warnings
        assert "Blast Radius" in out

    def test_impact_penalises_a_file_that_does_not_exist(self):
        _out, seen = self._run_impact(
            {**self.IMPACT, "summary": "Breaks app/does_not_exist.py and app/invented.py."}
        )
        assert seen["verdict"].confidence < 1.0
        assert any("does_not_exist" in w for w in seen["verdict"].warnings)

    def test_impact_flags_a_file_that_does_not_exist(self):
        from app.ai.hallucination import check_response

        r = check_response(
            {"summary": "Breaks app/does_not_exist.py and app/also_invented.py."},
            context={"files": ["app/auth.py"]},
        )
        assert r.confidence < 1.0
        assert any("does_not_exist" in w for w in r.warnings)


class TestCodeReviewHasItsHallucinationTerm:
    """
    The gate's heaviest term (0.35) was never supplied by _review_code, so every
    code review was scored on the three weaker terms and a response full of
    invented files or "I'm not sure" scored the same as a clean one.
    """

    TWO_FILES = [
        {"filename": "app/a.py", "patch": PATCH, "status": "modified"},
        {"filename": "app/b.py", "patch": PATCH, "status": "modified"},
    ]

    def _run(self, entries, tree_paths=("app/a.py", "app/b.py", "app/callers.py")):
        """Real _review_code with a recording gate and a counting tree fetch."""
        from app.github import helpers

        fetches = []

        def fake_ctx(repo, token, ref="HEAD", extra=(), get=None):
            fetches.append(ref)
            return {"files": sorted(set(tree_paths) | set(extra))}

        seen = []
        gate = MagicMock()
        gate.evaluate.side_effect = lambda action, r, **kw: (
            seen.append(kw.get("hallucination")),
            {"auto_apply": True, "confidence_score": 0.9},
        )[1]
        with (
            patch.object(pr_mod.router, "ask", return_value=({"files": entries}, MagicMock())),
            patch.object(helpers, "repo_file_context", side_effect=fake_ctx),
        ):
            pr_mod._review_code(
                {"head": {"sha": "HEADSHA"}},
                "o/r",
                1,
                self.TWO_FILES,
                "t",
                _cfg(),
                gate,
                "",
                MagicMock(),
            )
        return seen, fetches

    @staticmethod
    def _file(name, issue, summary="Reviewed the change to this file carefully."):
        return {
            "file": name,
            "score": 6,
            "summary": summary,
            "issues": [{"severity": "major", "line": "1", "issue": issue, "fix": "x = 2"}],
        }

    def test_the_gate_receives_a_hallucination_verdict(self):
        seen, _ = self._run([self._file("app/a.py", "off by one in the loop bound")])
        assert seen and seen[0] is not None, "code review still scored without the 0.35 term"

    def test_no_tree_fetch_when_every_reference_is_in_the_pr(self):
        """The common case pays nothing extra."""
        _seen, fetches = self._run(
            [self._file("app/a.py", "app/b.py calls this with a None default")]
        )
        assert fetches == []

    def test_one_tree_fetch_per_review_not_per_file(self):
        _seen, fetches = self._run(
            [
                self._file("app/a.py", "breaks app/callers.py which passes None"),
                self._file("app/b.py", "also breaks app/callers.py"),
            ]
        )
        assert fetches == ["HEADSHA"], "fetched per file, or not at the PR head"

    def test_a_real_caller_outside_the_pr_is_not_penalised(self):
        seen, _ = self._run([self._file("app/a.py", "breaks app/callers.py which passes None")])
        assert seen[0].confidence == 1.0, seen[0].warnings

    def test_an_invented_file_is_penalised(self):
        seen, _ = self._run([self._file("app/a.py", "breaks app/nowhere/ghost.py badly")])
        assert seen[0].confidence < 1.0
        assert any("ghost.py" in w for w in seen[0].warnings)

    def test_uncertainty_in_a_finding_is_penalised(self):
        seen, _ = self._run(
            [self._file("app/a.py", "I'm not sure, but this might leak the handle")]
        )
        assert seen[0].confidence < 1.0

    @pytest.mark.parametrize(
        "issue",
        [
            "The TODO on line 12 means this branch never runs.",
            "XXX marker left in the retry loop; the fallback is unimplemented.",
            "Hardcoded key: replace the [your api key] placeholder with an env lookup.",
        ],
    )
    def test_a_finding_that_quotes_the_code_is_not_penalised(self, issue):
        """Scanning nested findings would otherwise penalise exactly the
        findings that caught an unfinished branch or an exposed secret."""
        seen, _ = self._run([self._file("app/a.py", issue)])
        assert seen[0].confidence == 1.0, seen[0].warnings

    def test_the_exemption_is_only_for_code_review(self):
        from app.ai.hallucination import check_response

        r = check_response({"fix": "set it to [your api key] here please"}, response_type="fix")
        assert r.confidence < 1.0


class TestASecurityReportNeverCallsAnUnreadSourceClear:
    """
    The fetchers classified failures by `"403" in str(e)`, and the GitHub
    client says "Forbidden: ..." / "Not found: ..." with the status on
    `e.status_code`. A missing permission, a disabled feature, an outage and a
    timeout all left `errors` empty, and /secfull answered "All Clear" for a
    repository it had not read. Found by the end-to-end suite, not by these
    tests' predecessors, which raised Exception("403 Forbidden") — a message
    the real client never produces.
    """

    @staticmethod
    def _scan(side_effect):
        from app.security import scanner as S

        with patch("app.github.client.gh_get", side_effect=side_effect):
            return S.run_security_scan("o/r", "t")

    @pytest.mark.parametrize(
        "status,message",
        [
            (403, "Forbidden: Resource not accessible by integration"),
            (404, "Not found: /repos/o/r/dependabot/alerts"),
            (502, "GitHub server error 502: /repos/o/r/x"),
            (0, "ReadTimeout: slow"),
        ],
    )
    def test_an_unreadable_repo_is_not_scanned_not_clear(self, status, message):
        from app.github.client import GitHubError

        rep = self._scan(GitHubError(message, status))
        md = rep.to_markdown()
        assert "All Clear" not in md
        assert "Not Scanned" in md
        assert rep.scanned_nothing
        assert rep.unavailable == ["Dependabot", "CodeQL", "Secret Scanning"]

    def test_a_non_list_response_is_a_failure_not_a_clean_result(self):
        rep = self._scan(lambda path, tok: {"message": "unexpected shape"})
        assert rep.scanned_nothing
        assert "All Clear" not in rep.to_markdown()

    def test_one_unreadable_source_is_a_dash_not_a_zero(self):
        from app.github.client import GitHubError

        def gh(path, tok):
            if "dependabot" in path:
                raise GitHubError("Forbidden: no", 403)
            return []

        md = self._scan(gh).to_markdown()
        assert "| Dependabot | — | — | — | — |" in md
        assert "| CodeQL | 0 | 0 | 0 | 0 |" in md
        assert "All Clear" not in md

    def test_a_genuinely_clean_repo_is_still_all_clear(self):
        rep = self._scan(lambda path, tok: [])
        assert "All Clear" in rep.to_markdown()
        assert not rep.unavailable

    def test_the_sweep_does_not_count_an_unread_repo_as_scanned(self):
        """maintenance.scan_repo set ok=True after any scan that returned —
        and the scan never raises — so a repo nothing could be read from was
        counted as scanned with 0 findings."""
        from app.core import maintenance
        from app.github.client import GitHubError

        with (
            patch("app.github.auth.get_installation_token", return_value="t"),
            patch("app.github.client.gh_get", side_effect=GitHubError("Forbidden: no", 403)),
        ):
            record = maintenance.scan_repo("o/r", 1)
        assert record["ok"] is False
        assert "not enabled or no permission" in record["error"]

    def test_the_sweep_still_counts_a_readable_repo(self):
        from app.core import maintenance

        with (
            patch("app.github.auth.get_installation_token", return_value="t"),
            patch("app.github.client.gh_get", return_value=[]),
        ):
            record = maintenance.scan_repo("o/r", 1)
        assert record["ok"] is True
        assert record["error"] == ""


class TestFieldCompletenessIsGraded:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("", 0.0),
            ("abc", 0.3),
            ("x" * 9, 0.9),
            ("x" * 10, 1.0),
            ("x" * 400, 1.0),
            ("   ab   ", 0.2),
        ],
    )
    def test_grades_by_stripped_length(self, value, expected):
        from app.core.confidence import _field_completeness

        assert _field_completeness(value) == pytest.approx(expected)

    @pytest.mark.parametrize("value", [None, 42, ["a" * 20], {"a": "b" * 20}])
    def test_a_non_string_is_no_answer(self, value):
        from app.core.confidence import _field_completeness

        assert _field_completeness(value) == 0.0


class TestGapModelSeesWhatTheTestsReference:
    """
    The gap model is shown the first 600 characters of at most four test
    files. On this repository's own PR #108 the production bot reported four
    "untested" symbols, and every one had a direct test 800 to 1,300 lines into
    a single test file: the model saw imports and a docstring. The complete
    test diffs are now checked for the changed symbols by name, and the excerpts
    say when they were cut.
    """

    SOURCE = (
        "@@ -1,3 +1,9 @@ def scan_repo(repo, installation_id):\n"
        " x = 1\n"
        "+_SHA_REF = re.compile('x')\n"
        "+def as_text(value, default=''):\n"
        "+    return value\n"
        "+class Report:\n"
        "+    pass\n"
        "+def _untested_helper():\n"
        "+    return 2\n"
    )

    def test_changed_symbols_come_from_defs_constants_and_hunk_context(self):
        from app.handlers.pull_request.gaps import changed_symbols

        assert changed_symbols(self.SOURCE) == [
            "scan_repo",
            "_SHA_REF",
            "as_text",
            "Report",
            "_untested_helper",
        ]

    def test_short_names_locals_and_dunders_are_ignored(self):
        from app.handlers.pull_request.gaps import changed_symbols

        patch_ = "@@ -1 +1,4 @@\n+q = 1\n+def __init__(self):\n+    local_value = 3\n+ab = 2\n"
        assert changed_symbols(patch_) == []

    def test_a_reference_past_the_excerpt_is_still_found(self):
        from app.handlers.pull_request.gaps import referenced_by_tests

        test_patch = (
            "@@ -0,0 +1,900 @@\n" + "+# filler\n" * 800 + "+    assert as_text(None) == ''\n"
        )
        assert len(test_patch) > 600
        assert referenced_by_tests(["as_text", "_untested_helper"], [test_patch]) == ["as_text"]

    def test_a_deleted_test_is_not_a_reference(self):
        from app.handlers.pull_request.gaps import referenced_by_tests

        assert referenced_by_tests(["as_text"], ["@@ -1 +0,0 @@\n-    as_text(None)\n"]) == []

    def test_a_substring_is_not_a_reference(self):
        from app.handlers.pull_request.gaps import referenced_by_tests

        assert referenced_by_tests(["as_text"], ["+    has_textual_form()\n"]) == []

    def _prompt(self, source_patch, test_patch):
        sent = []
        files = [
            {"filename": "app/core/x.py", "patch": source_patch, "status": "modified"},
            {"filename": "tests/test_x.py", "patch": test_patch, "status": "modified"},
        ]
        with patch.object(
            gaps_mod.router,
            "ask",
            side_effect=lambda s, u, **k: (sent.append(u), ({"has_gaps": False}, None))[1],
        ):
            gaps_mod._detect_test_gaps({}, "o/r", 1, files, "t", _cfg(), MagicMock())
        return sent[0]

    def test_the_prompt_names_what_the_full_tests_reference(self):
        test_patch = (
            "@@ -0,0 +1,900 @@\n" + "+# filler\n" * 800 + "+    assert as_text(None) == ''\n"
        )
        prompt = self._prompt(self.SOURCE, test_patch)
        note = prompt.split("reference by name", 1)[1].split("\n\n", 1)[0]
        assert "as_text" in note
        assert "_untested_helper" not in note, "claimed a reference that does not exist"
        assert "not proof every branch is covered" in note

    def test_a_cut_excerpt_says_so(self):
        test_patch = "@@ -0,0 +1,900 @@\n" + "+# filler\n" * 800
        prompt = self._prompt(self.SOURCE, test_patch)
        assert f"(first 600 of {len(test_patch):,} characters shown)" in prompt
        flat = " ".join(prompt.split())  # the prompt wraps its lines
        assert "Never conclude a symbol is untested because its test is not in an excerpt" in flat

    def test_a_whole_excerpt_carries_no_cut_marker(self):
        prompt = self._prompt(self.SOURCE, "@@ -0,0 +1 @@\n+    as_text(None)\n")
        assert "characters shown)" not in prompt.split("Tests changed in this PR", 1)[1]

    def test_the_filename_line_stays_first_and_alone(self):
        """The eval stub reads `^### (\\S+)$` to learn which file a case changed."""
        import re as _re

        prompt = self._prompt(self.SOURCE, "@@ -0,0 +1,900 @@\n" + "+# filler\n" * 800)
        assert _re.search(r"^### app/core/x.py$", prompt, _re.M)
        assert _re.search(r"^### tests/test_x.py$", prompt, _re.M)


class TestTheFooterIsWrappedOnce:
    """
    Config.footer wraps its text as "\\n\\n---\\n*{text}*", and this repository's
    example .ai-repo-manager.yml — "Place this file in your repo root" — set the
    footer to an already-wrapped value. Every repo that copied it ended its
    comments with "---\\n*\\n\\n---\\n*🤖 ...**", as this repository's own PR
    reports did.
    """

    @staticmethod
    def _footer(value):
        from app.core.config import Config

        cfg = Config.__new__(Config)
        cfg.get = lambda *keys, default=None: value
        return Config.footer.fget(cfg)

    @pytest.mark.parametrize(
        "configured",
        [
            "🤖 GitHub Autopilot — AI-powered repo management",
            "\n\n---\n*🤖 GitHub Autopilot — AI-powered repo management*",
            "*🤖 GitHub Autopilot — AI-powered repo management*",
            "---\n🤖 GitHub Autopilot — AI-powered repo management",
        ],
    )
    def test_every_form_renders_the_same_footer(self, configured):
        assert (
            self._footer(configured)
            == "\n\n---\n*🤖 GitHub Autopilot — AI-powered repo management*"
        )

    def test_bold_is_left_alone(self):
        assert self._footer("**Bot**") == "\n\n---\n***Bot***"

    @pytest.mark.parametrize("configured", ["", "   ", "---", None])
    def test_an_empty_footer_renders_nothing(self, configured):
        assert self._footer(configured) == ""

    def test_the_example_config_is_plain_text(self):
        import yaml

        from pathlib import Path

        example = yaml.safe_load(
            (Path(__file__).resolve().parent.parent / ".ai-repo-manager.yml").read_text()
        )
        assert not example["bot"]["footer"].lstrip().startswith(("---", "*", "\n"))


class TestEveryFileInAPullRequest:
    """
    Five call sites fetched /pulls/{n}/files once, and GitHub's default page is
    30 files. The production bot reported this repository's own 32-file PR #108
    as "Files: 30 · +2101 −235" — the real figure was +3,618 −249 — and never
    reviewed, gap-checked or secret-scanned the files past the thirtieth.
    """

    @staticmethod
    def _pages(total):
        files = [{"filename": f"f{i}.py", "additions": 1, "deletions": 0} for i in range(total)]
        calls = []

        def get(path, token):
            calls.append(path)
            page = int(path.rsplit("page=", 1)[1])
            return files[(page - 1) * 100 : page * 100]

        return files, calls, get

    def test_every_page_is_fetched(self):
        from app.github.helpers import pr_files

        files, calls, get = self._pages(250)
        assert pr_files("o/r", 1, "t", get=get) == files
        assert len(calls) == 3
        assert all("per_page=100" in c for c in calls)

    def test_a_short_page_is_the_last(self):
        from app.github.helpers import pr_files

        _files, calls, get = self._pages(32)
        assert len(pr_files("o/r", 1, "t", get=get)) == 32
        assert len(calls) == 1

    def test_an_exact_multiple_stops_on_the_empty_page(self):
        from app.github.helpers import pr_files

        _files, calls, get = self._pages(200)
        assert len(pr_files("o/r", 1, "t", get=get)) == 200
        assert len(calls) == 3

    def test_a_first_page_failure_raises_as_before(self):
        from app.github.helpers import pr_files

        def get(path, token):
            raise RuntimeError("down")

        with pytest.raises(RuntimeError):
            pr_files("o/r", 1, "t", get=get)

    def test_a_later_page_failure_keeps_what_was_fetched(self):
        from app.github.helpers import pr_files

        files, _calls, good = self._pages(250)

        def get(path, token):
            if path.endswith("page=2"):
                raise RuntimeError("blip")
            return good(path, token)

        assert pr_files("o/r", 1, "t", get=get) == files[:100]

    def test_it_stops_at_githubs_cap(self):
        from app.github.helpers import PR_FILES_MAX_PAGES, pr_files

        calls = []

        def get(path, token):
            calls.append(path)
            return [{"filename": "x"}] * 100

        assert len(pr_files("o/r", 1, "t", get=get)) == 3000
        assert len(calls) == PR_FILES_MAX_PAGES

    def test_the_pr_handler_sees_every_file(self):
        """handle() is where the report's file count and totals come from."""
        from app.handlers import pull_request as P

        files, _calls, paged = self._pages(35)

        def gh(path, token):
            return paged(path, token) if "/files" in path else {"archived": False}

        payload = {
            "action": "opened",
            "pull_request": {
                "number": 1,
                "title": "t",
                "user": {"login": "a"},
                "head": {"sha": "s"},
            },
            "repository": {"full_name": "o/r"},
            "installation": {"id": 1},
        }
        cfg = MagicMock()
        cfg.pr_enabled.return_value = True
        cfg.footer = ""
        cfg.get.side_effect = lambda *a, default=None: default
        seen = {}
        with (
            patch.object(P, "get_installation_token", return_value="t"),
            patch.object(P, "load_config", return_value=cfg),
            patch.object(P, "gh_get", side_effect=gh),
            patch.object(P, "_analyze_pr", return_value=""),
            patch.object(P, "_build_pr_summary", return_value=""),
            patch.object(P, "_detect_test_gaps", return_value=""),
            patch.object(
                P,
                "_review_code",
                side_effect=lambda pr, repo, n, f, *a: (seen.setdefault("n", len(f)), ("", []))[1],
            ),
            patch("app.core.guardrails.check_repo_rate_limit", return_value=MagicMock(passed=True)),
            patch("app.core.guardrails.increment_repo_usage"),
        ):
            P.handle(payload)
        assert seen["n"] == 35, f"the review saw {seen.get('n')} of 35 files"
