"""
Structured Logger - app/core/logger.py
Sets up consistent, structured logging across the entire app.
"""

import logging
import sys
import os
import time


class StructuredFormatter(logging.Formatter):
    """
    Outputs logs in a consistent format:
    [LEVEL] timestamp | repo | event | message
    """

    def format(self, record: logging.LogRecord) -> str:
        timestamp = self.formatTime(record, "%Y-%m-%dT%H:%M:%S")
        repo = getattr(record, "repo", "-")
        event = getattr(record, "event", "-")
        level = record.levelname.ljust(8)
        return f"[{level}] {timestamp} | {repo} | {event} | {record.getMessage()}"


def setup_logging():
    """Call once at app startup."""
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    root = logging.getLogger()
    root.setLevel(level)

    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(StructuredFormatter())
        root.addHandler(handler)

    # Silence noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


class EventLogger:
    """
    Context-aware logger that auto-attaches repo and event to every log line.
    Usage:
        log = EventLogger("pull_request", repo="owner/repo")
        log.info("PR opened")   →  [INFO] ... | owner/repo | pull_request | PR opened
    """

    def __init__(self, event: str, repo: str = "-"):
        self._logger = logging.getLogger(f"app.{event}")
        self._extra = {"repo": repo, "event": event}
        self._start = time.time()

    def info(self, msg: str): self._logger.info(msg, extra=self._extra)
    def warning(self, msg: str): self._logger.warning(msg, extra=self._extra)
    def error(self, msg: str): self._logger.error(msg, extra=self._extra)
    def debug(self, msg: str): self._logger.debug(msg, extra=self._extra)

    def done(self, msg: str = ""):
        elapsed = int((time.time() - self._start) * 1000)
        self._logger.info(f"{msg} [{elapsed}ms]", extra=self._extra)
