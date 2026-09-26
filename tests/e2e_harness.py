"""
tests/e2e_harness.py — the real webhook pipeline, end to end, over real HTTP.

Not a test module (pytest collects test_*.py only); test_e2e_webhook_pipeline.py
drives it. Kept separate so the harness can also be run by hand while debugging.

What is real: gunicorn serving server:app with the Procfile's own flags, the
Redis event queue and its in-process consumers, HMAC webhook verification, the
GitHub App JWT and token exchange, every GitHub client call, the LLM router, the
Ollama provider and its HTTP call, the gatekeeper, the validators, the gate, and
every handler between them.

What is fake: the two remote services. A local GitHub API and a local Ollama
answer on 127.0.0.1. The only intervention inside the server process is the
socket destination for api.github.com, rewritten at the requests transport layer
(see LAUNCHER) — so URL building, host validation, headers, auth and response
handling all run unmodified. auth.py hardcodes its own api.github.com URL
separately from client.GITHUB_API, which is why patching that constant would
not have been enough.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
WEBHOOK_SECRET = "e2e-webhook-secret-32-characters!!"

LAUNCHER = """
import os
import requests.adapters
from urllib.parse import urlsplit, urlunsplit

_FAKE = urlsplit(os.environ["E2E_GITHUB_FAKE"])
_real_send = requests.adapters.HTTPAdapter.send


def _send(self, request, **kwargs):
    parts = urlsplit(request.url)
    if parts.hostname == "api.github.com":
        request.url = urlunsplit((_FAKE.scheme, _FAKE.netloc, parts.path, parts.query, ""))
    return _real_send(self, request, **kwargs)


requests.adapters.HTTPAdapter.send = _send

from server import app  # noqa: E402,F401
"""


def redis_command(url: str, *command: str, timeout: float = 2.0) -> str:
    """
    Run one Redis command over a raw socket and return the first reply line.

    Deliberately not the `redis` package: conftest swaps that module out of
    sys.modules for the unit suite, and this harness should not depend on how.
    Handles `redis://host:port/db` and selects the db first.
    """
    parts = urlsplit(url)
    db = (parts.path or "/0").lstrip("/") or "0"
    with socket.create_connection(
        (parts.hostname or "127.0.0.1", parts.port or 6379), timeout
    ) as s:
        s.sendall(f"SELECT {db}\r\n{' '.join(command)}\r\n".encode())
        data = b""
        while data.count(b"\r\n") < 2:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    return data.decode(errors="replace").split("\r\n")[1]


def redis_reachable(url: str) -> bool:
    if not url:
        return False
    try:
        return redis_command(url, "PING") == "+PONG"
    except OSError:
        return False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def rsa_private_key_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


# ── Fake GitHub ──────────────────────────────────────────────────────────────


class FakeGitHub:
    """
    Records every request. Routes are (METHOD, path-without-query) -> handler,
    where a handler returns (status, body) or raises to drop the connection.
    Unknown routes answer 404, which is what GitHub does, and are recorded so a
    test can see what the pipeline asked for that nothing expected.
    """

    def __init__(self):
        self.requests: list[dict] = []
        self.routes: dict = {}
        self.lock = threading.Lock()
        self._server = None

    def route(self, method: str, path: str, handler):
        self.routes[(method, path)] = handler

    def writes(self, method=None, path_contains=""):
        with self.lock:
            return [
                r
                for r in self.requests
                if r["method"]
                in (("POST", "PATCH", "PUT", "DELETE") if method is None else (method,))
                and path_contains in r["path"]
            ]

    def start(self) -> str:
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _serve(self, method):
                parts = urlsplit(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = raw.decode(errors="replace")
                entry = {
                    "method": method,
                    "path": parts.path,
                    "query": parts.query,
                    "body": body,
                    "auth": self.headers.get("Authorization", ""),
                }
                with fake.lock:
                    fake.requests.append(entry)
                handler = fake.routes.get((method, parts.path))
                if handler is None:
                    status, out = 404, {"message": "Not Found"}
                else:
                    status, out = handler(entry)
                data = json.dumps(out).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-RateLimit-Remaining", "4999")
                self.send_header("X-RateLimit-Limit", "5000")
                self.send_header("X-RateLimit-Reset", str(int(time.time()) + 3600))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                self._serve("POST")

            def do_PATCH(self):
                self._serve("PATCH")

            def do_PUT(self):
                self._serve("PUT")

            def do_DELETE(self):
                self._serve("DELETE")

        port = free_port()
        self._server = ThreadingHTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{port}"

    def stop(self):
        if self._server:
            self._server.shutdown()


# ── Fake Ollama ──────────────────────────────────────────────────────────────


class FakeOllama:
    """
    /api/chat, answering by which prompt it was sent. `script` maps a substring
    of the system prompt to either a dict (sent as JSON text) or a str.
    Every call is recorded with its prompts, so a test can assert on exactly
    what the model was shown.
    """

    def __init__(self, script: dict):
        self.script = script
        self.calls: list[dict] = []
        self.lock = threading.Lock()
        self._server = None

    def start(self) -> str:
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                data = json.dumps({"models": [{"name": "llama3.1:8b"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                msgs = req.get("messages") or []
                system = " ".join(m.get("content", "") for m in msgs if m.get("role") == "system")
                user = " ".join(m.get("content", "") for m in msgs if m.get("role") == "user")
                answer = None
                for key, value in fake.script.items():
                    if key in system or key in user:
                        answer = value(system, user) if callable(value) else value
                        break
                if answer is None:
                    answer = {"error": "no script for this prompt"}
                text = answer if isinstance(answer, str) else json.dumps(answer)
                with fake.lock:
                    fake.calls.append({"system": system, "user": user, "answer": text})
                data = json.dumps(
                    {"message": {"content": text}, "prompt_eval_count": 100, "eval_count": 50}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        port = free_port()
        self._server = ThreadingHTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{port}"

    def stop(self):
        if self._server:
            self._server.shutdown()


# ── The real server ──────────────────────────────────────────────────────────


class Server:
    """gunicorn with the Procfile's flags, against a real Redis."""

    def __init__(self, github_url: str, ollama_url: str, redis_url: str, extra_env=None):
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.tmp = tempfile.mkdtemp(prefix="e2e_")
        Path(self.tmp, "e2e_launcher.py").write_text(LAUNCHER)
        self.log_path = Path(self.tmp, "server.log")
        env = {
            k: v
            for k, v in os.environ.items()
            if k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
        }
        env.update(
            {
                "PYTHONPATH": f"{self.tmp}{os.pathsep}{REPO_ROOT}",
                "E2E_GITHUB_FAKE": github_url,
                "REDIS_URL": redis_url,
                "GITHUB_WEBHOOK_SECRET": WEBHOOK_SECRET,
                "GITHUB_APP_ID": "123456",
                "GITHUB_PRIVATE_KEY": rsa_private_key_pem(),
                "OLLAMA_HOST": ollama_url,
                "LLM_LOCAL_ONLY": "1",
                "METRICS_AUTH_TOKEN": "e2e-metrics-token",
                "MCP_API_KEY": "e2e-mcp-key",
                "LOG_LEVEL": "INFO",
                "LOG_FORMAT": "text",
                "NO_PROXY": "127.0.0.1,localhost",
                "GROQ_API_KEY": "",
                "GEMINI_API_KEY": "",
                "OPENROUTER_API_KEY": "",
            }
        )
        env.update(extra_env or {})
        self.env = env
        self.proc = None

    def start(self, timeout: float = 30.0):
        # Not a context manager: the server writes here for as long as it runs,
        # and stop() closes it.
        self._log = open(self.log_path, "w")  # noqa: SIM115
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "gunicorn",
                "e2e_launcher:app",
                "--workers",
                "1",
                "--threads",
                "8",
                "--timeout",
                "120",
                "--worker-class",
                "gthread",
                "--bind",
                f"127.0.0.1:{self.port}",
            ],
            cwd=str(REPO_ROOT),
            env=self.env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )
        import requests

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited early:\n{self.log()}")
            try:
                if requests.get(f"{self.base}/ping", timeout=1, proxies={"http": None}).ok:
                    return
            except Exception:
                time.sleep(0.2)
        raise RuntimeError(f"server did not come up:\n{self.log()}")

    def log(self) -> str:
        try:
            return self.log_path.read_text()
        except OSError:
            return ""

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if getattr(self, "_log", None):
            self._log.close()

    def send(self, event: str, payload: dict, secret: str = WEBHOOK_SECRET, delivery=None):
        import requests

        body = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return requests.post(
            f"{self.base}/webhook",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": event,
                "X-GitHub-Delivery": delivery or str(uuid.uuid4()),
                "X-Hub-Signature-256": sig,
            },
            timeout=10,
            proxies={"http": None},
        )


def wait_for(predicate, timeout: float = 30.0, interval: float = 0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = predicate()
        if v:
            return v
        time.sleep(interval)
    return None
