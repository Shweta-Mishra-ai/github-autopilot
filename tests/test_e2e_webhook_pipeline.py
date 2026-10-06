"""
End-to-end: signed webhook in, GitHub API calls out, through the real server.

gunicorn serves server:app with the Procfile's flags; events go through the
real Redis queue to the in-process consumers; the GitHub App JWT is signed and
exchanged; the LLM router picks the real Ollama provider and makes a real HTTP
call. Only the two remote services are fakes — see tests/e2e_harness.py.

Every unit test in this suite mocks the GitHub client, so none of them can see
the request a handler actually sends. The first run of this file found that the
PR title/description update had been sent as PUT — a method GitHub does not
accept on a pull request — since the day it shipped, and that /rollback's two
undo actions had the same bug.

Marked `integration` and skipped without a reachable REDIS_TEST_URL, the same
contract as tests/test_integration_redis.py, so CI's integration job runs it
and its "fail if nothing actually ran" step covers it.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_harness import (  # noqa: E402
    FakeGitHub,
    FakeOllama,
    Server,
    redis_command,
    redis_reachable,
    wait_for,
)

REDIS_TEST_URL = os.environ.get("REDIS_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not redis_reachable(REDIS_TEST_URL),
        reason="set REDIS_TEST_URL to a reachable Redis to run the end-to-end suite",
    ),
]

INSTALLATION = 42
MARKER = "<!-- github-autopilot:pr-report -->"

# A database of its own, flushed either side, so this suite and
# test_integration_redis.py can share one server without seeing each other.
E2E_DB = 14


def _e2e_redis_url() -> str:
    base = REDIS_TEST_URL.rsplit("/", 1)[0] if REDIS_TEST_URL.count("/") >= 3 else REDIS_TEST_URL
    return f"{base}/{E2E_DB}"


# ── The world ────────────────────────────────────────────────────────────────


class World:
    """One running stack. Each test gets a fresh repo name, so per-repo state in
    the server (rate budget, dedup, token cache) never leaks between tests."""

    def __init__(self):
        self.gh = FakeGitHub()
        self.llm = FakeOllama({})
        gh_url = self.gh.start()
        llm_url = self.llm.start()
        redis_command(_e2e_redis_url(), "FLUSHDB")
        self.server = Server(gh_url, llm_url, _e2e_redis_url())
        self.server.start()
        self.comments: dict = {}
        self._next_id = 1000

    def stop(self):
        self.server.stop()
        self.gh.stop()
        self.llm.stop()
        redis_command(_e2e_redis_url(), "FLUSHDB")

    # A repo with stateful issue comments, so the sticky can be found and edited.
    def repo(self, files, *, tree=None, reviews=(200, {"id": 1}), config_yaml=None):
        name = f"e2e/r{uuid.uuid4().hex[:8]}"
        gh = self.gh
        if config_yaml is not None:
            import base64

            encoded = base64.b64encode(config_yaml.encode()).decode()
            gh.route(
                "GET",
                f"/repos/{name}/contents/.ai-repo-manager.yml",
                lambda r: (200, {"content": encoded, "encoding": "base64"}),
            )
        gh.route(
            "POST",
            f"/app/installations/{INSTALLATION}/access_tokens",
            lambda r: (201, {"token": "ghs_e2e", "permissions": {"pull_requests": "write"}}),
        )
        gh.route("GET", f"/repos/{name}", lambda r: (200, {"full_name": name, "archived": False}))

        def list_files(r):
            # Paginated exactly as GitHub does it: 30 per page unless asked.
            # Serving every file on every request hid a bug that fetched only
            # the first page.
            from urllib.parse import parse_qsl

            q = dict(parse_qsl(r["query"]))
            per, page = int(q.get("per_page", 30)), int(q.get("page", 1))
            return 200, files[(page - 1) * per : page * per]

        gh.route("GET", f"/repos/{name}/pulls/1/files", list_files)
        gh.route("PATCH", f"/repos/{name}/pulls/1", lambda r: (200, {"number": 1}))
        gh.route(
            "POST",
            f"/repos/{name}/pulls/1/reviews",
            (lambda r: reviews) if not callable(reviews) else reviews,
        )
        paths = tree if tree is not None else [f["filename"] for f in files]
        gh.route(
            "GET",
            f"/repos/{name}/git/trees/HEADSHA1",
            lambda r: (
                200,
                {"truncated": False, "tree": [{"path": p, "type": "blob"} for p in paths]},
            ),
        )

        thread = self.comments.setdefault(name, [])

        def list_comments(r):
            return 200, list(thread)

        def post_comment(r):
            self._next_id += 1
            c = {"id": self._next_id, "body": r["body"]["body"]}
            thread.append(c)
            gh.route(
                "PATCH",
                f"/repos/{name}/issues/comments/{c['id']}",
                lambda rr, c=c: (c.update(body=rr["body"]["body"]) or (200, c)),
            )
            return 201, c

        gh.route("GET", f"/repos/{name}/issues/1/comments", list_comments)
        gh.route("POST", f"/repos/{name}/issues/1/comments", post_comment)
        return name

    def open_pr(self, repo, action="opened", title="update query", delivery=None):
        pr = {
            "number": 1,
            "title": title,
            "body": "",
            "user": {"login": "alice"},
            "head": {"sha": "HEADSHA1", "ref": "feat"},
            "base": {"ref": "main"},
        }
        return self.server.send(
            "pull_request",
            {
                "action": action,
                "pull_request": pr,
                "repository": {"full_name": repo},
                "installation": {"id": INSTALLATION},
                "sender": {"login": "alice", "type": "User"},
            },
            delivery=delivery,
        )

    def sticky(self, repo, timeout=30):
        return wait_for(
            lambda: next((c for c in self.comments.get(repo, []) if MARKER in c["body"]), None),
            timeout=timeout,
        )

    def writes(self, repo, method=None):
        return [w for w in self.gh.writes(method) if repo in w["path"]]

    def settle(self, repo, seconds=1.5):
        """Wait until the pipeline has stopped writing for this repo."""
        last, stable_since = -1, time.time()
        deadline = time.time() + 30
        while time.time() < deadline:
            n = len([r for r in self.gh.requests if repo in r["path"]])
            if n != last:
                last, stable_since = n, time.time()
            elif time.time() - stable_since >= seconds:
                return
            time.sleep(0.1)


@pytest.fixture(scope="module")
def world():
    w = World()
    try:
        yield w
    finally:
        w.stop()


@pytest.fixture
def show_log_on_failure(world, request):
    yield
    rep = getattr(request.node, "rep_call", None)
    if rep is not None and rep.failed:
        print("\n── server log (tail) ──\n" + "\n".join(world.server.log().splitlines()[-60:]))


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


# ── Scripted model answers ───────────────────────────────────────────────────

SQL_PATCH = (
    "@@ -1,3 +1,4 @@\n import os\n-q = 'x'\n"
    "+q = 'SELECT * FROM t WHERE id=' + uid\n+cur.execute(q)\n"
)
SQL_FILE = {
    "filename": "app/db.py",
    "status": "modified",
    "additions": 2,
    "deletions": 1,
    "patch": SQL_PATCH,
}


def _review(*issues, file="app/db.py", summary="Builds SQL from request input.", confidence=0.9):
    return {
        "files": [{"file": file, "score": 3, "summary": summary, "issues": list(issues)}],
        "confidence": confidence,
    }


def _issue(line, text, fix, severity="critical"):
    return {"severity": severity, "line": str(line), "issue": text, "fix": fix}


ANALYSIS = {
    "suggested_title": "fix(db): parameterise the lookup query",
    "description": "## Summary\nParameterises the query.\n## Changes\napp/db.py\n## Testing\nUnit.",
    "risk_level": "high",
    "risk_reason": "SQL is built from request input",
    "review_focus": ["app/db.py"],
    "confidence": 0.95,
}
GAPS = {
    "has_gaps": True,
    "coverage_score": "4",  # a string, as models send it — this used to delete the section
    "summary": "Nothing exercises the query builder.",
    "gaps": [
        {"file": "app/db.py", "function": "lookup", "risk": "high", "suggested_test": "bind params"}
    ],
}


def script(review, gaps=GAPS, analysis=ANALYSIS):
    return {
        "triage filter": "SUBSTANTIVE",
        "Senior code reviewer": review,
        "Analyze GitHub PRs": analysis,
        "PR summaries": "Changes how the lookup query is built; review app/db.py.",
        "Senior QA engineer": gaps,
    }


# ── Scenarios ────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("show_log_on_failure")
class TestPullRequestOpened:
    def test_one_report_with_every_section(self, world):
        world.llm.script = script(
            _review(_issue(3, "SQL injection via string concatenation", "cur.execute(q, (uid,))"))
        )
        repo = world.repo([SQL_FILE])
        assert world.open_pr(repo).status_code == 202

        sticky = world.sticky(repo)
        assert sticky, "no report was posted"
        world.settle(repo)
        body = sticky["body"]
        for section in ("Analysis", "Code review", "Test coverage", "Risk Level:** `HIGH`"):
            assert section in body, f"missing {section!r}"
        assert "bind params" in body, "a string coverage_score lost the section"
        assert "Coverage Score" not in body, "the model's own score must not be published"
        assert len([c for c in world.comments[repo] if MARKER in c["body"]]) == 1

    def _metadata_edits(self, world, **repo_kw):
        world.llm.script = script(_review())
        repo = world.repo([SQL_FILE], **repo_kw)
        world.open_pr(repo)
        assert world.sticky(repo)
        world.settle(repo)
        return [w for w in world.writes(repo) if w["path"].endswith("/pulls/1")]

    def test_description_is_written_with_patch(self, world):
        """Was PUT, which GitHub does not accept on a pull request: every
        description update since this shipped was a 404."""
        edits = self._metadata_edits(world)
        assert [w["method"] for w in edits] == ["PATCH"], edits
        assert edits[0]["body"]["body"].startswith("## Summary")
        # Title polish is off unless the repo opts in (config.py: "silent
        # rewrite off by default").
        assert "title" not in edits[0]["body"]

    def test_an_opted_in_title_goes_in_the_same_patch(self, world):
        edits = self._metadata_edits(
            world, config_yaml="pull_requests:\n  auto_polish_title: true\n"
        )
        assert [w["method"] for w in edits] == ["PATCH"], "title and body must be one request"
        assert edits[0]["body"]["title"] == ANALYSIS["suggested_title"]
        assert edits[0]["body"]["body"].startswith("## Summary")

    def test_an_exact_code_fix_is_a_commit_button(self, world):
        world.llm.script = script(
            _review(_issue(3, "SQL injection via string concatenation", "cur.execute(q, (uid,))"))
        )
        repo = world.repo([SQL_FILE])
        world.open_pr(repo)
        world.sticky(repo)
        world.settle(repo)

        review = world.writes(repo, "POST")
        review = [w for w in review if w["path"].endswith("/reviews")]
        assert len(review) == 1
        comments = review[0]["body"]["comments"]
        assert comments[0]["line"] == 3
        assert "```suggestion\ncur.execute(q, (uid,))\n```" in comments[0]["body"]
        assert review[0]["body"]["commit_id"] == "HEADSHA1"

    def test_prose_and_moved_anchors_get_no_commit_button(self, world):
        world.llm.script = script(
            _review(
                _issue(3, "SQL injection", "Use a parameterised query instead"),
                _issue(6, "handle is never closed", "cur.close()", severity="major"),
            )
        )
        repo = world.repo([SQL_FILE])
        world.open_pr(repo)
        world.sticky(repo)
        world.settle(repo)

        posted = [w for w in world.writes(repo, "POST") if w["path"].endswith("/reviews")]
        comments = posted[0]["body"]["comments"]
        # The diff's last commentable line is 3, so BOTH findings anchor there:
        # match them by text, not by line.
        prose = next(c for c in comments if "SQL injection" in c["body"])
        moved = next(c for c in comments if "never closed" in c["body"])
        assert prose["line"] == 3
        assert "```suggestion" not in prose["body"], "English prose offered as a commit"
        assert "Use a parameterised query instead" in prose["body"]
        assert moved["line"] == 3, "expected line 6 to snap to the nearest commentable line"
        assert "```suggestion" not in moved["body"], "fix offered as a commit on a different line"
        assert "Reported at line 6; anchored to line 3" in moved["body"]
        assert "cur.close()" in moved["body"], "the fix must still be shown"

    def test_a_rejected_inline_review_keeps_its_findings(self, world):
        world.llm.script = script(
            _review(_issue(3, "SQL injection via string concatenation", "cur.execute(q, (uid,))"))
        )
        repo = world.repo(
            [SQL_FILE], reviews=(422, {"message": "Unprocessable Entity", "errors": ["line"]})
        )
        world.open_pr(repo)
        sticky = world.sticky(repo)
        world.settle(repo)

        assert "SQL injection via string concatenation" in sticky["body"]
        assert "Findings GitHub would not accept" in sticky["body"]

    def test_a_demoted_review_never_loses_a_finding(self, world):
        """Whatever the gate decides, each finding reaches the reader once."""
        world.llm.script = script(
            _review(
                _issue(3, "I'm not sure, as an AI, but this may be SQL injection", "x = 1"),
                confidence=0.05,
                summary="Bad",
            )
        )
        repo = world.repo([SQL_FILE])
        world.open_pr(repo)
        sticky = world.sticky(repo)
        world.settle(repo)

        inline = [
            c["body"]
            for w in world.writes(repo, "POST")
            if w["path"].endswith("/reviews")
            for c in w["body"]["comments"]
        ]
        in_diff = any("SQL injection" in b for b in inline)
        in_body = "SQL injection" in sticky["body"]
        assert in_diff != in_body, f"reported {'twice' if in_diff else 'nowhere'}"
        if not in_diff:
            assert "All findings posted as inline comments" not in sticky["body"]


@pytest.mark.usefixtures("show_log_on_failure")
class TestWhatGetsReviewed:
    def test_generated_files_are_skipped_and_latest_is_source(self, world):
        latest = {
            "filename": "app/core/latest_run.py",
            "status": "modified",
            "additions": 1,
            "deletions": 0,
            "patch": "@@ -1,1 +1,2 @@\n x = 1\n+y = run(x)\n",
        }
        files = [
            {
                "filename": "package-lock.json",
                "status": "modified",
                "patch": "@@ -1 +1 @@\n-a\n+b\n",
            },
            {"filename": "dist/bundle.js", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b\n"},
            latest,
        ]
        world.llm.script = script(
            _review(file="app/core/latest_run.py", summary="Looks fine overall here."),
            gaps={"has_gaps": False, "gaps": [], "summary": "ok"},
        )
        repo = world.repo(files)
        seen = len(world.llm.calls)
        world.open_pr(repo)
        world.sticky(repo)
        world.settle(repo)
        mine = world.llm.calls[seen:]  # this test's model calls only

        review_prompts = [c["user"] for c in mine if "Senior code reviewer" in c["system"]]
        assert len(review_prompts) == 1
        reviewed = review_prompts[0]
        assert "FILE: app/core/latest_run.py" in reviewed
        assert "FILE: package-lock.json" not in reviewed, "a lockfile took a review slot"
        assert "FILE: dist/bundle.js" not in reviewed, "a build artefact took a review slot"

        # If latest_run.py were read as a TEST file there would be no source
        # file left, and no gap analysis would be requested at all.
        gap_prompts = [c["user"] for c in mine if "Senior QA engineer" in c["system"]]
        assert len(gap_prompts) == 1, "latest_run.py read as a test file: no gap analysis ran"
        source_part = gap_prompts[0].split("Tests changed in this PR", 1)[0]
        assert "app/core/latest_run.py" in source_part


@pytest.mark.usefixtures("show_log_on_failure")
class TestDelivery:
    def test_a_bad_signature_writes_nothing(self, world):
        world.llm.script = script(_review())
        repo = world.repo([SQL_FILE])
        calls_before = len(world.llm.calls)
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 1,
                "title": "t",
                "user": {"login": "a"},
                "head": {},
                "base": {},
            },
            "repository": {"full_name": repo},
            "installation": {"id": INSTALLATION},
        }
        r = world.server.send("pull_request", payload, secret="not-the-secret")
        assert r.status_code == 401
        time.sleep(1.5)
        assert world.writes(repo) == []
        assert len(world.llm.calls) == calls_before

    def test_a_redelivery_is_processed_once(self, world):
        world.llm.script = script(_review())
        repo = world.repo([SQL_FILE])
        delivery = str(uuid.uuid4())
        assert world.open_pr(repo, delivery=delivery).status_code == 202
        second = world.open_pr(repo, delivery=delivery)
        assert "duplicate" in second.text
        world.sticky(repo)
        world.settle(repo)
        assert len([w for w in world.writes(repo, "POST") if w["path"].endswith("/comments")]) == 1

    def test_a_second_push_edits_the_same_report(self, world):
        world.llm.script = script(
            _review(_issue(3, "SQL injection via string concatenation", "cur.execute(q, (uid,))"))
        )
        repo = world.repo([SQL_FILE])
        world.open_pr(repo)
        first = world.sticky(repo)
        world.settle(repo)
        first_body = first["body"]

        world.llm.script = script(
            _review(_issue(4, "cursor is never closed", "cur.close()", severity="major"))
        )
        world.open_pr(repo, action="synchronize")
        wait_for(lambda: first["body"] != first_body, timeout=30)
        world.settle(repo)

        stickies = [c for c in world.comments[repo] if MARKER in c["body"]]
        assert len(stickies) == 1, "a second push posted a second report"
        assert [w["method"] for w in world.writes(repo) if "/issues/comments/" in w["path"]] == [
            "PATCH"
        ]


# ── Slash commands ───────────────────────────────────────────────────────────


def _command(world, repo, text, issue=5):
    """Post `text` as a comment from a fresh admin, and return the bot's reply."""
    user = f"admin{uuid.uuid4().hex[:6]}"
    gh = world.gh
    gh.route(
        "GET",
        f"/repos/{repo}/collaborators/{user}/permission",
        lambda r: (200, {"permission": "admin"}),
    )
    replies: list = []
    gh.route(
        "POST",
        f"/repos/{repo}/issues/{issue}/comments",
        lambda r: (replies.append(r["body"]["body"]) or (201, {"id": 1})),
    )
    gh.route("POST", f"/repos/{repo}/issues/comments/1/reactions", lambda r: (201, {}))
    world.server.send(
        "issue_comment",
        {
            "action": "created",
            "issue": {"number": issue, "title": "t", "body": "b", "user": {"login": "bob"}},
            "comment": {"id": 1, "body": text, "user": {"login": user, "type": "User"}},
            "repository": {"full_name": repo},
            "installation": {"id": INSTALLATION},
            "sender": {"login": user, "type": "User"},
        },
    )
    return wait_for(lambda: replies[0] if replies else None, timeout=30)


@pytest.mark.usefixtures("show_log_on_failure")
class TestCommands:
    def test_secfull_does_not_call_an_unreadable_repo_clear(self, world):
        """Every security API 404s here — the feature is off. The report said
        "All Clear" for a repository it had not read a single alert from."""
        repo = world.repo([SQL_FILE])
        reply = _command(world, repo, "/secfull")
        assert reply, "no reply"
        assert "All Clear" not in reply
        assert "Not Scanned" in reply

    def test_changelog_describes_only_unreleased_commits(self, world):
        repo = world.repo([SQL_FILE])
        world.gh.route(
            "GET",
            f"/repos/{repo}/tags",
            lambda r: (200, [{"name": "v1.2.0", "commit": {"sha": "TAG"}}]),
        )
        world.gh.route(
            "GET",
            f"/repos/{repo}/commits",
            lambda r: (
                200,
                [
                    {"sha": "NEW", "commit": {"message": "feat: unreleased work"}},
                    {"sha": "TAG", "commit": {"message": "feat: shipped in v1.2.0"}},
                ],
            ),
        )
        world.llm.script = {"CHANGELOG": "## [1.2.1] - 2026-09-26\n### Added\n- unreleased work"}
        seen = len(world.llm.calls)
        reply = _command(world, repo, "/changelog")
        assert reply and "CHANGELOG Entry" in reply
        prompt = next(c["user"] for c in world.llm.calls[seen:] if "CHANGELOG" in c["system"])
        assert "unreleased work" in prompt
        assert "shipped in v1.2.0" not in prompt, "re-described a released commit"


@pytest.mark.usefixtures("show_log_on_failure")
class TestRepoConfig:
    def test_a_repo_that_copied_the_old_example_gets_a_clean_footer(self, world):
        """The example config's footer was already wrapped, and Config.footer
        wrapped it again: this repository's own PR #108 report ended with
        "---\\n*\\n\\n---\\n*🤖 ...**"."""
        world.llm.script = script(_review())
        old_example = (
            "bot:\n" '  footer: "\\n\\n---\\n*🤖 GitHub Autopilot — AI-powered repo management*"\n'
        )
        repo = world.repo([SQL_FILE], config_yaml=old_example)
        world.open_pr(repo)
        sticky = world.sticky(repo)
        world.settle(repo)
        body = sticky["body"]
        assert "*🤖 GitHub Autopilot — AI-powered repo management*" in body
        assert "**" not in body.split("repo management", 1)[1][:3], "footer double-wrapped"
        assert "---\n*\n" not in body, "footer double-wrapped"


@pytest.mark.usefixtures("show_log_on_failure")
class TestLargePullRequests:
    def test_a_35_file_pr_is_reported_in_full(self, world):
        """GitHub pages PR files 30 at a time. The bot fetched one page, and
        reported this repository's own 32-file PR as 30 files with less than
        two thirds of its additions."""
        world.llm.script = script(_review(), gaps={"has_gaps": False, "gaps": [], "summary": "ok"})
        files = [SQL_FILE] + [
            {
                "filename": f"app/mod{i}.py",
                "status": "modified",
                "additions": 10,
                "deletions": 1,
                "patch": "@@ -1 +1,2 @@\n x = 1\n+y = 2\n",
            }
            for i in range(34)
        ]
        repo = world.repo(files)
        world.open_pr(repo)
        body = world.sticky(repo)["body"]
        world.settle(repo)
        assert "**Files:** 35" in body, body.split("\n\n")[1]
        assert "+342 −35" in body  # 2 + 34*10 additions, 1 + 34 deletions
