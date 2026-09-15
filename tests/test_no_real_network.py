"""
tests/test_no_real_network.py

The unit suite must not reach the internet, and the guard that enforces it
must not quietly stop working.

This started as one instance: the provider catalogue reached openrouter.ai for
real, and CI went red on a test that only passed locally because the sandbox
blocked that host. That was fixed by mocking the catalogue — the instance, not
the class. A second instance was already present and unnoticed: `/secfull`
walks requirements.txt through app/security/licenses.py, which asks PyPI about
every package. Six real connections per suite run.

Worse than slow: in an environment that blocks egress, those calls fail, the
licence checker catches the failure by design, and the test passes anyway —
exercising the error path while claiming to test the happy one. Passing for
the wrong reason is the failure mode a network guard exists to remove.

The guard lives in tests/conftest.py as an autouse fixture. A guard nobody
tests is a guard that can be deleted or broken without anyone noticing, so it
is tested here.
"""

import socket

import pytest


class TestTheGuardIsActive:
    def test_an_outbound_connection_is_refused(self):
        with pytest.raises(RuntimeError, match="real connection"):
            socket.create_connection(("pypi.org", 443), timeout=1)

    def test_the_error_names_the_host_and_says_what_to_do(self):
        with pytest.raises(RuntimeError) as exc:
            socket.create_connection(("api.groq.com", 443), timeout=1)
        message = str(exc.value)
        assert "api.groq.com" in message, "the message must name the host"
        assert "Mock the client" in message
        assert "integration" in message, "it must say how to opt out legitimately"

    def test_a_raw_socket_connect_is_also_refused(self):
        """create_connection is not the only route to the network."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(RuntimeError):
                s.connect(("1.1.1.1", 443))
        finally:
            s.close()


class TestLoopbackStillWorks:
    """Blocking everything would break the in-process fakes and the Flask test
    client, and a guard that breaks the suite gets deleted within a day."""

    def test_localhost_is_allowed(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            client = socket.create_connection(("127.0.0.1", port), timeout=2)
            client.close()
        finally:
            server.close()


class TestTheKnownOffendersStayOffline:
    """The two paths that actually reached out. Both are exercised through
    their real entry point rather than by patching, so a regression that
    reintroduces the call fails here."""

    def test_the_licence_scanner_does_not_call_pypi(self):
        from app.security.licenses import scan_requirements

        # Reaching PyPI would raise inside check_package_license, which catches
        # it and reports "unchecked" — which scan_requirements then drops. A
        # clean empty result is what offline looks like.
        assert scan_requirements("requests==2.32.0\nflask==3.1.3\n") == []

    def test_the_provider_catalogue_does_not_call_openrouter(self):
        from app.ai.model_catalog import available_models

        assert available_models("openrouter") == []
