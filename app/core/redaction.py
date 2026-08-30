"""
app/core/redaction.py — scrub text before it enters long-lived memory.

Repo memory used to be gated behind an opt-in env var precisely because it
could hold source code and credentials, which meant it was inert in every
standard cloud deployment: the brain neither learned nor recalled anything.

Redacting at the boundary is the better trade. What memory keeps is prose,
file paths and symbol names — the things that actually make recall useful —
while code bodies and anything secret-shaped are dropped before storage.
"""

from __future__ import annotations

import re

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INDENTED_BLOCK_RE = re.compile(r"(?m)^(?: {4}|\t).*$")

_CODE_PLACEHOLDER = "[code omitted]"
_SECRET_PLACEHOLDER = "[REDACTED]"

# Leading unmasked run of a redacted match, e.g. "ghp_" from
# "ghp_********************aaaa". Anything shorter than 4 chars is too generic
# to substitute on without risking collateral damage to ordinary prose.
_PREFIX_RE = re.compile(r"[^*.]+")


def redact(text: str | None) -> str:
    """
    Remove secrets and code bodies. Returns prose safe to persist.

    Best-effort and never raises: memory is an enhancement, and a redaction
    failure must not take down the command that triggered the write. The
    structural strip (fences, indented blocks) runs first and unconditionally,
    so even if the secret scan fails the bulk of any code body is already gone.
    """
    if not text:
        return ""

    text = _FENCE_RE.sub(_CODE_PLACEHOLDER, text)
    text = _INDENTED_BLOCK_RE.sub(_CODE_PLACEHOLDER, text)

    try:
        from app.security.enhanced_secrets import scan_diff

        # scan_diff only inspects lines beginning with "+", so present each
        # line as a diff addition.
        as_diff = "\n".join(f"+{line}" for line in text.splitlines())
        for finding in scan_diff(as_diff):
            # redacted_match looks like "ghp_********************aaaa" — the
            # leading run before the mask is the only part we can match on.
            prefix = _PREFIX_RE.match(finding.redacted_match or "")
            token = prefix.group(0) if prefix else ""
            if len(token) >= 4:
                text = re.sub(re.escape(token) + r"\S*", _SECRET_PLACEHOLDER, text)
    except Exception:
        pass  # structural strip above already ran

    return text


# ── Credentials carried in a URL ─────────────────────────────────────────────
#
# A different job from redact() above, in the same place because it is the
# same idea at a different boundary: that one scrubs text on its way INTO
# memory, this one scrubs text on its way into a LOG.
#
# `requests` quotes the URL in every exception it raises, so any credential
# carried in a URL ends up in the exception message — and those messages get
# logged, and returned to callers:
#
#     HTTPSConnectionPool(host='generativelanguage.googleapis.com', port=443):
#     Max retries exceeded with url:
#     /v1beta/models/gemini-1.5-flash:generateContent?key=AIzaSy...
#
#     notification.slack_error: ... with url:
#     https://hooks.slack.com/services/T0.../B1.../SeCrEt...
#
# One ordinary connection error was enough to write a provider API key, and a
# Slack or Discord webhook URL, into the deployment's logs in plaintext. A
# webhook URL is itself a credential: anyone holding it can post into the
# channel as the bot.
#
# Where the protocol allows it, the real fix is to move the secret out of the
# URL, and that was done. Where it does not — Slack and Discord webhooks ARE
# the secret — this is the fix.

# Credentials passed as query parameters.
_SECRET_QUERY_PARAMS = ("key", "api_key", "apikey", "access_token", "token")
_SECRET_QUERY_RE = re.compile(r"(?i)\b(" + "|".join(_SECRET_QUERY_PARAMS) + r")=([^&\s\"'`]+)")

# Credentials that ARE the URL path. The bearer of the URL can post as the
# bot, so the whole tail is secret — but the host is kept, because knowing
# WHICH integration failed is the entire value of the log line.
_WEBHOOK_PATH_RES = (
    re.compile(r"(?i)(https?://hooks\.slack\.com/services/)\S+"),
    re.compile(r"(?i)(https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/)\S+"),
    re.compile(r"(?i)(https?://[\w.-]*webhook\.office\.com/webhookb2/)\S+"),
)


def redact_secrets(text: str | None) -> str:
    """
    Return `text` with URL-borne credentials replaced by REDACTED.

    Never raises and never returns None: it is called on error paths, where a
    redaction that throws would replace a logged failure with a new one.
    """
    if not text:
        return ""
    out = str(text)
    for pattern in _WEBHOOK_PATH_RES:
        out = pattern.sub(lambda m: f"{m.group(1)}REDACTED", out)
    return _SECRET_QUERY_RE.sub(lambda m: f"{m.group(1)}=REDACTED", out)
