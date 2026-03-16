"""
Logger - app/core/logger.py
V3: Structured logging using structlog.
IMPORTANT: Never use 'event' as a keyword argument in log calls.
structlog reserves 'event' for the log message itself.
Use 'webhook_event', 'event_name', or 'evt' instead.
"""

import logging
import structlog


def setup_logging(level: str = "INFO"):
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )


def get_logger(name: str = __name__):
    return structlog.get_logger(name)


class EventLogger:
    """
    Wrapper around structlog for handler-level logging.
    NOTE: Never pass event= as a kwarg. Use evt=, webhook_event=, or event_name= instead.
    """
    def __init__(self, name: str, **ctx):
        self._log = structlog.get_logger(name).bind(**ctx)

    def info(self, msg: str, **kw):
        self._log.info(msg, **kw)

    def warning(self, msg: str, **kw):
        self._log.warning(msg, **kw)

    def error(self, msg: str, **kw):
        self._log.error(msg, **kw)

    def debug(self, msg: str, **kw):
        self._log.debug(msg, **kw)

    def done(self, msg: str, **kw):
        self._log.info(msg, status="done", **kw)
