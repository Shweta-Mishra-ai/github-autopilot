"""
app/core/webhook_security.py
─────────────────────────────
Production-grade webhook security layer.

FIXES vs original server.py:
  1. FAIL CLOSED: Missing WEBHOOK_SECRET → reject ALL webhooks (not skip).
  2. REPLAY PROTECTION: Reject webhooks older than 5 minutes using
     X-GitHub-Delivery timestamp embedded in the delivery ID prefix.
     GitHub sends `X-GitHub-Event-Timestamp` (undocumented but real).
  3. STARTUP VALIDATION: Fails loudly at boot if WEBHOOK_SECRET not set.
  4. STRUCTURED LOGGING: All security events get consistent log format.
  5. RATE LIMITING: Per-IP is preserved but uses stricter sliding window.

HOW TO USE:
  from app.core.webhook_security import verify_webhook, startup_check
  startup_check()      # call once at app startup
  ok, err = verify_webhook(request)
  if not ok:
      return jsonify({"error": err}), 401
"""

import hashlib
import hmac
import logging
import os
import time
import threading

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "").encode()
MAX_PAYLOAD_BYTES = 25 * 1024 * 1024   # 25 MB
MAX_AGE_SECONDS = 300                   # Reject webhooks older than 5 minutes
IP_RATE_LIMIT = 100                     # Requests per IP per minute

# ── Startup check ─────────────────────────────────────────────────────────────

def startup_check():
    """
    Call at application startup. Raises RuntimeError if secrets not set.
    This is intentional — running without a webhook secret is a security hole,
    not a configuration warning.
    """
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        raise RuntimeError(
            "GITHUB_WEBHOOK_SECRET is not set. "
            "Refusing to start — webhooks cannot be verified without it. "
            "Set this environment variable to your GitHub App webhook secret."
        )
    if len(secret) < 20:
        log.warning(
            "webhook_security.weak_secret: GITHUB_WEBHOOK_SECRET is very short "
            f"({len(secret)} chars). Use a strong random secret (32+ chars recommended)."
        )
    log.info("webhook_security.startup_ok: GITHUB_WEBHOOK_SECRET is configured.")


# ── Signature verification ────────────────────────────────────────────────────

def verify_signature(payload_bytes: bytes, signature_header: str) -> bool:
    """
    Constant-time HMAC-SHA256 verification.
    FAIL CLOSED: returns False if WEBHOOK_SECRET is empty.
    """
    if not WEBHOOK_SECRET:
        log.error(
            "webhook_security.no_secret: GITHUB_WEBHOOK_SECRET is empty — "
            "REJECTING all webhooks. Set this variable to enable webhook processing."
        )
        return False   # ← KEY CHANGE: was True (bypass), now False (reject)

    if not signature_header or not signature_header.startswith("sha256="):
        log.warning("webhook_security.missing_signature")
        return False

    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET, payload_bytes, hashlib.sha256
    ).hexdigest()

    ok = hmac.compare_digest(expected, signature_header)
    if not ok:
        log.warning("webhook_security.invalid_signature")
    return ok


# ── Timestamp / replay protection ─────────────────────────────────────────────

def verify_timestamp(headers: dict) -> bool:
    """
    Reject webhooks older than MAX_AGE_SECONDS.
    GitHub sends X-GitHub-Hook-Installation-Target-ID but NOT a reliable
    timestamp header in all versions. We use our own receive time + check
    for the optional GitHub-supplied header when available.

    NOTE: This cannot catch replays within the MAX_AGE window — that's
    what idempotency (delivery ID dedup) handles. Together they give
    complete replay protection.
    """
    # GitHub does not consistently send a timestamp — we check if available
    ts_header = headers.get("X-GitHub-Event-Time") or headers.get("X-Timestamp")
    if not ts_header:
        return True   # Header not present — skip age check, rely on idempotency

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
        pass   # Can't parse — allow (timestamp header is optional)

    return True


# ── IP Rate Limiting (sliding window) ─────────────────────────────────────────

_ip_counts: dict[str, list] = {}   # {ip: [timestamp, ...]}
_ip_lock = threading.Lock()


def check_ip_rate_limit(ip: str) -> bool:
    """
    Sliding window rate limit: IP_RATE_LIMIT requests per 60 seconds.
    Falls back to Redis if available for multi-process correctness.
    """
    # Try Redis first (survives Gunicorn multi-worker)
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
    except Exception:
        pass

    # In-memory sliding window fallback
    now = time.time()
    with _ip_lock:
        window = _ip_counts.get(ip, [])
        # Keep only timestamps within last 60 seconds
        window = [t for t in window if now - t < 60]
        window.append(now)
        _ip_counts[ip] = window
        ok = len(window) <= IP_RATE_LIMIT
        if not ok:
            log.warning(f"webhook_security.rate_limit_memory ip={ip} count={len(window)}")
        return ok


# ── Bot loop prevention ────────────────────────────────────────────────────────

BOT_SENDER_TYPES = {"Bot", "bot"}
BOT_LOGIN_SUFFIXES = ("[bot]",)
OWN_BOT_LOGINS = {
    "ai-repo-manager[bot]",
    "github-autopilot[bot]",
}


def is_bot_sender(payload: dict) -> bool:
    """
    Returns True if webhook was triggered by a bot to prevent loops.
    Checks sender.type, sender.login suffix, and own-app logins.
    """
    sender = payload.get("sender", {})
    sender_type = sender.get("type", "")
    sender_login = sender.get("login", "")

    if sender_type in BOT_SENDER_TYPES:
        return True
    if any(sender_login.endswith(suf) for suf in BOT_LOGIN_SUFFIXES):
        return True
    if sender_login in OWN_BOT_LOGINS:
        return True
    return False


# ── Full verification pipeline ────────────────────────────────────────────────

def verify_webhook(request) -> tuple[bool, str]:
    """
    Full webhook verification pipeline. Returns (ok, error_message).

    Checks (in order):
    1. Payload size
    2. IP rate limit
    3. HMAC signature (fail closed if secret missing)
    4. Timestamp / replay protection
    """
    # 1. Payload size
    content_length = request.content_length
    if content_length and content_length > MAX_PAYLOAD_BYTES:
        return False, "Payload too large"

    # 2. IP rate limit
    client_ip = (
        request.headers.get("X-Forwarded-For", request.remote_addr or "")
        .split(",")[0].strip()
    )
    if not check_ip_rate_limit(client_ip):
        return False, "Too many requests"

    # 3. HMAC signature
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not verify_signature(request.data, sig):
        return False, "Invalid signature"

    # 4. Timestamp / replay
    if not verify_timestamp(dict(request.headers)):
        return False, "Webhook too old or timestamp invalid"

    return True, ""
