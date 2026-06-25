"""
app/core/logger.py — V5
Structured logging via stdlib only. No structlog dependency.

REMOVED: structlog dependency.
  - structlog was in requirements.txt but only used here.
  - The rest of the codebase (every handler, every module) uses
    stdlib `logging.getLogger(__name__)` directly.
  - structlog added ~15MB to the install with no benefit for this use case.
  - V5 configures stdlib logging with JSON-like output for Render log drain
    compatibility and consistent structured output.
"""

import logging
import sys
import json


class _JSONFormatter(logging.Formatter):
    """
    Single-line JSON log formatter for Render log drain / Datadog / Logtail.
    Each log line is valid JSON, making it trivial to filter in any log aggregator.
    """

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(level: str = "INFO", fmt: str = "json") -> None:
    """
    Configure root logger. Call once at application startup in server.py.

    Args:
        level: Log level string — "DEBUG", "INFO", "WARNING", "ERROR".
        fmt:   "json" (default, for production) or "text" (for local dev).
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Clear any handlers added by previous calls (gunicorn sometimes adds one)
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)

    if fmt == "json":
        handler.setFormatter(_JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    root.addHandler(handler)

    # Quieten noisy third-party loggers
    for noisy in ("urllib3", "requests", "httpx", "werkzeug"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.getLogger(__name__).info(f"logging.configured level={level} fmt={fmt}")


def get_logger(name: str) -> logging.Logger:
    """Convenience wrapper — identical to logging.getLogger(name)."""
    return logging.getLogger(name)


class EventLogger:
    """
    Structured per-event logger for handlers.

    Drop-in replacement for the old structlog-based EventLogger.
    Accepts the same constructor kwargs (repo, pr, issue, etc.) and
    injects them as key=value pairs into every log line.

    Usage (unchanged from V4):
        log = EventLogger("push", repo="org/myrepo")
        log.info("secret_found", file="config.py", severity="high")
        log.done("push_complete")   # convenience: logs at INFO
        log.warn("rate_limited")    # alias for warning

    NOTE: Never pass event= as a kwarg. Use webhook_event=, evt=, or event_name=.
    """

    def __init__(self, name: str, **ctx):
        self._logger = logging.getLogger(f"handler.{name}")
        self._ctx = ctx

    def _fmt(self, msg: str, **kw) -> str:
        """Format: 'msg key=val key=val ...'"""
        parts = {**self._ctx, **kw}
        suffix = " ".join(f"{k}={v}" for k, v in parts.items())
        return f"{msg} {suffix}" if suffix else msg

    def info(self, msg: str, **kw) -> None:
        self._logger.info(self._fmt(msg, **kw))

    def warning(self, msg: str, **kw) -> None:
        self._logger.warning(self._fmt(msg, **kw))

    # Aliases used across handler files
    def warn(self, msg: str, **kw) -> None:
        self.warning(msg, **kw)

    def error(self, msg: str, **kw) -> None:
        self._logger.error(self._fmt(msg, **kw))

    def debug(self, msg: str, **kw) -> None:
        self._logger.debug(self._fmt(msg, **kw))

    def done(self, msg: str, **kw) -> None:
        """Convenience: logs at INFO level — used for completion signals."""
        self.info(msg, **kw)

    def bind(self, **extra):
        """Return a new EventLogger with additional context merged in."""
        merged = {**self._ctx, **extra}
        return EventLogger(self._logger.name.removeprefix("handler."), **merged)
