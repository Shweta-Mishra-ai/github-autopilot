"""
tests/test_logging_config.py

LOG_LEVEL and LOG_FORMAT are documented in .env.example, set by two workflows,
and were read by nothing.

server.py and worker.py each called logging.basicConfig with a hardcoded INFO
and a hardcoded text format. app/core/logger.py had setup_logging(), which
reads both and carries a JSON formatter written for log drains — and no file
in the repository ever called it. Verified before the fix by booting the app
with LOG_LEVEL=DEBUG: the root logger stayed at INFO and the debug line was
never emitted.

That is the worst shape a bug can take in an operations tool. The setting is
documented, so an operator sets it, sees no debug output, and concludes the
code path they are chasing is not running.

Two kinds of test here, because the bug had two halves:
  - setup_logging must honour what it is given;
  - the entry points must actually call it. A correct function nobody calls is
    exactly what shipped.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

from app.core.logger import setup_logging

ROOT = Path(__file__).parent.parent
ENTRY_POINTS = ("server.py", "worker.py")


@pytest.fixture(autouse=True)
def restore_root_logging():
    """setup_logging clears the root logger, which would otherwise leak into
    every test that runs after this file."""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    yield
    root.handlers.clear()
    root.handlers.extend(saved_handlers)
    root.setLevel(saved_level)


def _emit(level: str, fmt: str, message: str = "probe-line") -> str:
    import io

    setup_logging(level=level, fmt=fmt)
    root = logging.getLogger()
    buffer = io.StringIO()
    sink = logging.StreamHandler(buffer)
    sink.setFormatter(root.handlers[0].formatter)
    root.addHandler(sink)
    logging.getLogger("probe").debug(message)
    logging.getLogger("probe").info(message)
    return buffer.getvalue()


class TestLogLevelIsHonoured:
    def test_debug_emits_debug_lines(self):
        assert "probe-line" in _emit("DEBUG", "text")
        assert logging.getLogger().level == logging.DEBUG

    def test_info_suppresses_debug_lines(self):
        output = _emit("INFO", "text")
        assert output.count("probe-line") == 1, "the debug line should not appear"

    def test_lowercase_is_accepted(self):
        """Nobody reads .env.example closely enough to match the case."""
        setup_logging(level="debug", fmt="text")
        assert logging.getLogger().level == logging.DEBUG

    def test_an_unknown_level_falls_back_to_info_rather_than_crashing(self):
        setup_logging(level="LOUD", fmt="text")
        assert logging.getLogger().level == logging.INFO


class TestLogFormatIsHonoured:
    def test_json_lines_parse_as_json(self):
        for line in _emit("INFO", "json").strip().splitlines():
            entry = json.loads(line)
            assert set(entry) >= {"ts", "level", "logger", "msg"}

    def test_text_is_not_json(self):
        assert not _emit("INFO", "text").strip().startswith("{")


class TestTheDefaultDoesNotChangeExistingDeployments:
    """Every deployment emits text today because the JSON formatter was never
    reached. Wiring the setting up must not silently reformat everyone's logs;
    json is opt-in."""

    @pytest.mark.parametrize("entry", ENTRY_POINTS)
    def test_entry_points_default_to_text(self, entry):
        source = (ROOT / entry).read_text(encoding="utf-8")
        match = re.search(r'LOG_FORMAT",\s*"(\w+)"', source)
        assert match, f"{entry} does not read LOG_FORMAT with a default"
        assert match.group(1) == "text", (
            f"{entry} defaults LOG_FORMAT to {match.group(1)!r}. Existing "
            "deployments emit text; changing that while fixing a setting that "
            "never worked would reformat production logs nobody asked about."
        )

    @pytest.mark.parametrize("entry", ENTRY_POINTS)
    def test_entry_points_default_to_info(self, entry):
        source = (ROOT / entry).read_text(encoding="utf-8")
        assert re.search(r'LOG_LEVEL",\s*"INFO"', source), f"{entry} default is not INFO"


class TestTheEntryPointsActuallyCallIt:
    """The half that shipped: setup_logging was correct, complete, tested by
    nothing, and called by nowhere."""

    @pytest.mark.parametrize("entry", ENTRY_POINTS)
    def test_setup_logging_is_called(self, entry):
        source = (ROOT / entry).read_text(encoding="utf-8")
        assert "setup_logging(" in source, (
            f"{entry} does not call setup_logging, so LOG_LEVEL and LOG_FORMAT "
            "are decoration again"
        )

    @pytest.mark.parametrize("entry", ENTRY_POINTS)
    def test_basic_config_is_not_used(self, entry):
        """basicConfig cannot read the settings, and does nothing at all when
        the root logger already has a handler — which under gunicorn it
        sometimes does, so even its hardcoded format was not reliable."""
        source = (ROOT / entry).read_text(encoding="utf-8")
        assert "logging.basicConfig(" not in source, (
            f"{entry} is back on basicConfig, which ignores LOG_LEVEL and "
            "LOG_FORMAT and is a no-op under a pre-configured root logger"
        )

    @pytest.mark.parametrize("entry", ENTRY_POINTS)
    def test_both_settings_are_read_from_the_environment(self, entry):
        source = (ROOT / entry).read_text(encoding="utf-8")
        for var in ("LOG_LEVEL", "LOG_FORMAT"):
            assert var in source, f"{entry} never reads {var}"
