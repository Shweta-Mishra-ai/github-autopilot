"""
app/core/env.py — numeric settings read from the environment at import time.

Several modules did `int(os.environ.get(NAME, "default"))` at import. An empty
or mistyped value ("", "6 workers", "1e3") raised ValueError while importing,
which stopped the whole app from starting over a tuning knob, and a value
such as MAX_DISPATCH_WORKERS=0 was accepted and made every dispatch a 503.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def env_int(name: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    """An integer setting, or `default` when unset, malformed or out of range. Never raises."""
    raw = os.environ.get(name, "")
    if not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        log.warning(f"env.invalid_int name={name} value={raw!r} — using {default}")
        return default
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        log.warning(f"env.out_of_range name={name} value={value} — using {default}")
        return default
    return value
