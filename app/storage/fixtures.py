"""
Replay Fixtures - app/storage/fixtures.py
V3: Capture real webhook payloads as test fixtures.
Enables regression testing when models/prompts change.
"""

import json
import os
from datetime import datetime
from app.core.logger import get_logger

log = get_logger(__name__)

FIXTURES_DIR = os.environ.get("FIXTURES_DIR", "tests/fixtures")


def capture(event_type: str, payload: dict, label: str = ""):
    """Save a real webhook payload as a named fixture."""
    os.makedirs(FIXTURES_DIR, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    name = (
        f"{event_type}_{label}_{timestamp}.json"
        if label
        else f"{event_type}_{timestamp}.json"
    )
    path = os.path.join(FIXTURES_DIR, name)
    with open(path, "w") as f:
        json.dump({"event_type": event_type, "payload": payload}, f, indent=2)
    log.info("fixture.captured", path=path)
    return path


def load(name: str) -> dict:
    """Load a fixture by filename."""
    path = os.path.join(FIXTURES_DIR, name)
    with open(path) as f:
        return json.load(f)


def list_fixtures(event_type: str = "") -> list:
    """List available fixtures, optionally filtered by event type."""
    if not os.path.exists(FIXTURES_DIR):
        return []
    files = os.listdir(FIXTURES_DIR)
    if event_type:
        files = [f for f in files if f.startswith(event_type)]
    return sorted(files)
