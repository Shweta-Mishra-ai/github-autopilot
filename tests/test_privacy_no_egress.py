"""
tests/test_privacy_no_egress.py

The one claim this project leads with, verified by watching sockets.

The README's first line is "AI code review that never sends your code
anywhere." Everything else it says is a feature; this is a promise, and it is
the only reason to choose this over a hosted reviewer. A promise about where
data goes cannot be tested by mocking the thing that would send it — mock the
provider and you have proved the mock does not call out.

So these tests record every address the process attempts, and assert that with
LLM_LOCAL_ONLY=1 the only one is loopback. If a future refactor adds a cloud
fallback to any path, the recording shows it.

The failure mode being guarded against is specific: Ollama goes down, and the
router "helpfully" falls back to a cloud provider. The code on that machine
then leaves the building, silently, because the bot still answered.
"""

from __future__ import annotations

import http.client
import socket
from unittest.mock import patch
from urllib.parse import urlparse

import pytest

from app.ai.circuit_breaker import AllProvidersDown

PROXY_VARS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "ftp_proxy",
)


@pytest.fixture
def watch_egress(monkeypatch):
    """Record every address attempted. Loopback still connects.

    Watching sockets alone is not enough, and the first version of this file
    was wrong about that. With HTTPS_PROXY set, every outbound request has a
    socket destination of the proxy — 127.0.0.1 in this sandbox and in most
    corporate runners — so a socket-only watcher records loopback, concludes
    nothing left the machine, and passes while the code is on its way to a
    third party. The test then asserts the opposite of what it claims.

    The real destination is therefore taken from the three places it survives
    proxying: the hostname that reappears in DNS once the proxy environment is
    removed, the CONNECT target of a tunnel, and the absolute-URI request line
    that plain http:// uses instead of a tunnel.
    """
    attempted: list[str] = []
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo
    real_set_tunnel = http.client.HTTPConnection.set_tunnel
    real_putrequest = http.client.HTTPConnection.putrequest

    for var in PROXY_VARS:
        monkeypatch.delenv(var, raising=False)

    def note(host):
        attempted.append(str(host))

    def guard_connect(self, address, *a, **kw):
        host = address[0] if isinstance(address, tuple) and address else address
        note(host)
        if isinstance(host, str) and host.startswith("127."):
            return real_connect(self, address, *a, **kw)
        raise OSError("blocked by the egress watcher")

    def guard_getaddrinfo(host, *a, **kw):
        note(host)
        if isinstance(host, str) and host.startswith(("127.", "localhost")):
            return real_getaddrinfo(host, *a, **kw)
        raise OSError("blocked by the egress watcher")

    def guard_set_tunnel(self, host, port=None, headers=None):
        note(host)
        raise OSError("blocked by the egress watcher")

    def guard_putrequest(self, method, url, *a, **kw):
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            host = urlparse(url).hostname
            if host:
                note(host)
                if not host.startswith("127."):
                    raise OSError("blocked by the egress watcher")
        return real_putrequest(self, method, url, *a, **kw)

    monkeypatch.setattr(socket.socket, "connect", guard_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guard_getaddrinfo)
    monkeypatch.setattr(http.client.HTTPConnection, "set_tunnel", guard_set_tunnel)
    monkeypatch.setattr(http.client.HTTPConnection, "putrequest", guard_putrequest)
    yield attempted


@pytest.fixture
def local_only(monkeypatch):
    """LLM_LOCAL_ONLY, with every cloud credential present and an Ollama that
    is not answering — the exact situation a fallback would trigger in."""
    for key, value in {
        "GROQ_API_KEY": "gsk_present_and_tempting",
        "GEMINI_API_KEY": "present_and_tempting",
        "OPENROUTER_API_KEY": "present_and_tempting",
        "LLM_LOCAL_ONLY": "1",
        # A port nothing listens on: reachable host, refused connection.
        "OLLAMA_HOST": "http://127.0.0.1:59999",
        "OLLAMA_MODEL": "llama3.1:8b",
    }.items():
        monkeypatch.setenv(key, value)

    from app.ai.circuit_breaker import get_breaker

    for provider in ("ollama", "groq_70b", "groq_8b", "gemini", "openrouter"):
        try:
            get_breaker(provider).reset()
        except Exception:
            pass

    from app.ai.router import LLMRouter

    return LLMRouter()


def _cloud(attempted: list[str]) -> list[str]:
    return sorted({h for h in attempted if not h.startswith(("127.", "localhost", "::1"))})


class TestNothingLeavesTheMachine:
    def test_ask_fails_closed_and_contacts_no_cloud_host(self, local_only, watch_egress):
        """Ollama is down and every cloud key is set. The bot must fail, not
        quietly succeed somewhere else."""
        with pytest.raises(AllProvidersDown):
            local_only.ask("system", "proprietary source code", task="code_review")

        assert _cloud(watch_egress) == [], (
            f"code left the machine: {_cloud(watch_egress)}"
        )

    def test_safe_ask_degrades_without_contacting_a_cloud_host(self, local_only, watch_egress):
        """safe_ask never raises, which makes it the likelier place for a
        well-meaning fallback to be added."""
        result, meta = local_only.safe_ask("system", "proprietary code", task="code_review")

        assert result.get("_providers_down") is True
        assert meta is None
        assert _cloud(watch_egress) == []

    def test_ask_text_contacts_no_cloud_host(self, local_only, watch_egress):
        with pytest.raises(AllProvidersDown):
            local_only.ask_text("system", "proprietary code", task="explain")
        assert _cloud(watch_egress) == []

    def test_no_task_type_finds_a_cloud_route(self, local_only, watch_egress):
        """Selection branches on task type — fast, standard, deep, long — and
        each branch has its own provider order. One of them forgetting the
        local-only guard is exactly how this breaks."""
        for task in ("code_review", "fix_command", "explain", "labeling", "summarize"):
            with pytest.raises(AllProvidersDown):
                local_only.ask("system", "proprietary code", task=task)
        assert _cloud(watch_egress) == []


class TestTheGuaranteeIsStructural:
    def test_the_fallback_list_contains_only_ollama(self, local_only):
        """Belt and braces: even if a provider were reachable, the fallback
        chain must not offer a cloud one."""
        candidates = local_only._fallback_candidates("code_review")
        names = [type(p).__name__ for p in candidates if p is not None]
        assert names in ([], ["OllamaProvider"]), f"cloud provider in the chain: {names}"

    def test_selection_raises_rather_than_returning_a_cloud_provider(self, local_only):
        with patch.object(local_only, "_get_ollama", return_value=None):
            with pytest.raises(AllProvidersDown):
                local_only._select_provider("code_review")

    def test_cost_is_zero_in_local_mode(self, local_only):
        """The README states cost_usd is always 0 here. A non-zero cost would
        mean something billable was called."""
        from app.ai.routing_policy import COST_PER_1K

        assert COST_PER_1K.get("ollama", 0) == 0


class TestTheWatcherCanActuallySee:
    """The control group, and the reason this class exists at all.

    Every assertion above is `_cloud(attempted) == []`, which is also what a
    watcher that observes nothing returns. The first version of this file
    passed for exactly that reason: it watched sockets, the sandbox routes
    everything through a proxy on 127.0.0.1, and so a cloud call looked local.
    Eight green tests, zero of them checking anything.

    A negative assertion needs a positive control. This one turns the guarantee
    off and requires the watcher to notice — if it cannot see egress when egress
    is certain, the tests above are decoration and this fails instead.
    """

    def test_the_watcher_records_a_cloud_host_when_the_flag_is_off(
        self, local_only, watch_egress, monkeypatch
    ):
        monkeypatch.delenv("LLM_LOCAL_ONLY", raising=False)

        from app.ai.router import LLMRouter

        router = LLMRouter()
        try:
            router.ask("system", "code", task="code_review")
        except Exception:
            pass  # blocked by the watcher, which is the point

        assert _cloud(watch_egress), (
            "the egress watcher saw nothing while a cloud provider was being "
            "called. It is blind — most likely a proxy it does not know about "
            "— so every 'nothing left the machine' assertion in this file is "
            "currently vacuous."
        )


class TestTheDefaultIsStillCloud:
    """The guarantee is opt-in. If local-only were the default, every existing
    deployment would silently stop working — so the inverse is tested too."""

    def test_without_the_flag_cloud_providers_are_available(self, monkeypatch):
        monkeypatch.delenv("LLM_LOCAL_ONLY", raising=False)
        monkeypatch.delenv("LLM_PREFER_LOCAL", raising=False)
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test")

        from app.ai.router import LLMRouter

        names = [type(p).__name__ for p in LLMRouter()._fallback_candidates("code_review") if p]
        assert any("Groq" in n for n in names), names
