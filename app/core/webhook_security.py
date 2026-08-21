"""
app/core/webhook_security.py
V5 — Security hardening pass.

FIXES vs V4:
  1. WEBHOOK_SECRET frozen at import: verify_signature() now reads from
     os.environ on every call instead of using the module-level constant.
     Rotating the secret no longer requires a full redeploy.
  2. Content-Length bypass: verify_webhook() now checks len(request.data)
     instead of the Content-Length header. A missing or spoofed header can no
     longer let an oversized body bypass the size check.
  3. X-Forwarded-For IP spoofing: The IP used for rate limiting is no longer
     taken blindly from X-Forwarded-For. We read the *last* trusted hop from
     the header chain (Render always appends the real client IP) and fall back
     to request.remote_addr. Attackers can no longer spoof IPs by injecting a
     forged X-Forwarded-For header value.
  4. In-memory IP dict memory leak: old IP keys were never removed after their
     sliding window emptied. Under a flood, every unique IP stayed in
     _ip_counts forever. Fixed: delete key when window becomes empty.
  5. startup_check() now also validates GITHUB_APP_ID (numeric) and
     GITHUB_PRIVATE_KEY (non-empty). Auth failures at request time are now
     caught at boot instead.
"""

import hashlib
import hmac
import logging
import os
import time
import threading

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

MAX_PAYLOAD_BYTES = 25 * 1024 * 1024  # 25 MB
MAX_AGE_SECONDS = 300  # Reject webhooks older than 5 minutes
IP_RATE_LIMIT = 100  # Max requests per IP per minute


# ── Startup check ─────────────────────────────────────────────────────────────


def startup_check():
    """
    Call once at application startup. Raises RuntimeError on any missing or
    obviously invalid credential. Fail loud at boot, not silently per-request.
    """
    errors = []

    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        errors.append("GITHUB_WEBHOOK_SECRET is not set. Webhooks cannot be verified.")
    elif len(secret) < 20:
        log.warning(
            f"webhook_security.weak_secret: only {len(secret)} chars. Use 32+ random chars."
        )

    app_id = os.environ.get("GITHUB_APP_ID", "")
    if not app_id:
        errors.append("GITHUB_APP_ID is not set.")
    elif not app_id.strip().isdigit():
        errors.append(f"GITHUB_APP_ID must be numeric, got: {app_id!r}")

    private_key = os.environ.get("GITHUB_PRIVATE_KEY", "")
    if not private_key:
        errors.append("GITHUB_PRIVATE_KEY is not set.")
    elif "BEGIN" not in private_key:
        errors.append("GITHUB_PRIVATE_KEY does not look like a PEM key (missing 'BEGIN' marker).")

    if errors:
        raise RuntimeError("Startup validation failed:\n  - " + "\n  - ".join(errors))

    # ── Non-fatal warnings — surface config gaps that weaken security but
    #    should not block boot (the endpoints fail closed / degrade safely). ──
    if not os.environ.get("METRICS_AUTH_TOKEN", "").strip():
        log.warning(
            "webhook_security.metrics_unauthed: METRICS_AUTH_TOKEN is NOT set — "
            "/health and /metrics expose internal state PUBLICLY. Set a strong "
            "random value to lock them (and the dashboard)."
        )
    if not os.environ.get("MCP_API_KEY", "").strip():
        log.warning(
            "webhook_security.mcp_unconfigured: MCP_API_KEY is NOT set — the /mcp "
            "endpoint is fail-closed (returns 503) so the IDE plugin will NOT work "
            "until you set it."
        )

    log.info("webhook_security.startup_ok: all credentials validated.")


# ── Signature verification ────────────────────────────────────────────────────


def _get_webhook_secret() -> bytes:
    """
    Read GITHUB_WEBHOOK_SECRET from env on every call.

    FIXED: V4 stored this as a module-level constant WEBHOOK_SECRET, so
    rotating the secret required a full redeploy. Now we read from env each
    time — zero-downtime secret rotation is possible.
    """
    return os.environ.get("GITHUB_WEBHOOK_SECRET", "").encode()


def verify_signature(payload_bytes: bytes, signature_header: str) -> bool:
    """
    Constant-time HMAC-SHA256 verification.
    FAIL CLOSED: returns False if GITHUB_WEBHOOK_SECRET is empty.
    """
    secret = _get_webhook_secret()
    if not secret:
        log.error(
            "webhook_security.no_secret: GITHUB_WEBHOOK_SECRET is empty — REJECTING all webhooks."
        )
        return False

    if not signature_header or not signature_header.startswith("sha256="):
        log.warning("webhook_security.missing_signature")
        return False

    expected = "sha256=" + hmac.new(secret, payload_bytes, hashlib.sha256).hexdigest()

    ok = hmac.compare_digest(expected, signature_header)
    if not ok:
        log.warning("webhook_security.invalid_signature")
    return ok


# ── Timestamp / replay protection ─────────────────────────────────────────────


def verify_timestamp(headers: dict) -> bool:
    """
    Reject webhooks older than MAX_AGE_SECONDS when a timestamp header is present.
    GitHub does not consistently send a timestamp — idempotency (delivery ID
    dedup) handles replay protection for the common case.
    """
    ts_header = headers.get("X-GitHub-Event-Time") or headers.get("X-Timestamp")
    if not ts_header:
        return True

    try:
        event_ts = int(ts_header)
        age = time.time() - event_ts
        if age > MAX_AGE_SECONDS:
            log.warning(
                f"webhook_security.replay_attempt age={int(age)}s "
                f"max={MAX_AGE_SECONDS}s — rejecting"
            )
            return False
        if age < -30:
            log.warning(f"webhook_security.future_timestamp age={age:.0f}s — rejecting")
            return False
    except (ValueError, TypeError):
        pass

    return True


# ── IP extraction — spoofing-resistant ────────────────────────────────────────


def _get_client_ip(request) -> str:
    """
    Extract real client IP in a spoofing-resistant way.

    FIXED: V4 took the *first* value from X-Forwarded-For, which an attacker
    can inject freely (X-Forwarded-For: spoofed, real). The platform (Render)
    always appends the actual client IP as the *last* entry in the chain.
    Taking the last value makes forged prefixes irrelevant.

    Falls back to request.remote_addr if the header is missing (direct
    connections / local dev).
    """
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        # Last entry = the IP the platform saw; earlier entries are client-supplied
        return xff.split(",")[-1].strip()
    return request.remote_addr or "unknown"


# ── IP Rate Limiting (sliding window) ─────────────────────────────────────────

_ip_counts: dict[str, list] = {}
_ip_lock = threading.Lock()

# Whether the limiter is running without Redis. Logging that on every request
# does not make it more true, only less readable — and this is the webhook
# path, so "every request" is the busiest thing in the process.
_rl_in_fallback = False

# Amortised sweep of IPs nobody has seen in a while. Without it the dict grows
# once per distinct source address, forever: the old code tried to delete empty
# windows, but appended the current timestamp *before* testing for emptiness,
# so the window was never empty and the delete branch could not run.
_last_sweep = 0.0
_SWEEP_INTERVAL = 60.0


def check_ip_rate_limit(ip: str) -> bool:
    """
    Sliding window rate limit: IP_RATE_LIMIT requests per 60 seconds.
    Prefers Redis for multi-worker correctness. Falls back to in-memory.
    """
    try:
        from app.core.redis_client import get_redis, is_redis_available

        if is_redis_available():
            r = get_redis()
            key = f"webhook_rl:{ip}:{int(time.time() // 60)}"
            count = r.incr(key)
            r.expire(key, 60)
            ok = int(count) <= IP_RATE_LIMIT
            if not ok:
                log.warning(f"webhook_security.rate_limit_redis ip={ip} count={count}")
            return ok
    except Exception as e:
        _rate_limit_fallback_once(e)

    # In-memory sliding window fallback
    now = time.time()
    with _ip_lock:
        _sweep_stale_ips(now)

        window = [t for t in _ip_counts.get(ip, []) if now - t < 60]
        ok = len(window) < IP_RATE_LIMIT

        # Only record requests that are actually allowed. Appending while over
        # the limit let a flooding IP grow its own window without bound for a
        # full minute — the limiter paying for the flood it is refusing.
        if ok:
            window.append(now)

        if window:
            _ip_counts[ip] = window
        else:
            _ip_counts.pop(ip, None)

        if not ok:
            log.warning(f"webhook_security.rate_limit_memory ip={ip} count={len(window)}")
        return ok


def _rate_limit_fallback_once(exc: Exception) -> None:
    """Log the drop to in-memory limiting once per episode, not per request."""
    global _rl_in_fallback
    if not _rl_in_fallback:
        _rl_in_fallback = True
        log.warning(
            f"webhook_security.rate_limit_memory_fallback ({exc}) — the limit is "
            "per-process until Redis returns, so N workers allow N times the "
            "configured rate. Logged once, not per request."
        )


def _sweep_stale_ips(now: float) -> None:
    """
    Drop IPs with no request in the last window. Caller holds _ip_lock.

    Amortised: at most once per _SWEEP_INTERVAL, so the O(addresses) pass is
    not paid on every webhook. Without any sweep the dict gains an entry per
    distinct source address and never loses one — a public endpoint is scanned
    by a lot of addresses that never come back.
    """
    global _last_sweep
    if now - _last_sweep < _SWEEP_INTERVAL:
        return
    _last_sweep = now
    stale = [addr for addr, win in _ip_counts.items() if not win or now - win[-1] >= 60]
    for addr in stale:
        del _ip_counts[addr]
    if stale:
        log.debug(f"webhook_security.rate_limit_swept addresses={len(stale)}")


# ── Bot loop prevention ────────────────────────────────────────────────────────

BOT_SENDER_TYPES = {"Bot", "bot"}
BOT_LOGIN_SUFFIXES = ("[bot]",)
OWN_BOT_LOGINS = {
    "ai-repo-manager[bot]",
    "github-autopilot[bot]",
}


def is_bot_sender(payload: dict) -> bool:
    """Returns True if webhook was triggered by a bot — prevents loops."""
    sender = payload.get("sender", {})
    sender_type = sender.get("type", "")
    sender_login = sender.get("login", "")

    if sender_type in BOT_SENDER_TYPES:
        return True
    if any(sender_login.endswith(suf) for suf in BOT_LOGIN_SUFFIXES):
        return True
    return sender_login in OWN_BOT_LOGINS


# ── Full verification pipeline ────────────────────────────────────────────────


def verify_webhook(request) -> tuple[bool, str]:
    """
    Full webhook verification pipeline. Returns (ok, error_message).

    Checks (in order):
    1. Payload size  — checked against actual body bytes, not Content-Length header
    2. IP rate limit — using spoofing-resistant IP extraction
    3. HMAC signature
    4. Timestamp / replay protection
    """
    # 1. Payload size — use len(request.data) not Content-Length header
    #    FIXED: Content-Length is optional; a missing header let large bodies through.
    payload_bytes = request.data
    if len(payload_bytes) > MAX_PAYLOAD_BYTES:
        return False, "Payload too large"

    # 2. IP rate limit — spoofing-resistant extraction
    client_ip = _get_client_ip(request)
    if not check_ip_rate_limit(client_ip):
        return False, "Too many requests"

    # 3. HMAC signature
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not verify_signature(payload_bytes, sig):
        return False, "Invalid signature"

    # 4. Timestamp / replay
    if not verify_timestamp(dict(request.headers)):
        return False, "Webhook too old or timestamp invalid"

    return True, ""
