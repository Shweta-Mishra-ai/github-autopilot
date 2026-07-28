import os
import base64
from unittest.mock import MagicMock, patch

import app.handlers.comments.generator as G
import app.handlers.comments.reviewer as R
import app.handlers.comments.publisher as P
import app.handlers.comments.security as SEC
import app.handlers.comments.service as S

# Mock responses
mock_router_response = ({"root_cause": "cause", "fix": "fix", "explanation": "exp", "test": "test", "confidence": 0.9}, None)
mock_router_text_response = ("some text", None)

# V7: generator commands now go through app.ai.guarded.guarded_ask, which
# returns (payload, HallucinationResult) instead of (payload, meta).
from app.ai.hallucination import HallucinationResult
_clean_verdict = HallucinationResult(confidence=0.9, is_acceptable=True)

@patch("app.handlers.comments.router")
@patch("app.handlers.comments.generator.guarded_ask")
def test_generator_commands(mock_safe_ask, mock_router):
    mock_router.ask.return_value = mock_router_response
    mock_router.ask_text.return_value = mock_router_text_response
    mock_safe_ask.return_value = (mock_router_response[0], _clean_verdict)

    # cmd_fix
    res = G.cmd_fix("title", "context")
    assert "Fix" in res

    # cmd_explain
    res = G.cmd_explain("context")
    assert "Explanation" in res

    # cmd_improve
    mock_safe_ask.return_value = ({"summary": "sum", "improvements": [{"area": "perf", "suggestion": "sug", "example": "ex"}]}, _clean_verdict)
    res = G.cmd_improve("context")
    assert "Improvements" in res

    # cmd_test
    mock_safe_ask.return_value = ({"framework": "pytest", "tests": [{"name": "t1", "type": "unit", "desc": "d", "code": "c"}]}, _clean_verdict)
    res = G.cmd_test("context")
    assert "pytest" in res

    # cmd_docs
    mock_safe_ask.return_value = ({"docstring": "doc", "usage": "use", "readme_section": "readme"}, _clean_verdict)
    res = G.cmd_docs("context")
    assert "documentation" in res.lower()

    # cmd_refactor
    mock_safe_ask.return_value = ({"summary": "sum", "refactors": [{"type": "extract_function", "description": "desc", "before": "before", "after": "after", "benefit": "benefit"}]}, _clean_verdict)
    res = G.cmd_refactor("context")
    assert "refactor" in res.lower()

    # cmd_gaps
    mock_safe_ask.return_value = ({"gaps": [{"area": "perf", "risk": "high", "suggested_test": "t"}]}, _clean_verdict)
    res = G.cmd_gaps("context")
    assert "Gaps" in res

    # cmd_perf
    mock_safe_ask.return_value = ({"overall_rating": "fast", "complexity_issues": [{"location": "loc", "current_complexity": "O(n)", "issue": "i", "fix": "f", "improvement": "imp"}], "quick_wins": ["w"]}, _clean_verdict)
    res = G.cmd_perf("context")
    assert "Performance" in res

    # cmd_arch
    with patch("app.handlers.comments.generator.gh_get", create=True) as mock_gh_get:
        mock_gh_get.return_value = [{"filename": "f.py"}]
        mock_safe_ask.return_value = ({"health": "good", "violations": [{"type": "layer", "severity": "high", "location": "loc", "description": "d", "recommendation": "r"}], "positive_patterns": ["p"], "refactoring_priority": "planned", "summary": "s"}, _clean_verdict)
        res = G.cmd_arch("repo", 1, {"pull_request": {}}, "token")
        assert "Architecture" in res

@patch("app.handlers.comments.reviewer.gh_get")
@patch("app.handlers.comments.router")
@patch("app.ai.guarded.safe_router_ask")
def test_reviewer_commands(mock_guarded_ask, mock_router, mock_gh_get):
    mock_router.ask_text.return_value = mock_router_text_response
    mock_router.ask.return_value = ({"version": "v1.0.0", "title": "t", "release_notes": "notes", "highlights": ["h"]}, None)

    # Health check
    def gh_get_side_effect(url, token=None):
        if "tags" in url:
            return [{"name": "v1.0.0"}]
        if "releases" in url:
            return [{"name": "v1.0.0"}]
        if "commits" in url:
            return [{"sha": "sha123", "commit": {"message": "commit msg"}}]
        if "contents" in url:
            return {"content": base64.b64encode(b"v1.0.0").decode()}
        if "comments" in url:
            return [{"user": {"login": "user"}, "body": "comment"}]
        if "pulls" in url or "issues" in url:
            return {
                "head": {"sha": "sha123", "ref": "feat"},
                "base": {"ref": "feature-branch"},
                "title": "title",
                "body": "body",
                "license": {"key": "mit"},
                "description": "desc",
                "commits": 1,
                "draft": False,
                "mergeable": True
            }
        return {}

    mock_gh_get.side_effect = gh_get_side_effect

    # cmd_health
    res = R.cmd_health("repo", "token")
    assert "Health" in res

    # cmd_version
    res = R.cmd_version("repo", "token")
    assert "Version" in res

    # cmd_summarize
    res = R.cmd_summarize("repo", 1, "token")
    assert "Summary" in res

    # cmd_ci
    mock_gh_get.side_effect = None
    mock_gh_get.return_value = {"check_runs": [{"name": "ci", "status": "completed", "conclusion": "success", "html_url": "url"}]}
    res = R.cmd_ci("args", "repo", "token")
    assert "CI" in res

    # cmd_budget
    mock_redis = MagicMock()
    mock_redis.get.return_value = "10"
    with patch("app.core.redis_client.get_redis", return_value=mock_redis):
        res = R.cmd_budget()
        assert "Budget" in res

    # cmd_report
    mock_redis.lrange.return_value = ['{"repo": "r", "pr": 1, "by": "u", "at": 123, "sha": "abc"}']
    with patch("app.core.redis_client.get_redis", return_value=mock_redis):
        res = R.cmd_report("repo")
        assert "Report" in res

    # cmd_impact
    mock_gh_get.side_effect = None
    mock_gh_get.return_value = [{"filename": "f.py", "additions": 10, "deletions": 5}]
    mock_guarded_ask.return_value = ({"summary": "a clear one sentence impact statement",
                                      "affected_systems": ["api"],
                                      "breaking_change_risk": "low",
                                      "requires_migration": False,
                                      "review_priority": "low",
                                      "notes": ""}, None)
    res = R.cmd_impact("repo", 1, {"pull_request": {}}, "token")
    assert "Impact" in res

    # cmd_changelog
    mock_gh_get.side_effect = gh_get_side_effect
    res = R.cmd_changelog("repo", "token")
    assert "CHANGELOG" in res

@patch("app.handlers.comments.publisher.gh_get")
@patch("app.handlers.comments.publisher.gh_post")
@patch("app.handlers.comments.publisher.gh_put")
@patch("app.handlers.comments.publisher.gh_delete")
@patch("app.handlers.comments.router")
def test_publisher_commands(mock_router, mock_gh_delete, mock_gh_put, mock_gh_post, mock_gh_get):
    mock_router.ask.return_value = ({"version": "v1.0.1", "title": "t", "release_notes": "notes", "highlights": ["h"]}, None)

    def gh_get_side_effect(url, token=None):
        if "reviews" in url:
            return []
        if "check-runs" in url:
            return {"check_runs": []}
        if "tags" in url:
            return [{"name": "v1.0.0"}]
        if "releases" in url:
            return [{"name": "v1.0.0"}]
        if "commits" in url:
            return [{"sha": "sha123", "commit": {"message": "commit msg"}}]
        if "contents" in url:
            return {"content": base64.b64encode(b"v1.0.0").decode()}
        if "comments" in url:
            return [{"user": {"login": "user"}, "body": "comment"}]
        if "pulls" in url or "issues" in url:
            return {
                "head": {"sha": "sha123", "ref": "feat"},
                "base": {"ref": "feature-branch"},
                "title": "title",
                "body": "body",
                "license": {"key": "mit"},
                "description": "desc",
                "commits": 1,
                "draft": False,
                "mergeable": True
            }
        return {}

    mock_gh_get.side_effect = gh_get_side_effect
    mock_gh_put.return_value = {"merged": True, "sha": "sha123"}

    # cmd_merge
    mock_config = MagicMock()
    mock_config.auto_merge_enabled.return_value = True
    mock_config.get.return_value = True
    res = P.cmd_merge("repo", 1, {"pull_request": {}}, "token", "author", mock_config)
    assert "Merged" in res

    # cmd_apply
    mock_gh_get.side_effect = None
    mock_gh_get.return_value = {"default_branch": "main"}
    mock_gh_post.return_value = {"number": 2, "title": "t", "html_url": "url"}
    res = P.cmd_apply("repo", 1, "token", "fix/bot-issue-1")
    assert "PR Created" in res

    # cmd_rollback
    with patch("app.core.snapshot.take_snapshot"), \
         patch("app.core.snapshot.get_snapshot_by_number") as mock_get_snap, \
         patch("app.core.snapshot.format_rollback_result") as mock_format:
        mock_get_snap.return_value = {"bot_actions": [{"type": "create_issue", "number": 1}], "timestamp": "2026-06-25T12:00:00"}
        mock_format.return_value = "Rollback Completed"
        res = P.cmd_rollback("repo", 1, "token", "1 confirm", "author")
        assert "Rollback" in res

    # cmd_release
    mock_gh_get.side_effect = gh_get_side_effect
    mock_gh_post.return_value = {"html_url": "url"}
    res = P.cmd_release("repo", "token", "author")
    assert "Release" in res

    # cmd_runtests
    mock_gh_get.side_effect = [{"default_branch": "main"}, {"workflows": [{"id": 1, "name": "ci", "path": ".github/workflows/ci.yml"}]}]
    res = P.cmd_runtests("repo", "token")
    assert "Tests Triggered" in res

    # cmd_notify
    with patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "http://discord"}):
        with patch("app.github.notifications.send_rich_discord") as mock_send:
            mock_send.return_value = (True, "ok")
            res = P.cmd_notify("repo", 1, {"labels": [{"name": "bug"}]}, "token", "note")
            assert "Notification Sent" in res

    # cmd_security
    mock_gh_get.side_effect = None
    mock_gh_get.return_value = [{"filename": "f.py", "patch": "diff"}]
    res = SEC.cmd_security("repo", 1, {"pull_request": {}}, "token")
    assert "Security" in res

    # cmd_secfull
    with patch("app.security.scanner.run_security_scan") as mock_scan:
        mock_report = MagicMock()
        mock_report.to_markdown.return_value = "report"
        mock_scan.return_value = mock_report
        res = SEC.cmd_secfull("repo", "token")
        assert "report" in res

@patch("app.handlers.comments.service.get_installation_token")
@patch("app.handlers.comments.service.load_config")
@patch("app.handlers.comments.service.check_user_rate_limit")
@patch("app.handlers.comments.service.check_command_permission")
@patch("app.handlers.comments.service._dispatch")
@patch("app.handlers.comments.service._post_comment")
def test_service_handler(mock_post, mock_dispatch, mock_perm, mock_limit, mock_config, mock_token):
    mock_token.return_value = "token"
    mock_limit.return_value = True
    mock_perm.return_value = (True, "allowed")
    mock_dispatch.return_value = "Done"

    payload = {
        "action": "created",
        "comment": {"body": "/fix error"},
        "issue": {"number": 1, "title": "t", "body": "b"},
        "repository": {"full_name": "repo"},
        "installation": {"id": 123},
        "sender": {"login": "user"}
    }
    S.handle_comment_event(payload)
    mock_post.assert_called()


# --- Additional coverage tests for core and security modules ---

import app.core.metrics as metrics_mod
import app.core.thread_pool as tp_mod
import app.security.licenses as lic_mod
import app.core.health_check as hc_mod

def test_metrics_collector():
    collector = metrics_mod.MetricsCollector()
    collector.increment("test.counter", 2)
    assert collector.get("test.counter") == 2
    assert collector.get("nonexistent") == 0

    snap = collector.snapshot()
    assert snap["test.counter"] == 2
    assert "uptime_seconds" in snap
    assert "uptime_human" in snap

    collector.reset()
    assert collector.get("test.counter") == 0

def test_thread_pool():
    # Test pool initialization and config
    pool = tp_mod.get_pool()
    assert pool is not None

    # Test pool stats
    stats = tp_mod.pool_stats()
    assert stats["max_workers"] == tp_mod.MAX_DISPATCH_WORKERS
    assert stats["queue_capacity"] == 50

    # Test bounded dispatch
    called = []
    def sample_task():
        called.append(True)

    future = tp_mod.dispatch(sample_task)
    assert not tp_mod.is_saturated(future)
    future.result() # Wait for task
    assert called == [True]

    # Test saturation sentinel
    assert tp_mod.is_saturated(tp_mod._SATURATED)

    # Test shutdown
    tp_mod.shutdown(wait=True)
    assert tp_mod._pool is None

@patch("requests.get")
def test_licenses_scan(mock_get):
    # Mock PyPI API return
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"info": {"license": "GPL-3.0 License"}}
    mock_get.return_value = mock_resp

    res = lic_mod.check_package_license("gpl-pkg")
    assert res["package"] == "gpl-pkg"
    assert res["risk"] == "copyleft"

    # Scan requirements.txt content
    findings = lic_mod.scan_requirements("gpl-pkg==1.0")
    assert len(findings) == 1
    assert findings[0]["package"] == "gpl-pkg"

    # Format findings
    formatted = lic_mod.format_findings(findings)
    assert "gpl-pkg" in formatted
    assert "copyleft" in formatted

    # Non-copyleft case
    mock_resp.json.return_value = {"info": {"license": "MIT"}}
    res_mit = lic_mod.check_package_license("mit-pkg")
    assert res_mit["risk"] == "safe"

    # Empty findings formatting
    empty_formatted = lic_mod.format_findings([])
    assert "permissive" in empty_formatted

@patch("app.ai.circuit_breaker.status_all")
@patch("app.github.rate_limit.get_status")
@patch("app.core.redis_client.is_redis_available")
@patch("app.core.redis_client.get_redis")
def test_system_health(mock_get_redis, mock_redis_avail, mock_gh_status, mock_breakers):
    # Set mock configurations
    mock_redis_avail.return_value = True
    mock_gh_status.return_value = {"remaining": 5000, "resets_in": 3600}
    mock_breakers.return_value = {
        "groq": {"state": "closed"},
        "gemini": {"state": "open"} # degraded
    }

    # Mock redis instance for record_latency
    mock_redis = MagicMock()
    mock_redis.get.return_value = None
    mock_get_redis.return_value = mock_redis

    # Record latency
    hc_mod.record_latency("groq", 150)
    mock_redis.set.assert_called()

    # Get system health
    health = hc_mod.get_system_health()
    assert health["status"] == "partial"
    assert health["is_degraded"] is True
    assert health["providers"]["gemini"]["is_degraded"] is True
    assert health["providers"]["groq"]["is_degraded"] is False

    # Get degraded message
    msg = hc_mod.get_degraded_message()
    assert "gemini" in msg

