"""
tests/test_setup_flow.py

One-click App creation, and the doctor that explains what is still wrong.

The old quickstart asked an operator to create a GitHub App by hand: webhook
URL, generated secret, four permission groups, four event subscriptions, a
downloaded .pem. Getting the permissions wrong there is what produces the
failure this project spent a release fixing — seven commands refusing to run
and unable to say why.

Two properties carry this feature and are what these tests defend:

  * The manifest asks for exactly what the code calls. Asking for less breaks
    commands silently; asking for more is a permission grab a reader is right
    to refuse.
  * A page that renders a private key must be unreplayable and unlogged. The
    callback is single-use, and nothing about it reaches the logs.
"""

from __future__ import annotations

import json
import logging
import re

import pytest

from app.setup_flow import (
    build_manifest,
    consume_state,
    credentials_page,
    new_state,
    setup_page,
)

BASE = "https://autopilot.example.com"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("EVENT_QUEUE_CONSUMERS", "0")
    import server

    return server.app.test_client()


class TestTheManifestMatchesWhatTheCodeCalls:
    def test_it_requests_every_permission_the_handlers_need(self):
        perms = build_manifest(BASE)["default_permissions"]
        # Derived from the endpoints in app/: issues, pulls, contents, actions,
        # checks, code-scanning/dependabot alerts, and repo metadata.
        assert perms["issues"] == "write"
        assert perms["pull_requests"] == "write"
        assert perms["contents"] == "write"
        assert perms["actions"] == "write"
        assert perms["metadata"] == "read"
        assert perms["security_events"] == "read"

    def test_it_asks_for_nothing_beyond_that(self):
        """A permission the code never exercises is one the operator is right
        to refuse, and one more thing to justify."""
        allowed = {
            "metadata", "issues", "pull_requests", "contents",
            "actions", "checks", "security_events",
        }
        assert set(build_manifest(BASE)["default_permissions"]) <= allowed

    def test_it_subscribes_to_exactly_the_handled_events(self):
        """An event with no handler is delivered, dropped, and billed."""
        events = set(build_manifest(BASE)["default_events"])
        assert events == {"push", "pull_request", "issues", "issue_comment"}

    def test_the_webhook_and_callback_point_at_this_deployment(self):
        m = build_manifest(BASE)
        assert m["hook_attributes"]["url"] == f"{BASE}/webhook"
        assert m["redirect_url"] == f"{BASE}/setup/callback"

    def test_the_app_is_private_by_default(self):
        """A public App can be installed by strangers on their own repos, which
        points their traffic at someone else's deployment and quota."""
        assert build_manifest(BASE)["public"] is False

    def test_the_page_embeds_valid_manifest_json(self):
        """GitHub parses this attribute, so a quoting mistake here is a broken
        button rather than a visible error."""
        import html as html_mod

        page = setup_page(BASE)
        raw = re.search(r"""name=["']manifest["']\s+value='([^']+)'""", page)
        assert raw, "manifest field missing from the form"

        parsed = json.loads(html_mod.unescape(raw.group(1)))
        assert parsed["hook_attributes"]["url"].endswith("/webhook")
        assert parsed["redirect_url"].endswith("/setup/callback")

    def test_the_manifest_attribute_is_quote_safe(self):
        """The JSON is embedded in a single-quoted attribute. An unescaped
        quote would truncate it and GitHub would receive a fragment."""
        page = setup_page(BASE)
        raw = re.search(r"""name=["']manifest["']\s+value='([^']+)'""", page)
        assert '"' not in raw.group(1), "raw double quote survived into the attribute"
        assert "'" not in raw.group(1), "raw single quote would close the attribute"


class TestTheCallbackCannotBeReplayed:
    def test_a_state_works_exactly_once(self):
        s = new_state()
        assert consume_state(s) is True
        assert consume_state(s) is False

    def test_a_state_this_server_never_issued_is_rejected(self):
        assert consume_state("forged-value") is False

    def test_the_endpoint_refuses_a_forged_state(self, client):
        assert client.get("/setup/callback?code=abc&state=forged").status_code == 400

    def test_the_endpoint_refuses_a_missing_code(self, client):
        assert client.get("/setup/callback").status_code == 400

    def test_state_storage_is_bounded(self):
        """An unbounded set of tokens is a memory leak reachable by anyone who
        can load a public page."""
        from app.setup_flow import _MAX_STATES, _STATES

        for _ in range(_MAX_STATES * 3):
            new_state()
        assert len(_STATES) <= _MAX_STATES


class TestCredentialsAreHandledLikeCredentials:
    SAMPLE = {
        "id": 12345,
        "slug": "my-autopilot",
        "webhook_secret": "s3cr3t-webhook-value",
        "pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
    }

    def test_the_setup_page_itself_contains_no_secret(self, client):
        body = client.get("/setup").get_data(as_text=True)
        assert "webhook_secret" not in body
        assert "PRIVATE KEY" not in body

    def test_the_credentials_page_says_it_is_shown_once(self):
        page = credentials_page(self.SAMPLE, BASE)
        assert "once" in page.lower()
        assert "not stored" in page.lower()

    def test_the_credentials_page_carries_all_three_values(self):
        page = credentials_page(self.SAMPLE, BASE)
        assert "12345" in page
        assert "s3cr3t-webhook-value" in page
        assert "BEGIN RSA PRIVATE KEY" in page

    def test_nothing_secret_reaches_the_logs(self, client, monkeypatch, caplog):
        """A private key in a log file is a private key in every log
        aggregator, backup and support ticket downstream of it."""
        from app import setup_flow

        state = new_state()
        monkeypatch.setattr(setup_flow, "exchange_code", lambda code: (self.SAMPLE, ""))

        with caplog.at_level(logging.DEBUG):
            resp = client.get(f"/setup/callback?code=abc&state={state}")

        assert resp.status_code == 200
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "BEGIN RSA PRIVATE KEY" not in logged
        assert self.SAMPLE["webhook_secret"] not in logged

    def test_the_credentials_response_is_not_cacheable(self, client, monkeypatch):
        from app import setup_flow

        state = new_state()
        monkeypatch.setattr(setup_flow, "exchange_code", lambda code: (self.SAMPLE, ""))
        resp = client.get(f"/setup/callback?code=abc&state={state}")
        assert "no-store" in resp.headers.get("Cache-Control", "")

    def test_a_failed_exchange_does_not_render_a_page(self, client, monkeypatch):
        from app import setup_flow

        state = new_state()
        monkeypatch.setattr(setup_flow, "exchange_code", lambda code: ({}, "code expired"))
        resp = client.get(f"/setup/callback?code=abc&state={state}")
        assert resp.status_code == 502
        assert "code expired" in resp.get_data(as_text=True)


class TestTheDoctorReportsEvidence:
    def test_it_is_auth_gated(self, client, monkeypatch):
        """It names repositories and reports what the App may do."""
        import server

        monkeypatch.setattr(server, "METRICS_TOKEN", "tok")
        assert client.get("/setup/doctor?repo=o/r&installation_id=1").status_code == 401

    def test_it_explains_what_it_needs(self, client):
        body = client.get("/setup/doctor").get_json()
        assert "usage" in body and "installation_id" in body["usage"]
        assert "hint" in body

    @pytest.mark.parametrize(
        "query", ["repo=notarepo&installation_id=1", "repo=o/r&installation_id=abc", "repo=o/r"]
    )
    def test_it_rejects_malformed_input(self, client, query):
        assert client.get(f"/setup/doctor?{query}").status_code == 400

    def test_a_broken_capability_names_the_commands_it_breaks(self):
        """The whole point: not "denied", but "these seven stop working, and
        here is the status GitHub returned"."""
        from app.core.preflight import Diagnosis, ProbeResult, format_report

        d = Diagnosis(repo="o/r", installation_id=1, granted={"issues": "write"})
        d.probes = [
            ProbeResult(
                "collaborator permission", False, 403, "Forbidden: Resource not accessible",
                ("/merge", "/apply", "/autofix"), True,
            ),
            ProbeResult("issues", True, 200, "ok", ("triage",), True),
        ]
        report = format_report(d)

        assert "Setup is incomplete" in report
        assert "/merge" in report and "/apply" in report
        assert "403" in report
        assert "Resource not accessible" in report
        assert d.healthy is False

    def test_an_optional_capability_does_not_mark_setup_broken(self):
        """Code scanning is not enabled on every repo; reporting that as a
        setup failure would be a false alarm on most of them."""
        from app.core.preflight import Diagnosis, ProbeResult

        d = Diagnosis(repo="o/r", installation_id=1)
        d.probes = [ProbeResult("code scanning", False, 404, "not enabled", ("x",), False)]
        assert d.healthy is True

    def test_probes_are_read_only(self):
        """Running a diagnostic must never change anything."""
        import inspect

        from app.core import preflight

        src = inspect.getsource(preflight)
        for writer in ("gh_post", "gh_put", "gh_patch", "gh_delete"):
            assert writer not in src, f"preflight calls {writer}"

    def test_every_capability_names_what_it_enables(self):
        """A probe result nobody can act on is noise."""
        from app.core.preflight import CAPABILITIES

        for cap in CAPABILITIES:
            assert cap.enables, f"{cap.name} does not say what it enables"
            assert "{repo}" in cap.path
