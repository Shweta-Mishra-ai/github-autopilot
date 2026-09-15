"""
tests/test_docker_compose.py

docker-compose.yml is the only way to run this project entirely on your own
hardware, which is the thing the README leads with. It had drifted out of
agreement with the application in three ways at once, none of them visible
without running it:

  - OLLAMA_HOST, OLLAMA_MODEL and LLM_LOCAL_ONLY were not passed into the
    containers, so the private path could not be reached through the project's
    own compose file no matter what you put in .env;
  - MCP_API_KEY and METRICS_AUTH_TOKEN were not passed either, and both
    endpoints fail closed without them, so the plugin, the editor integration,
    /health and /setup/doctor were all unusable against a compose deployment;
  - web and worker both started queue consumers, and both therefore ran
    recover_stale() at boot — the duplicate-processing configuration that
    worker.py's own docstring says to avoid.

Configuration has no type checker. These assertions are the only thing
standing between the next edit and the same class of silent breakage.
"""

from __future__ import annotations

import os

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPOSE_PATH = os.path.join(ROOT, "docker-compose.yml")

# The services that run application code, as opposed to redis and ollama.
APP_SERVICES = ("web", "worker")


@pytest.fixture(scope="module")
def compose() -> dict:
    with open(COMPOSE_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def env_of(compose: dict, service: str) -> dict:
    """Compose accepts `environment` as a mapping or as a list of KEY=value
    strings. Both are valid, so normalise rather than assume — a test that
    raises AttributeError on the other form reports a parsing accident where
    the reader expects a finding about the deployment."""
    raw = compose["services"][service].get("environment", {})
    if isinstance(raw, dict):
        return dict(raw)
    normalised = {}
    for item in raw:
        key, _, value = str(item).partition("=")
        normalised[key] = value
    return normalised


class TestThePrivatePathIsReachable:
    """The headline claim, expressed as configuration."""

    @pytest.mark.parametrize("service", APP_SERVICES)
    @pytest.mark.parametrize("var", ["OLLAMA_HOST", "OLLAMA_MODEL", "LLM_LOCAL_ONLY"])
    def test_local_model_variables_reach_the_containers(self, compose, service, var):
        assert var in env_of(compose, service), (
            f"{var} is not passed to `{service}`. Setting it in .env will do "
            "nothing, and the local-only path this project is built around "
            "cannot be reached through compose at all."
        )

    def test_an_ollama_service_is_available(self, compose):
        assert "ollama" in compose["services"], (
            "no ollama service — running privately then requires the user to "
            "install and wire it themselves, which is not the one command the "
            "README offers."
        )

    def test_ollama_is_profile_gated(self, compose):
        """A ~2GB image must not be pulled by someone who wanted the cloud path."""
        assert compose["services"]["ollama"].get("profiles"), (
            "the ollama service has no `profiles`, so a plain `docker compose "
            "up` starts it and pulls the image for everyone."
        )

    def test_nothing_else_is_profile_gated(self, compose):
        """The default `up` must still be a complete, working deployment."""
        for name, svc in compose["services"].items():
            if name == "ollama":
                continue
            assert not svc.get("profiles"), f"{name} would not start on a plain `up`"


class TestFailClosedEndpointsAreConfigurable:
    @pytest.mark.parametrize("service", APP_SERVICES)
    @pytest.mark.parametrize("var", ["MCP_API_KEY", "METRICS_AUTH_TOKEN"])
    def test_the_keys_that_gate_whole_endpoints_are_passed(self, compose, service, var):
        assert var in env_of(compose, service), (
            f"{var} is not passed to `{service}`. This does not degrade a "
            "feature, it removes one: the endpoint it guards fails closed."
        )


class TestOnlyOneProcessRecoversTheQueue:
    """recover_stale() assumes nothing is legitimately in flight. Two processes
    running it at boot means one requeues what the other is handling."""

    def test_web_does_not_start_consumers(self, compose):
        value = env_of(compose, "web").get("EVENT_QUEUE_CONSUMERS")
        assert str(value) == "0", (
            "the web service starts queue consumers while a dedicated worker "
            f"is also running (EVENT_QUEUE_CONSUMERS={value!r}). Both call "
            "recover_stale() at boot, so an event the worker is processing "
            "right now can be requeued and handled twice."
        )

    def test_the_worker_does_start_consumers(self, compose):
        value = str(env_of(compose, "worker").get("EVENT_QUEUE_CONSUMERS", ""))
        assert value != "0", "worker.py exits(1) with no consumers — it would have nothing to do"


class TestTheTwoAppServicesDoNotDrift:
    def test_web_and_worker_receive_the_same_credentials(self, compose):
        """They run the same code against the same GitHub App. A variable
        present in one and missing from the other is the bug this file was
        written about, and it is invisible until a command fails in production.
        """
        web = set(env_of(compose, "web"))
        worker = set(env_of(compose, "worker"))
        # Deliberately different: it is what makes only one of them recover.
        expected_difference = set()
        assert (web ^ worker) == expected_difference, (
            f"only in web: {sorted(web - worker)}; only in worker: {sorted(worker - web)}"
        )


class TestRedisMayNotForgetWhatItHasDone:
    def test_eviction_is_disabled(self, compose):
        """Matching render.yaml. Under the default allkeys-lru, Redis discards
        idempotency keys when memory fills, and the next redelivery of an
        already-handled event is handled again."""
        command = compose["services"]["redis"].get("command", "")
        text = " ".join(command) if isinstance(command, list) else str(command)
        assert "noeviction" in text, (
            "redis runs with the default eviction policy, so idempotency keys "
            "can be dropped under memory pressure and webhooks double-handled"
        )

    def test_the_app_waits_for_redis(self, compose):
        """Without Redis at boot the app falls back to in-memory state, which
        loses durability and idempotency silently — the failure this project
        cares most about."""
        for service in APP_SERVICES:
            depends = compose["services"][service].get("depends_on", {})
            assert isinstance(depends, dict) and "redis" in depends, (
                f"{service} does not wait for redis"
            )
            assert depends["redis"].get("condition") == "service_healthy", (
                f"{service} starts as soon as the redis container exists, not "
                "when it is answering — so an unlucky boot silently degrades "
                "to the in-memory fallback"
            )
