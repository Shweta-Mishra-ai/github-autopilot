"""
tests/test_code_answers_grounded.py

/fix and the other code commands on an ISSUE are shown its title and body
only. Asked for a root cause, the model named `app/auth/jwt_utils.py:
verify_token()` at "line 88" for a repository it had never seen, and the
reply read as a diagnosis. Nothing checked the paths it named.
"""

from __future__ import annotations

from unittest.mock import patch

from app.handlers.comments import dispatcher as D
from app.handlers.comments import generator as G

ISSUE_CTX = "Title: Users logged out randomly\nBody: happens daily"
PR_CTX = f"Title: t\nBody: b\n\nChanged files (1):\n- app/new_mod.py (+3 -0)\n\n{D.DIFF_MARKER}\n+x = 1"


class TestThePromptSaysWhenNoCodeWasShown:
    def test_an_issue_gets_the_rule_outside_the_untrusted_block(self):
        out = G._ctx(ISSUE_CTX)
        rule_at = out.index("You have NOT been shown this repository's code")
        assert rule_at > out.index("</ISSUE_CONTEXT>"), "inside the block it is data, not a rule"

    def test_a_pr_with_its_diff_does_not(self):
        assert "NOT been shown" not in G._ctx(PR_CTX)


class TestTheReplySaysWhatItWasBasedOn:
    TREE = {"files": ["app/auth/session.py", "app/models.py"]}

    def _ground(self, answer, ctx, tree=None):
        with patch("app.github.helpers.repo_file_context", return_value=self.TREE if tree is None else tree):
            return D.ground_answer("/fix", answer, ctx, "o/r", "t")

    def test_an_issue_answer_is_labelled(self):
        out = self._ground("Check the session refresh.", ISSUE_CTX)
        assert "Based on the issue's text only" in out

    def test_files_that_do_not_exist_are_named(self):
        out = self._ground("Root cause: app/auth/jwt_utils.py line 88; see models.py", ISSUE_CTX)
        assert "Not in this repository: `app/auth/jwt_utils.py`" in out
        assert "`models.py`" not in out.split("Not in this repository:")[1], "a basename of a real file"

    def test_a_file_the_pr_adds_is_not_flagged(self):
        out = self._ground("The bug is in app/new_mod.py.", PR_CTX)
        assert "Not in this repository" not in out
        assert "Based on the issue" not in out

    def test_an_unreadable_tree_claims_nothing(self):
        out = self._ground("See app/auth/jwt_utils.py", PR_CTX, tree={})
        assert out == "See app/auth/jwt_utils.py"

    def test_other_commands_and_degraded_replies_are_untouched(self):
        with patch("app.github.helpers.repo_file_context", return_value=self.TREE):
            assert D.ground_answer("/merge", "merged x.py", ISSUE_CTX, "o/r", "t") == "merged x.py"
            assert D.ground_answer("/fix", {"_providers_down": True}, ISSUE_CTX, "o/r", "t") == {
                "_providers_down": True}


def test_the_whole_reply_through_the_service():
    from app.handlers.comments.service import handle_comment_event

    payload = {"action": "created", "comment": {"body": "/fix", "id": 1},
               "issue": {"number": 3, "title": "Users logged out randomly", "body": "daily"},
               "repository": {"full_name": "o/r"}, "installation": {"id": 9},
               "sender": {"login": "dev"}}
    posted = []
    from app.core.config import Config

    with patch("app.handlers.comments.service.get_installation_token", return_value="t"), \
         patch("app.handlers.comments.service.load_config", return_value=Config({})), \
         patch("app.handlers.comments.service.check_command_permission", return_value=(True, "")), \
         patch("app.handlers.comments.service._dispatch",
               return_value="**Root cause:** app/auth/jwt_utils.py:verify_token()"), \
         patch("app.github.helpers.repo_file_context", return_value={"files": ["app/main.py"]}), \
         patch("app.handlers.comments.gh_post", side_effect=lambda p, t, b: posted.append(b["body"])):
        handle_comment_event(payload)
    assert "Based on the issue's text only" in posted[0]
    assert "Not in this repository: `app/auth/jwt_utils.py`" in posted[0]
