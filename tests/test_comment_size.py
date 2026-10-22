"""
tests/test_comment_size.py

GitHub refuses a comment or issue body over 65,536 characters with a 422.
Nothing checked: /secfull on a repository at the scanner's own page limits
rendered ~69,000 characters, the POST was refused, and the person who asked
got no reply at all — on the repository with the most findings.
"""

from __future__ import annotations

from unittest.mock import patch

from app.github.helpers import GITHUB_BODY_LIMIT, fit_comment


def _long(n_lines=1000):
    return "\n".join(f"- finding {i} " + "x" * 80 for i in range(n_lines))


class TestFitComment:
    def test_a_short_body_is_untouched(self):
        assert fit_comment("hello") == "hello"

    def test_a_long_body_fits_and_says_how_much_is_hidden(self):
        body = _long()
        out = fit_comment(body)
        assert len(out) <= GITHUB_BODY_LIMIT
        assert "Cut to fit GitHub's comment limit" in out
        shown = out.split("\n\n> ✂️")[0]
        hidden = int(out.rsplit("— ", 1)[1].split(" characters")[0].replace(",", ""))
        assert hidden == len(body) - len(shown)

    def test_it_cuts_at_a_line_boundary(self):
        out = fit_comment(_long())
        last = out.split("\n\n> ✂️")[0].rsplit("\n", 1)[1]
        assert last.endswith("x" * 80), "a finding was cut mid-line"

    def test_a_cut_code_block_is_closed(self):
        body = "```\n" + _long()
        out = fit_comment(body)
        assert out.split("\n\n> ✂️")[0].count("```") % 2 == 0

    def test_reserve_leaves_room(self):
        assert len(fit_comment(_long(), reserve=100)) <= GITHUB_BODY_LIMIT - 100


class TestEveryWayOut:
    def test_a_command_reply_is_posted_within_the_limit(self):
        from app.github.deferred_reply import post_comment

        sent = {}
        post_comment("o/r", 1, "t", 1, _long(), post=lambda p, t, b: sent.update(b))
        assert len(sent["body"]) <= GITHUB_BODY_LIMIT

    def test_a_sticky_keeps_its_marker_when_cut(self):
        """A report cut past its marker could not be found again, and every
        update would post a new one."""
        from app.github import sticky

        marker = "<!-- autopilot:pr-report -->"
        with patch.object(sticky, "find_sticky", return_value=None), \
             patch.object(sticky, "gh_post", return_value={}) as post:
            sticky.upsert_sticky("o/r", 1, "t", marker, _long() + "\n\n" + marker)
        body = post.call_args[0][2]["body"]
        assert len(body) <= GITHUB_BODY_LIMIT and body.endswith(marker)

    def test_secfull_at_the_scanners_page_limits(self):
        from app.security.scanner import SecurityFinding, SecurityReport

        rep = SecurityReport(repo="o/r")
        rep.dependabot = [SecurityFinding(
            source="dependabot", severity="high", title="Prototype pollution in lodash.merge",
            description="", package=f"pkg{i}", cve_id=f"CVE-2024-{1000 + i}",
            url=f"https://github.com/o/r/security/dependabot/{i}") for i in range(100)]
        rep.codeql = [SecurityFinding(
            source="codeql", severity="medium", title="Uncontrolled data used in path expression reaches a filesystem call",
            description="", file_path=f"src/module_{i}/handlers/file_{i}.py", line_number=i)
            for i in range(500)]
        md = rep.to_markdown(include_low=True)
        assert len(md) > GITHUB_BODY_LIMIT, "the fixture must reproduce the oversize report"
        assert len(fit_comment(md)) <= GITHUB_BODY_LIMIT
