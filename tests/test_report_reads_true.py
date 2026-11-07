"""
tests/test_report_reads_true.py — the PR report says only what the bot saw,
and says it in whole words.

Both from the report the bot posted on PR #114 (244 files):

- The summary said `gatekeeper.py` and four other `app/ai/` files "block
  unsafe writes and detect secret leaks". The model had been shown the diff of
  a handful of files; `gatekeeper.py` changed two lines and does neither. The
  sentence named a file the PR does touch, so the grounding filter — which
  only rejected files outside the PR — let it through.
- "CI workflow adjustmen" and "Retry‑Af": the risk reason was cut at 300
  characters and the suggested test at 80, mid-word.
"""

from unittest.mock import MagicMock, patch

from app.handlers.pull_request import analysis


def _file(name: str, lines: int = 400) -> dict:
    body = "\n".join(f"+line {i} of {name}" for i in range(lines))
    return {"filename": name, "patch": f"@@ -0,0 +1,{lines} @@\n{body}", "additions": lines}


def _big_pr() -> list[dict]:
    names = [f"app/handlers/mod_{i:03d}.py" for i in range(60)] + ["app/ai/gatekeeper.py"]
    return [_file(n) for n in names]


def _summary(model_text: str, files: list[dict]) -> str:
    pr = {"title": "fix: things", "body": "", "user": {"login": "u"}, "base": {"ref": "main"}}
    with patch.object(analysis.router, "ask_text", return_value=(model_text, MagicMock())):
        return analysis._build_pr_summary(pr, "o/r", 1, files, "t", MagicMock(), MagicMock())


class TestTheSummaryDescribesOnlyWhatItSaw:
    def test_a_claim_about_a_file_whose_diff_was_not_shown_is_dropped(self):
        files = _big_pr()
        shown = analysis.shown_files(analysis._diff_excerpt(files, analysis.SUMMARY_DIFF_CHARS))
        unseen = sorted({f["filename"] for f in files} - shown)
        assert shown and unseen, "the case needs both a seen and an unseen file"
        seen = sorted(shown)[0]
        out = _summary(
            f"This PR reworks `{seen}`. "
            f"Added safeguards in {unseen[0]} to block unsafe writes and detect secret leaks.",
            files,
        )
        assert unseen[0] not in out and "secret leaks" not in out, out
        assert seen in out

    def test_a_partial_view_is_disclosed(self):
        files = _big_pr()
        seen = sorted(analysis.shown_files(analysis._diff_excerpt(files, analysis.SUMMARY_DIFF_CHARS)))[0]
        out = _summary(f"This PR reworks `{seen}`.", files)
        assert f"of {len(files)} changed files" in out, out

    def test_a_small_pr_is_not_given_a_coverage_note(self):
        files = [_file("app/x.py", 5)]
        out = _summary("This PR reworks `app/x.py`.", files)
        assert "changed files" not in out, out

    def test_the_model_is_given_the_whole_file_list_and_told_what_it_saw(self):
        files = _big_pr()
        seen_prompt = {}

        def ask(system, prompt, **kw):
            seen_prompt["p"] = prompt
            return "This PR changes code.", MagicMock()

        pr = {"title": "t", "body": "", "user": {"login": "u"}, "base": {"ref": "main"}}
        with patch.object(analysis.router, "ask_text", side_effect=ask):
            try:
                analysis._build_pr_summary(pr, "o/r", 1, files, "t", MagicMock(), MagicMock())
            except Exception:
                pass
        p = seen_prompt["p"]
        assert "app/ai/gatekeeper.py" in p, "the file list stopped at ten names"
        assert "You were shown the diff of" in p


class TestTextIsCutAtAWord:
    def test_clip_never_ends_mid_word(self):
        from app.ai.validator import clip

        text = "Additionally, CI workflow adjustments could break the release job " * 10
        out = clip(text, 300)
        assert len(out) <= 300
        assert out.endswith("…")
        assert out[:-1].rstrip().split()[-1] in text.split()

    def test_clip_leaves_short_text_alone(self):
        from app.ai.validator import clip

        assert clip("short reason", 300) == "short reason"

    def test_the_risk_reason_is_clipped_at_a_word(self):
        from app.ai.validator import validate_pr_analysis

        reason = "While the changes aim to close secret-leak paths, CI workflow adjustments " * 8
        r = validate_pr_analysis(
            {"risk_level": "medium", "risk_reason": reason, "suggested_title": "t", "confidence": 0.9}
        )
        assert r["risk_reason"].endswith("…")
        assert "adjustmen…" not in r["risk_reason"]

    def test_the_suggested_test_cell_is_not_cut_mid_word(self):
        from app.handlers.pull_request.gaps import gap_row

        long = (
            "Create a test that simulates a provider returning a 429 response with a "
            "Retry-After header of two hours | and checks the breaker stays open that long"
        )
        row = gap_row({"file": "app/x.py", "function": "f", "risk": "HIGH", "suggested_test": long})
        cells = row.strip("|").split(" | ")
        assert len(cells) == 4, f"a | in model text split the row: {row}"
        assert "Retry-After" in row and "`high`" in row


class TestTheRiskIsReadFirst:
    ANALYSIS = (
        "🟡 **Risk Level:** `MEDIUM`\n**Reason:** touches the circuit breaker and PyJWT."
        "\n\n### Review Focus\n- Security\n\n### Suggested Title\n```\nfix: x\n```"
    )
    GAPS = (
        "### Gaps Found\n\n| File | Function | Risk | Suggested Test |\n|---|---|---|---|\n"
        "| `a.py` | `f` | `high` | test f |\n| `b.py` | `g` | `low` | test g |\n"
    )

    def _report(self, analysis_md="", gaps_md=""):
        from app.handlers.pull_request.report import _build_pr_report

        return _build_pr_report(analysis_md, "Summary.", "", gaps_md, {"number": 1}, [])

    def test_the_risk_line_is_outside_the_fold(self):
        body = self._report(self.ANALYSIS)
        fold = body.index("<details>")
        assert body.index("**Risk Level:**") < fold
        assert body.index("**Reason:**") < fold
        assert body.index("### Review Focus") > fold, "the rest stays folded"

    def test_the_gap_section_says_how_many(self):
        assert "🧪 Test coverage — 2 gaps" in self._report(gaps_md=self.GAPS)

    def test_an_analysis_without_a_risk_line_stays_folded(self):
        body = self._report("### Review Focus\n- x")
        assert "<summary>📋 Analysis</summary>" in body and "### Review Focus" in body


class TestTheSummaryKeepsItsShape:
    """PR #119: the summary's bullet list was published as one paragraph."""

    def test_lines_and_list_markers_survive_the_filter(self):
        from app.handlers.pull_request.analysis import grounded_sentences

        files = [_file("evals/run.py", 3), _file("tests/test_evals_harness.py", 3)]
        text = (
            "**Key technical changes**\n"
            "- In `evals/run.py`, outages are no longer crashes.\n"
            "- Updated app/ghost.py to cache tokens.\n"
            "- Added tests in tests/test_evals_harness.py."
        )
        out = grounded_sentences(text, files)
        assert out.splitlines() == [
            "**Key technical changes**",
            "- In `evals/run.py`, outages are no longer crashes.",
            "- Added tests in tests/test_evals_harness.py.",
        ], out


class TestACutDiffSaysSo:
    """PR #119: the analysis was shown each file's diff cut mid-line with no
    mark, and rated the PR HIGH for an `if` "with only comments" and a function
    that "lacks a return" — both past the cut."""

    def _files(self):
        lines = "\n".join(f"+    value_{i} = compute(argument_{i})" for i in range(200))
        return [
            {"filename": "a.py", "patch": f"@@ -0,0 +1,200 @@\n{lines}"},
            {"filename": "b.py", "patch": "@@ -0,0 +1,1 @@\n+x = 1"},
        ]

    def test_a_cut_file_ends_on_a_whole_line_with_the_mark(self):
        excerpt, _shown, _total = analysis.diff_excerpt_coverage(self._files(), 1500)
        a = excerpt.split("### b.py")[0] if excerpt.index("### a.py") < excerpt.index("### b.py") else excerpt.split("### a.py")[1]
        body = a.strip().splitlines()
        assert body[-1] == analysis.DIFF_CUT_MARK
        assert body[-2].startswith("+    value_") and body[-2].endswith(")"), body[-2]

    def test_the_excerpt_stays_within_its_budget(self):
        excerpt, _s, _t = analysis.diff_excerpt_coverage(self._files(), 1500)
        assert len(excerpt) <= 1500

    def test_an_uncut_file_has_no_mark(self):
        excerpt, _s, _t = analysis.diff_excerpt_coverage(self._files()[1:], 1500)
        assert analysis.DIFF_CUT_MARK not in excerpt

    def test_both_prompts_explain_the_mark(self):
        prompts = []

        def capture(*args, **kwargs):
            prompts.append(args[1])
            raise RuntimeError("stop after the prompt")

        pr = {"title": "t", "body": "", "user": {"login": "u"}, "base": {"ref": "main"}, "head": {}}
        with patch.object(analysis.router, "ask", side_effect=capture), patch.object(
            analysis.router, "ask_text", side_effect=capture
        ):
            for call in (
                lambda: analysis._analyze_pr(pr, "o/r", 1, self._files(), "t", MagicMock(), MagicMock(), "", MagicMock()),
                lambda: analysis._build_pr_summary(pr, "o/r", 1, self._files(), "t", MagicMock(), MagicMock()),
            ):
                try:
                    call()
                except RuntimeError:
                    pass
        assert len(prompts) == 2
        assert all("the code goes on" in p for p in prompts)


class TestAnOlderPushDoesNotOverwriteTheReport:
    """PR #119: the 5-minute retry of the first commit's event rewrote the
    report three pushes later — the first commit's summary under the newest
    file count."""

    FILES = [{"filename": "app/x.py", "status": "modified", "additions": 1, "deletions": 0,
              "patch": "@@ -1 +1,2 @@\n x = 1\n+y = 2\n"}]

    def _handle(self, current_head):
        from app.core.config import Config
        from app.handlers import pull_request as prh

        written, asked = [], []

        def gh_get(path, token, *a, **k):
            if path.endswith("/pulls/7"):
                if isinstance(current_head, Exception):
                    raise current_head
                return {"head": {"sha": current_head}}
            return {"archived": False}

        def ask(*a, **k):
            asked.append(1)
            return {"files": [], "confidence": 0.9}, MagicMock(total_tokens=0)

        payload = {
            "action": "synchronize", "repository": {"full_name": "o/r"}, "installation": {"id": 1},
            "pull_request": {"number": 7, "title": "t", "body": "", "user": {"login": "u"},
                             "head": {"sha": "old111"}, "base": {"ref": "main"}},
        }
        with patch.object(prh, "get_installation_token", return_value="t"), \
             patch.object(prh, "load_config", return_value=Config({})), \
             patch.object(prh, "gh_get", side_effect=gh_get), \
             patch("app.github.helpers.pr_files", return_value=self.FILES), \
             patch("app.ai.gatekeeper.is_substantive", return_value=(True, "")), \
             patch("app.ai.router.router.ask", side_effect=ask), \
             patch("app.ai.router.router.ask_text", side_effect=lambda *a, **k: ("Adds y.", MagicMock())), \
             patch.object(prh, "update_sticky_if_present", side_effect=lambda *a: written.append(a) or {"id": 1}), \
             patch.object(prh, "upsert_sticky", side_effect=lambda *a: written.append(a) or {"id": 1}):
            try:
                prh.handle(payload)
            except Exception:
                pass
        return written, asked

    def test_a_superseded_event_spends_nothing_and_writes_nothing(self):
        written, asked = self._handle(current_head="new222")
        assert written == [] and asked == []

    def test_the_current_head_is_reported(self):
        written, asked = self._handle(current_head="old111")
        assert written and asked

    def test_a_failed_lookup_does_not_stop_the_report(self):
        written, _ = self._handle(current_head=RuntimeError("502"))
        assert written, "the check must fail open"


class TestAQuoteCopiedWithTheBarStillGrounds:
    """The review now prints `NUMBER MARKER | CODE`; a model that copies the
    prefix along with the code must still be matched to the line."""

    PATCH = "@@ -1,2 +1,3 @@\n def f(case_id):\n+    reason = lookup(case_id)\n     return reason\n"

    def test_added_line_quoted_with_its_prefix(self):
        from app.handlers.pull_request.grounding import GROUNDED, ground_finding

        issue = {"line": 2, "code": "     2 + |     reason = lookup(case_id)"}
        assert ground_finding(issue, self.PATCH) == (GROUNDED, 2)

    def test_unchanged_line_quoted_with_its_prefix_is_context(self):
        from app.handlers.pull_request.grounding import CONTEXT, ground_finding

        issue = {"line": 1, "code": "     1   | def f(case_id):"}
        assert ground_finding(issue, self.PATCH)[0] == CONTEXT


class TestTheReviewHasRoomToAnswer:
    """Live eval runs logged the review's JSON cut off at the router's default
    1500 tokens; a four-file review now asks for twice that."""

    def test_the_review_call_asks_for_its_own_limit(self):
        from app.core.confidence import ConfidenceGate
        from app.handlers.pull_request import review

        seen = {}

        def ask(system, user, **kw):
            seen.update(kw)
            raise RuntimeError("stop")

        files = [_file("app/x.py", 5)]
        cfg = MagicMock()
        cfg.get.side_effect = lambda *a, **k: k.get("default")
        with patch.object(review.router, "ask", side_effect=ask), patch.object(
            review, "gh_get", side_effect=RuntimeError("no network")
        ):
            try:
                review._review_code({"head": {"sha": "s"}}, "o/r", 1, files, "t", cfg, ConfidenceGate(None), "", MagicMock())
            except RuntimeError:
                pass
        assert seen.get("max_tokens") == review.REVIEW_MAX_TOKENS > 1500
