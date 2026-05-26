"""
tests/test_push.py
Sprint 8 — push handler tests.
Covers: secret scan, dep scan, commit lint, dedup, bot skip, branch filter.
"""

from unittest.mock import MagicMock, patch


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _payload(
    pusher="shweta",
    ref="refs/heads/main",
    commits=None,
    installation_id=99,
):
    return {
        "repository": {"full_name": "org/repo"},
        "pusher": {"name": pusher},
        "ref": ref,
        "commits": commits if commits is not None else [{"id": "abc1234", "message": "feat: add login", "added": [], "modified": []}],
        "installation": {"id": installation_id},
    }


def _commit(sha="abc1234", msg="feat: add login", added=None, modified=None):
    return {
        "id": sha,
        "message": msg,
        "added": added or [],
        "modified": modified or [],
    }


def _mock_config(enabled=True, scan_secrets=True, scan_deps=True, conv_commits=True):
    cfg = MagicMock()
    cfg.get.side_effect = lambda *args, **kw: {
        ("push", "enabled"): enabled,
        ("push", "scan_secrets"): scan_secrets,
        ("push", "scan_dependencies"): scan_deps,
        ("push", "enforce_conventional_commits"): conv_commits,
        ("push", "create_issue_threshold"): 3,
    }.get(args, kw.get("default", True))
    return cfg


# ── Skip tests ────────────────────────────────────────────────────────────────

class TestHandleSkips:

    def test_bot_pusher_skipped(self):
        with patch("app.handlers.push.get_installation_token") as mock_tok:
            from app.handlers.push import handle
            handle(_payload(pusher="dependabot[bot]"))
            mock_tok.assert_not_called()

    def test_non_main_branch_skipped(self):
        with patch("app.handlers.push.get_installation_token") as mock_tok:
            from app.handlers.push import handle
            handle(_payload(ref="refs/heads/feature/foo"))
            mock_tok.assert_not_called()

    def test_empty_commits_skipped(self):
        import importlib
        import app.handlers.push as push_mod
        importlib.reload(push_mod)
        with patch.object(push_mod, 'get_installation_token') as mock_tok,              patch.object(push_mod, 'load_config', return_value=_mock_config()):
            push_mod.handle(_payload(commits=[]))
            mock_tok.assert_not_called()

    def test_master_branch_allowed(self):
        with patch("app.handlers.push.get_installation_token", return_value="tok"), \
             patch("app.handlers.push.load_config", return_value=_mock_config()), \
             patch("app.handlers.push._scan_secrets"), \
             patch("app.handlers.push._scan_dependencies"), \
             patch("app.handlers.push._lint_commits"), \
             patch("app.handlers.push._index_changed_files"):
            from app.handlers.push import handle
            handle(_payload(ref="refs/heads/master"))  # Should not skip

    def test_auth_failure_returns_early(self):
        with patch("app.handlers.push.get_installation_token", side_effect=Exception("auth failed")), \
             patch("app.handlers.push._scan_secrets") as mock_scan:
            from app.handlers.push import handle
            handle(_payload())
            mock_scan.assert_not_called()


# ── Conventional commit tests ─────────────────────────────────────────────────

class TestIsConventional:

    def test_valid_types(self):
        from app.handlers.push import _is_conventional
        for msg in [
            "feat: add login",
            "fix: correct typo",
            "docs: update readme",
            "refactor: extract helper",
            "test: add unit tests",
            "chore: bump version",
            "perf: cache results",
            "ci: add lint step",
        ]:
            assert _is_conventional(msg), f"Expected valid: {msg}"

    def test_with_scope(self):
        from app.handlers.push import _is_conventional
        assert _is_conventional("feat(auth): add OAuth")
        assert _is_conventional("fix(api): handle 404")

    def test_breaking_change_marker(self):
        from app.handlers.push import _is_conventional
        assert _is_conventional("feat!: breaking change")
        assert _is_conventional("fix(core)!: breaking fix")

    def test_invalid_types(self):
        from app.handlers.push import _is_conventional
        for msg in [
            "add login",
            "WIP: stuff",
            "update things",
            "",
            "FEAT: uppercase not valid",
        ]:
            assert not _is_conventional(msg), f"Expected invalid: {msg}"


# ── Secret scan tests ─────────────────────────────────────────────────────────

class TestScanSecrets:

    def _base_patches(self):
        return [
            patch("app.handlers.push.gh_get"),
            patch("app.handlers.push.gh_post"),
            patch("app.handlers.push.notify_secret_detected"),
            patch("app.handlers.push._already_reported", return_value=False),
        ]

    def test_no_findings_no_issue(self):
        with patch("app.handlers.push.gh_get", return_value={"files": []}), \
             patch("app.handlers.push.gh_post") as mock_post, \
             patch("app.handlers.push.scan_diff", return_value=[]):
            from app.handlers.push import _scan_secrets
            log = MagicMock()
            _scan_secrets("org/repo", [_commit()], "tok", MagicMock(), log)
            mock_post.assert_not_called()

    def test_findings_creates_issue(self):
        fake_finding = MagicMock()
        fake_finding.pattern_name = "GitHub PAT (classic)"
        with patch("app.handlers.push.gh_get", return_value={"files": [{"patch": "+token=ghp_xxx"}]}), \
             patch("app.handlers.push.gh_post") as mock_post, \
             patch("app.handlers.push.scan_diff", return_value=[fake_finding]), \
             patch("app.handlers.push.format_secret_findings", return_value="## Secret"), \
             patch("app.handlers.push._already_reported", return_value=False), \
             patch("app.handlers.push.notify_secret_detected"):
            from app.handlers.push import _scan_secrets
            log = MagicMock()
            _scan_secrets("org/repo", [_commit()], "tok", MagicMock(), log)
            mock_post.assert_called_once()
            args = mock_post.call_args[0]
            assert "issues" in args[0]

    def test_dedup_suppresses_second_issue(self):
        """SPRINT 8 KEY TEST: same finding set within 1h → only 1 issue."""
        fake_finding = MagicMock()
        fake_finding.pattern_name = "GitHub PAT (classic)"

        post_calls = []

        def fake_already_reported(repo, key, ttl_seconds=3600):
            # First call → False (not reported), second → True (already done)
            already = len(post_calls) > 0
            return already

        with patch("app.handlers.push.gh_get", return_value={"files": [{"patch": "+tok=ghp_xxx"}]}), \
             patch("app.handlers.push.gh_post", side_effect=lambda *a, **kw: post_calls.append(1)) as mock_post, \
             patch("app.handlers.push.scan_diff", return_value=[fake_finding]), \
             patch("app.handlers.push.format_secret_findings", return_value="## Secret"), \
             patch("app.handlers.push._already_reported", side_effect=fake_already_reported), \
             patch("app.handlers.push.notify_secret_detected"):
            from app.handlers.push import _scan_secrets
            log = MagicMock()
            commits = [_commit()]
            _scan_secrets("org/repo", commits, "tok", MagicMock(), log)  # creates issue
            _scan_secrets("org/repo", commits, "tok", MagicMock(), log)  # deduped

        assert mock_post.call_count == 1, (
            "SPRINT 8 REGRESSION: _scan_secrets dedup failed — "
            "second push with same secrets created duplicate issue"
        )

    def test_different_findings_not_deduped(self):
        """Different secret patterns → different dedup key → both issues created."""
        call_count = [0]

        def mock_already(repo, key, ttl_seconds=3600):
            call_count[0] += 1
            return False  # Always allow — different keys

        finding1 = MagicMock()
        finding1.pattern_name = "GitHub PAT (classic)"
        finding2 = MagicMock()
        finding2.pattern_name = "AWS Access Key ID"

        with patch("app.handlers.push.gh_get", return_value={"files": [{"patch": "+t=x"}]}), \
             patch("app.handlers.push.gh_post") as mock_post, \
             patch("app.handlers.push.format_secret_findings", return_value="## S"), \
             patch("app.handlers.push._already_reported", side_effect=mock_already), \
             patch("app.handlers.push.notify_secret_detected"):
            from app.handlers.push import _scan_secrets
            log = MagicMock()
            with patch("app.handlers.push.scan_diff", return_value=[finding1]):
                _scan_secrets("org/repo", [_commit()], "tok", MagicMock(), log)
            with patch("app.handlers.push.scan_diff", return_value=[finding2]):
                _scan_secrets("org/repo", [_commit()], "tok", MagicMock(), log)

        assert mock_post.call_count == 2

    def test_gh_get_error_handled_gracefully(self):
        with patch("app.handlers.push.gh_get", side_effect=Exception("network error")), \
             patch("app.handlers.push.gh_post") as mock_post:
            from app.handlers.push import _scan_secrets
            log = MagicMock()
            _scan_secrets("org/repo", [_commit()], "tok", MagicMock(), log)
            mock_post.assert_not_called()


# ── Findings dedup key tests ──────────────────────────────────────────────────

class TestFindingsDedupKey:

    def test_same_patterns_same_key(self):
        from app.handlers.push import _findings_dedup_key
        f1, f2 = MagicMock(), MagicMock()
        f1.pattern_name = "GitHub PAT (classic)"
        f2.pattern_name = "AWS Access Key ID"
        key_a = _findings_dedup_key([f1, f2])
        key_b = _findings_dedup_key([f2, f1])   # Different order
        assert key_a == key_b   # Order-independent

    def test_different_patterns_different_key(self):
        from app.handlers.push import _findings_dedup_key
        f1, f2 = MagicMock(), MagicMock()
        f1.pattern_name = "GitHub PAT (classic)"
        f2.pattern_name = "Stripe Secret Key"
        key_a = _findings_dedup_key([f1])
        key_b = _findings_dedup_key([f2])
        assert key_a != key_b


# ── Dependency scan tests ─────────────────────────────────────────────────────

class TestScanDependencies:

    def test_no_dep_files_no_scan(self):
        with patch("app.handlers.push.gh_get"), \
             patch("app.handlers.push.gh_post") as mock_post:
            from app.handlers.push import _scan_dependencies
            log = MagicMock()
            commits = [_commit(added=["app/main.py"])]
            _scan_dependencies("org/repo", commits, "tok", MagicMock(), log)
            mock_post.assert_not_called()

    def test_clean_requirements_no_issue(self):
        import base64
        content = base64.b64encode(b"flask==2.0.0\nrequests==2.28.0").decode()
        with patch("app.handlers.push.gh_get", return_value={"content": content}), \
             patch("app.handlers.push.gh_post") as mock_post, \
             patch("app.handlers.push.scan_requirements_txt", return_value=[]), \
             patch("app.handlers.push.get_actionable_findings", return_value=[]):
            from app.handlers.push import _scan_dependencies
            log = MagicMock()
            commits = [_commit(modified=["requirements.txt"])]
            _scan_dependencies("org/repo", commits, "tok", MagicMock(), log)
            mock_post.assert_not_called()

    def test_high_severity_creates_issue(self):
        import base64
        content = base64.b64encode(b"insecure-package==1.0.0").decode()
        high_finding = MagicMock()
        high_finding.severity = "HIGH"
        with patch("app.handlers.push.gh_get", return_value={"content": content}), \
             patch("app.handlers.push.gh_post") as mock_post, \
             patch("app.handlers.push.scan_requirements_txt", return_value=[high_finding]), \
             patch("app.handlers.push.get_actionable_findings", return_value=[high_finding]), \
             patch("app.handlers.push.format_dep_findings", return_value="## Deps"), \
             patch("app.handlers.push._already_reported", return_value=False):
            from app.handlers.push import _scan_dependencies
            log = MagicMock()
            commits = [_commit(modified=["requirements.txt"])]
            _scan_dependencies("org/repo", commits, "tok", MagicMock(), log)
            mock_post.assert_called_once()

    def test_low_severity_no_issue(self):
        import base64
        content = base64.b64encode(b"oldlib==0.1.0").decode()
        low_finding = MagicMock()
        low_finding.severity = "LOW"
        with patch("app.handlers.push.gh_get", return_value={"content": content}), \
             patch("app.handlers.push.gh_post") as mock_post, \
             patch("app.handlers.push.scan_requirements_txt", return_value=[low_finding]), \
             patch("app.handlers.push.get_actionable_findings", return_value=[]):
            from app.handlers.push import _scan_dependencies
            log = MagicMock()
            commits = [_commit(modified=["requirements.txt"])]
            _scan_dependencies("org/repo", commits, "tok", MagicMock(), log)
            mock_post.assert_not_called()

    def test_dep_dedup_suppresses_second_issue(self):
        import base64
        content = base64.b64encode(b"bad==1.0.0").decode()
        high_finding = MagicMock()
        high_finding.severity = "HIGH"
        with patch("app.handlers.push.gh_get", return_value={"content": content}), \
             patch("app.handlers.push.gh_post") as mock_post, \
             patch("app.handlers.push.scan_requirements_txt", return_value=[high_finding]), \
             patch("app.handlers.push.get_actionable_findings", return_value=[high_finding]), \
             patch("app.handlers.push.format_dep_findings", return_value="## D"), \
             patch("app.handlers.push._already_reported", return_value=True):
            from app.handlers.push import _scan_dependencies
            log = MagicMock()
            commits = [_commit(modified=["requirements.txt"])]
            _scan_dependencies("org/repo", commits, "tok", MagicMock(), log)
            mock_post.assert_not_called()


# ── Commit lint tests ─────────────────────────────────────────────────────────

class TestLintCommits:

    def test_all_conventional_no_issue(self):
        commits = [
            _commit(msg="feat: add login"),
            _commit(msg="fix: correct bug"),
        ]
        with patch("app.handlers.push.gh_post") as mock_post, \
             patch("app.handlers.push._already_reported", return_value=False):
            from app.handlers.push import _lint_commits
            cfg = _mock_config()
            log = MagicMock()
            _lint_commits("org/repo", commits, "tok", cfg, log)
            mock_post.assert_not_called()

    def test_below_threshold_no_issue(self):
        commits = [
            _commit(msg="WIP: stuff"),
            _commit(msg="update things"),
        ]
        with patch("app.handlers.push.gh_post") as mock_post, \
             patch("app.handlers.push._already_reported", return_value=False):
            from app.handlers.push import _lint_commits
            cfg = _mock_config()
            log = MagicMock()
            _lint_commits("org/repo", commits, "tok", cfg, log)
            mock_post.assert_not_called()  # 2 < threshold of 3

    def test_above_threshold_creates_issue(self):
        commits = [
            _commit(sha="a1b2c3d", msg="update stuff"),
            _commit(sha="b2c3d4e", msg="fix things maybe"),
            _commit(sha="c3d4e5f", msg="WIP"),
        ]
        with patch("app.handlers.push.gh_post") as mock_post, \
             patch("app.handlers.push._already_reported", return_value=False):
            from app.handlers.push import _lint_commits
            cfg = _mock_config()
            log = MagicMock()
            _lint_commits("org/repo", commits, "tok", cfg, log)
            mock_post.assert_called_once()

    def test_commit_lint_dedup(self):
        commits = [_commit(msg="bad") for _ in range(5)]
        with patch("app.handlers.push.gh_post") as mock_post, \
             patch("app.handlers.push._already_reported", return_value=True):
            from app.handlers.push import _lint_commits
            cfg = _mock_config()
            log = MagicMock()
            _lint_commits("org/repo", commits, "tok", cfg, log)
            mock_post.assert_not_called()

