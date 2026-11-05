"""
tests/test_review_findings_shape.py

What reaches the PR from the model's findings, driven through _review_code:
a finding about unchanged code is not pinned to it, a repeated finding is
posted once, and the model's own severity words keep their rank.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.handlers import pull_request as pr_mod

PATCH = (
    "@@ -10,3 +10,4 @@ def load():\n"
    " cfg = read()\n"
    "+if cfg is None:\n"
    '+    raise ConfigError("missing")\n'
    " ok()"
)


def _review(issues):
    cfg = MagicMock()
    cfg.footer = ""
    cfg.get.return_value = 4
    llm = ({"files": [{"file": "app/cfg.py", "issues": issues, "summary": "s"}],
            "confidence": 0.9}, MagicMock())
    files = [{"filename": "app/cfg.py", "patch": PATCH, "status": "modified",
              "additions": 2, "deletions": 0}]
    with patch.object(pr_mod.router, "ask", return_value=llm), \
         patch("app.handlers.pull_request.review._posted_findings", return_value=set()), \
         patch("app.handlers.pull_request.review.fetch_file", return_value=""):
        return pr_mod._review_code({"head": {"sha": "abc"}}, "o/r", 1, files, "t", cfg,
                                   MagicMock(), "", MagicMock())


def test_a_finding_on_unchanged_code_is_shown_but_not_anchored():
    md, inline = _review([{"severity": "major", "line": "10", "code": "cfg = read()",
                           "issue": "read() can block", "fix": "add a timeout"}])
    assert inline == [], "pinned an inline comment to a line this PR did not change"
    assert "about unchanged code" in md and "read() can block" in md


def test_an_invented_quote_around_a_real_line_is_withheld():
    md, inline = _review([{
        "severity": "critical", "line": "11",
        "code": 'if cfg is None: cursor.execute(f"SELECT * FROM t WHERE id={x}")',
        "issue": "SQL injection", "fix": "parametrise"}])
    assert inline == []
    assert "withheld" in md


def test_a_repeated_finding_is_posted_once():
    same = {"severity": "major", "line": "11", "code": "if cfg is None:",
            "issue": "None  check   is late", "fix": "check earlier"}
    _md, inline = _review([same, {**same, "issue": "none check is LATE"}])
    assert len(inline) == 1


def test_high_is_major_not_minor():
    from app.ai.validator import validate_code_review

    r = validate_code_review({"issues": [
        {"severity": "low", "issue": "a"}, {"severity": "HIGH", "issue": "b"},
        {"severity": "blocker", "issue": "c"}, {"severity": "weird", "issue": "d"}]})
    assert [(i["issue"], i["severity"]) for i in r["issues"]] == [
        ("c", "critical"), ("b", "major"), ("d", "minor"), ("a", "nit")]
