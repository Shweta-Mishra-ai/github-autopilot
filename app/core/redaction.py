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

# What is masked on a line the scanner flagged: a quoted value, the password in
# `scheme://user:password@`, the value after `=` or `:`, and any long
# token-shaped run. Applied ONLY to flagged lines, so ordinary prose survives.
_LINE_MASKS = (
    (re.compile(r"(['\"])[^'\"]{4,}\1"), r"\1[REDACTED]\1"),
    (re.compile(r"(://[^:@/\s]+:)[^@\s]+@"), r"\1[REDACTED]@"),
    (re.compile(r"([=:]\s*)(?!\[REDACTED\])[^\s'\"]{6,}"), r"\1[REDACTED]"),
    (re.compile(r"(?<![\w\[])[A-Za-z0-9_\-/+=.]{16,}"), "[REDACTED]"),
)


def _mask_line(line: str) -> str:
    for pattern, repl in _LINE_MASKS:
        line = pattern.sub(repl, line)
    return line


def redact(text: str | None) -> str:
    """
    Remove secrets and code bodies. Returns prose safe to persist.

    Best-effort and never raises: memory is an enhancement, and a redaction
    failure must not take down the command that triggered the write. The
    structural strip (fences, indented blocks) runs first and unconditionally,
    so even if the secret scan fails the bulk of any code body is already gone.

    A line the scanner flags has every value-shaped part masked. This used to
    substitute on the first four characters of the scanner's REDACTED match
    up to the next space, which for `password = "…"` replaced `pass` + `word`
    and left the value, and skipped matches of 12 characters or fewer
    entirely — while the memory record said the text had been redacted.
    """
    if not text:
        return ""

    text = _FENCE_RE.sub(_CODE_PLACEHOLDER, text)
    text = _INDENTED_BLOCK_RE.sub(_CODE_PLACEHOLDER, text)

    try:
        from app.security.enhanced_secrets import scan_diff

        lines = text.split("\n")
        # scan_diff only inspects lines beginning with "+", so present each
        # line as a diff addition. With no hunk header, a finding's line
        # number is its position here.
        as_diff = "\n".join(f"+{line}" for line in lines)
        flagged = {f.line_number for f in scan_diff(as_diff)}
        for n in flagged:
            if 1 <= n <= len(lines):
                lines[n - 1] = _mask_line(lines[n - 1])
        text = "\n".join(lines)
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
    # urllib3's "Max retries exceeded with url: …" quotes only the PATH, so
    # the host-anchored patterns above never matched the commonest failure
    # message, and the full webhook secret was logged — and, from the rich
    # senders, returned to /notify and posted into a GitHub comment.
    re.compile(r"((?<![\w/])/services/)T[A-Z0-9]+/B[A-Z0-9]+/\S+"),
    re.compile(r"((?<![\w/])/api/webhooks/)\d+/\S+"),
    re.compile(r"((?<![\w/])/webhookb2/)\S+"),
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
