"""
Deployment settings the operator was never told about.

Three settings are silently optional. Unset, each one degrades the bot in a
way that produces no error: rate limiting stops being per-client, everything
the bot learns is lost on restart, and every trivial event is billed to a
hosted model. Nothing surfaced any of it.

The X-Forwarded-For check is the one with teeth. Getting TRUSTED_PROXY_HOPS
wrong is silent in BOTH directions — too high and every request falls back to
one shared bucket, too low and the entry being trusted was written by the
client. It was reported at DEBUG level, which nobody reads.
"""

import pytest

from app.core import webhook_security as ws
from app.core.preflight import format_environment_report, inspect_environment


@pytest.fixture(autouse=True)
def _clean_observations():
    ws._chain_observations[:] = [0] * len(ws._chain_observations)
    yield
    ws._chain_observations[:] = [0] * len(ws._chain_observations)


def _observe(chain_lengths):
    for n in chain_lengths:
        ws._record_chain_length(n)


class TestProxyVerdict:
    def test_no_traffic_yet_is_unknown_not_a_guess(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
        verdict, detail = ws.proxy_configuration_verdict()
        assert verdict == "unknown"
        assert "no request" in detail

    def test_configured_hops_matching_traffic_is_ok(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
        _observe([1] * 50)
        verdict, _ = ws.proxy_configuration_verdict()
        assert verdict == "ok"

    def test_hops_too_high_for_the_traffic_warns(self, monkeypatch):
        # Every request falls back to remote_addr, so the whole deployment
        # shares one rate-limit bucket and nothing said so.
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "2")
        _observe([1] * 50)
        verdict, detail = ws.proxy_configuration_verdict()
        assert verdict == "warn"
        assert "fell back to remote_addr" in detail

    def test_more_proxies_than_configured_warns(self, monkeypatch):
        # The security-relevant direction: if only 1 proxy appends, the extra
        # entries came from the client and the trusted one is attacker-chosen.
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
        _observe([3] * 50)
        verdict, detail = ws.proxy_configuration_verdict()
        assert verdict == "warn"
        assert "attacker-chosen" in detail

    def test_zero_hops_with_a_proxy_present_warns(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "0")
        _observe([1] * 50)
        verdict, detail = ws.proxy_configuration_verdict()
        assert verdict == "warn"
        assert "one rate-limit bucket" in detail

    def test_zero_hops_with_no_proxy_is_ok(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "0")
        _observe([0] * 50)
        verdict, _ = ws.proxy_configuration_verdict()
        assert verdict == "ok"

    def test_observations_are_recorded_from_real_requests(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")

        class _Req:
            remote_addr = "10.0.0.1"
            headers = {"X-Forwarded-For": "1.2.3.4, 10.0.0.9"}

        ws._get_client_ip(_Req())
        assert ws.forwarded_chain_observations() == {2: 1}

    def test_a_chain_longer_than_the_ceiling_is_still_counted(self):
        _observe([99])
        assert ws.forwarded_chain_observations() == {ws._MAX_TRACKED_CHAIN: 1}


class TestEnvironmentFindings:
    def _find(self, name):
        return next(f for f in inspect_environment() if f.name == name)

    def test_memory_backup_fully_unset_is_reported_off(self, monkeypatch):
        for var in ("MEMORY_BACKUP_KEY", "MEMORY_BACKUP_REPO", "MEMORY_BACKUP_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        finding = self._find("Memory backup")
        assert finding.state == "off"
        assert "lost when the instance restarts" in finding.detail

    def test_partial_memory_backup_config_is_a_warning(self, monkeypatch):
        # The dangerous state: looks configured, keeps nothing.
        monkeypatch.setenv("MEMORY_BACKUP_KEY", "k")
        monkeypatch.delenv("MEMORY_BACKUP_REPO", raising=False)
        monkeypatch.delenv("MEMORY_BACKUP_TOKEN", raising=False)
        finding = self._find("Memory backup")
        assert finding.state == "warn"
        assert "MEMORY_BACKUP_REPO" in finding.detail
        assert "MEMORY_BACKUP_TOKEN" in finding.detail

    def test_fully_configured_memory_backup_is_ok(self, monkeypatch):
        for var in ("MEMORY_BACKUP_KEY", "MEMORY_BACKUP_REPO", "MEMORY_BACKUP_TOKEN"):
            monkeypatch.setenv(var, "x")
        assert self._find("Memory backup").state == "ok"

    def test_ollama_unset_is_off_not_broken(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        finding = self._find("Local triage gate")
        assert finding.state == "off"
        assert "Valid, and the default" in finding.detail

    def test_no_secret_value_is_ever_reported(self, monkeypatch):
        secret = "s3cr3t-value-that-must-not-leak"
        for var in ("MEMORY_BACKUP_KEY", "MEMORY_BACKUP_REPO", "MEMORY_BACKUP_TOKEN"):
            monkeypatch.setenv(var, secret)
        monkeypatch.setenv("OLLAMA_HOST", secret)
        rendered = format_environment_report(inspect_environment())
        assert secret not in rendered
        for finding in inspect_environment():
            assert secret not in finding.detail

    def test_inspection_never_raises(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "not-a-number")
        assert inspect_environment()
        assert format_environment_report(inspect_environment())


class TestDoctorEndpoint:
    def _auth(self, server):
        return {"Authorization": f"Bearer {server.METRICS_TOKEN}"} if server.METRICS_TOKEN else {}

    def test_settings_are_returned_without_a_repo(self):
        # A diagnostic that requires the working configuration you are trying
        # to diagnose is not much of a diagnostic.
        import server

        client = server.app.test_client()
        r = client.get("/setup/doctor", headers=self._auth(server))
        assert r.status_code == 400
        assert r.get_json()["environment"], "settings must be reported without a repo"
        assert "Deployment settings" in r.get_json()["report_markdown"]

    def test_still_auth_gated(self):
        import server

        if not server.METRICS_TOKEN:
            pytest.skip("auth gate open when METRICS_AUTH_TOKEN is unset")
        r = server.app.test_client().get("/setup/doctor")
        assert r.status_code == 401
