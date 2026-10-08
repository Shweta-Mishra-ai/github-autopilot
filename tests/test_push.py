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
    default_branch="main",
):
    return {
        "repository": {"full_name": "org/repo", "default_branch": default_branch},
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
        """V5: non-main branches still run secret scan, so token IS fetched."""
        with patch("app.handlers.push.get_installation_token", return_value="tok") as mock_tok, \
             patch("app.handlers.push.load_config", return_value=_mock_config()), \
             patch("app.handlers.push._scan_secrets"), \
             patch("app.handlers.push._scan_dependencies"), \
             patch("app.handlers.push._lint_commits"):
            from app.handlers.push import handle
            handle(_payload(ref="refs/heads/feature/foo"))

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
             patch("app.handlers.push._lint_commits"):
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
        fake_finding.severity = "critical"
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

    def test_different_findings_reuse_the_same_alert_issue(self):
        """
        V7: two pushes with different secret patterns must NOT open two issues.

        This test previously asserted the opposite — "different patterns →
        different dedup key → both issues created" — which is the defect that
        put seven secret issues in this repo inside 73 seconds. The dedup key
        is now per-repo, and later findings comment on the open alert.
        """
        finding1 = MagicMock()
        finding1.pattern_name = "GitHub PAT (classic)"
        finding1.severity = "critical"
        finding2 = MagicMock()
        finding2.pattern_name = "AWS Access Key ID"
        finding2.severity = "critical"

        fake_redis = MagicMock()
        fake_redis.get.return_value = None      # no alert recorded yet
        fake_redis.set.return_value = True

        with patch("app.handlers.push.gh_get", return_value={"files": [{"patch": "+t=x"}]}), \
             patch("app.handlers.push.gh_post", return_value={"number": 9}) as mock_post, \
             patch("app.handlers.push.format_secret_findings", return_value="## S"), \
             patch("app.core.redis_client.get_redis", return_value=fake_redis), \
             patch("app.handlers.push.notify_secret_detected"):
            from app.handlers.push import _scan_secrets
            log = MagicMock()
            with patch("app.handlers.push.scan_diff", return_value=[finding1]):
                _scan_secrets("org/repo", [_commit()], "tok", MagicMock(), log)

            # Second push: the alert issue from the first is now on record.
            # gh_get serves two different lookups here — the commit diff and
            # the state of the existing alert issue — so dispatch on path.
            fake_redis.get.return_value = "9"

            def _gh_get(path, _token):
                if "/commits/" in path:
                    return {"files": [{"patch": "+t=x", "filename": "app/a.py"}]}
                return {"state": "open"}

            with patch("app.handlers.push.scan_diff", return_value=[finding2]), \
                 patch("app.handlers.push.gh_get", side_effect=_gh_get):
                _scan_secrets("org/repo", [_commit()], "tok", MagicMock(), log)

        paths = [c[0][0] for c in mock_post.call_args_list]
        assert paths[0] == "/repos/org/repo/issues"
        assert paths[1] == "/repos/org/repo/issues/9/comments"

    def test_medium_severity_alone_opens_no_issue(self):
        """The entropy heuristic fires on hashes and UUIDs — those are medium."""
        finding = MagicMock()
        finding.pattern_name = "High Entropy String"
        finding.severity = "medium"

        with patch("app.handlers.push.gh_get", return_value={"files": [{"patch": "+h=abc"}]}), \
             patch("app.handlers.push.gh_post") as mock_post, \
             patch("app.handlers.push.scan_diff", return_value=[finding]), \
             patch("app.handlers.push._already_reported", return_value=False), \
             patch("app.handlers.push.notify_secret_detected"):
            from app.handlers.push import _scan_secrets
            _scan_secrets("org/repo", [_commit()], "tok", MagicMock(), MagicMock())
            mock_post.assert_not_called()

    def test_gh_get_error_handled_gracefully(self):
        with patch("app.handlers.push.gh_get", side_effect=Exception("network error")), \
             patch("app.handlers.push.gh_post") as mock_post:
            from app.handlers.push import _scan_secrets
            log = MagicMock()
            _scan_secrets("org/repo", [_commit()], "tok", MagicMock(), log)
            mock_post.assert_not_called()


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



class TestSecretScanSkip:
    """Only files whose purpose is stand-in values are skipped — as the
    scanner defines them, matched on path segments."""

    def test_skips_test_and_example_paths(self):
        from app.handlers.push import _skip_secret_scan

        for p in [
            "tests/test_memory.py",
            "app/foo_test.py",
            "test_secrets.py",
            ".env.example",
            "config/prod.env.example",
            "fixtures/keys.txt",
        ]:
            assert _skip_secret_scan(p) is True, p

    def test_scans_real_source_paths(self):
        from app.handlers.push import _skip_secret_scan

        for p in ["app/handlers/push.py", "server.py", "src/config.py", ""]:
            assert _skip_secret_scan(p) is False, p


class TestSecretScanPaths:
    def test_a_directory_name_containing_test_or_docs_is_not_skipped(self):
        """Substring matching skipped all of these: contest/, protest/, attest/
        and latest/ contain "test/"; mydocs/ contains "docs/"."""
        from app.handlers.push import _skip_secret_scan

        for p in [
            "deploy/latest/settings.py",
            "app/contest/config.py",
            "src/mydocs/creds.py",
            "infra/protest/keys.env",
            "services/attest/secrets.yaml",
        ]:
            assert _skip_secret_scan(p) is False, p

    def test_documentation_is_scanned_for_real_tokens(self):
        """docs/ was skipped outright; the scanner scans prose with its
        high-specificity patterns, so a pasted token in docs is found."""
        from app.handlers.push import _skip_secret_scan
        from app.security.enhanced_secrets import scan_diff

        assert _skip_secret_scan("docs/setup.md") is False
        token = "ghp_" + "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5"
        found = scan_diff(f"+export GITHUB_TOKEN={token}\n", file_path="docs/setup.md")
        assert found and found[0].severity == "critical"


class _KV:
    """The two Redis calls the secret path makes, with real NX semantics."""

    def __init__(self):
        self.data = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    def get(self, key):
        return self.data.get(key)


def _finding(pattern="GitHub PAT (classic)", value="ghp_…Ab12", path="app/a.py"):
    from app.security.enhanced_secrets import SecretFinding

    return SecretFinding(
        pattern_name=pattern, line_number=1, severity="critical",
        redacted_match=value, file_path=path,
    )


class TestSecretDedupIsPerFinding:
    """The dedup key was per repo: after one alert, every other secret pushed
    to that repo for an hour was dropped and never re-scanned."""

    def _push(self, kv, findings):
        posts = []

        def _gh_get(path, _token):
            if "/commits/" in path:
                return {"files": [{"patch": "+x", "filename": "app/a.py"}]}
            return {"state": "open"}

        def _gh_post(path, _token, body):
            posts.append(path)
            return {"number": 7}

        with patch("app.core.redis_client.get_redis", return_value=kv), \
             patch("app.handlers.push.gh_get", side_effect=_gh_get), \
             patch("app.handlers.push.gh_post", side_effect=_gh_post), \
             patch("app.handlers.push.scan_diff", return_value=findings), \
             patch("app.handlers.push.notify_secret_detected"):
            from app.handlers.push import _scan_secrets

            _scan_secrets("org/repo", [_commit()], "tok", MagicMock(), MagicMock())
        return posts

    def test_the_same_secret_pushed_again_is_reported_once(self):
        kv = _KV()
        assert self._push(kv, [_finding()]) == ["/repos/org/repo/issues"]
        assert self._push(kv, [_finding()]) == []

    def test_a_different_secret_minutes_later_is_reported(self):
        kv = _KV()
        self._push(kv, [_finding()])
        posts = self._push(kv, [_finding("AWS Access Key ID", "AKIA…WXYZ")])
        assert posts == ["/repos/org/repo/issues/7/comments"], (
            "a new secret must be appended to the open alert, not dropped"
        )

    def test_only_the_new_findings_are_reported(self):
        kv = _KV()
        self._push(kv, [_finding()])
        with patch("app.handlers.push.format_secret_findings", return_value="b") as fmt:
            self._push(kv, [_finding(), _finding("AWS Access Key ID", "AKIA…WXYZ")])
        assert [f.pattern_name for f in fmt.call_args[0][0]] == ["AWS Access Key ID"]

    def test_one_secret_in_several_commits_is_one_finding(self):
        kv = _KV()
        with patch("app.handlers.push.format_secret_findings", return_value="b") as fmt:
            self._push(kv, [_finding(), _finding()])
        assert len(fmt.call_args[0][0]) == 1

    def test_redis_down_reports_nothing_rather_than_duplicates(self):
        class Down:
            def set(self, *a, **kw):
                raise ConnectionError("down")

            def get(self, *a):
                raise ConnectionError("down")

        assert self._push(Down(), [_finding()]) == []

    def test_the_fingerprint_does_not_contain_the_value(self):
        from app.handlers.push import _secret_fingerprint

        fp = _secret_fingerprint(_finding(value="ghp_…Ab12"))
        assert "Ab12" not in fp and len(fp) == 24


class TestDefaultBranch:
    """The default branch is the repository's, not a hard-coded main/master."""

    def _run(self, ref, default_branch):
        with patch("app.handlers.push.get_installation_token", return_value="tok"), \
             patch("app.handlers.push.load_config", return_value=_mock_config()), \
             patch("app.handlers.push._scan_secrets"), \
             patch("app.handlers.push._scan_dependencies") as deps, \
             patch("app.handlers.push._lint_commits") as lint, \
             patch("app.handlers.readme.maybe_update_readme"), \
             patch("app.handlers.commit_message.suggest_commit_messages"):
            from app.handlers.push import handle

            handle(_payload(ref=ref, default_branch=default_branch))
        return deps, lint

    def test_a_develop_default_branch_gets_the_full_scan(self):
        deps, lint = self._run("refs/heads/develop", "develop")
        deps.assert_called_once()
        assert lint.call_args.kwargs["branch"] == "develop"

    def test_a_stray_master_in_a_main_repo_is_not_the_default(self):
        deps, lint = self._run("refs/heads/master", "main")
        deps.assert_not_called()
        lint.assert_not_called()

    def test_the_dependency_scan_reads_the_pushed_commit(self):
        deps, _ = self._run("refs/heads/main", "main")
        assert deps.call_args.kwargs["ref"] == "abc1234"

    def test_the_dependency_file_is_fetched_at_that_commit(self):
        from app.handlers.push import _scan_dependencies

        commits = [_commit(modified=["requirements.txt"])]
        with patch("app.handlers.push.gh_get", side_effect=Exception("stop")) as get:
            _scan_dependencies("org/repo", commits, "tok", _mock_config(), MagicMock(), ref="abc1234")
        assert get.call_args[0][0] == "/repos/org/repo/contents/requirements.txt?ref=abc1234"

    def test_the_lint_issue_names_the_branch(self):
        commits = [_commit(sha=f"abc{i}xxx", msg="wip") for i in range(4)]
        with patch("app.handlers.push._already_reported", return_value=False), \
             patch("app.handlers.push.gh_post") as post:
            from app.handlers.push import _lint_commits

            _lint_commits("org/repo", commits, "tok", _mock_config(), MagicMock(), branch="trunk")
        assert post.call_args[0][2]["title"].endswith("pushed to trunk")
