"""
Tests - tests/test_storage.py
V3: Unit tests for event storage and fixture system.
"""

import os
import tempfile
from unittest.mock import patch


class TestEventStorage:

    def setup_method(self):
        # Use temp DB for each test
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()

    def teardown_method(self):
        # Close any open SQLite connections before deleting (Windows fix)
        import gc
        gc.collect()  # Force garbage collection to close connections
        try:
            os.unlink(self.tmp.name)
        except (PermissionError, OSError):
            # Windows: file still locked — ignore, temp dir will clean up
            pass

    def test_init_db_creates_table(self):
        with patch("app.storage.events.DB_PATH", self.tmp.name):
            from app.storage.events import init_db
            init_db()
            import sqlite3
            conn = sqlite3.connect(self.tmp.name)
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            assert any("events" in t for t in tables)

    def test_save_and_retrieve_event(self):
        with patch("app.storage.events.DB_PATH", self.tmp.name):
            from app.storage.events import init_db, save_event, get_recent
            init_db()
            save_event("d-001", "push", "owner/repo", {"ref": "main"})
            events = get_recent("owner/repo")
            assert len(events) == 1
            assert events[0]["event_type"] == "push"

    def test_duplicate_delivery_ignored(self):
        with patch("app.storage.events.DB_PATH", self.tmp.name):
            from app.storage.events import init_db, save_event, get_recent
            init_db()
            save_event("d-001", "push", "owner/repo", {})
            save_event("d-001", "push", "owner/repo", {})
            events = get_recent("owner/repo")
            assert len(events) == 1

    def test_mark_processed_updates_status(self):
        with patch("app.storage.events.DB_PATH", self.tmp.name):
            from app.storage.events import init_db, save_event, mark_processed
            import sqlite3
            init_db()
            save_event("d-002", "push", "owner/repo", {})
            mark_processed("d-002", "done")
            conn = sqlite3.connect(self.tmp.name)
            row = conn.execute(
                "SELECT status FROM events WHERE delivery_id='d-002'"
            ).fetchone()
            assert row[0] == "done"


class TestFixtures:

    def test_capture_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("app.storage.fixtures.FIXTURES_DIR", tmpdir):
                from app.storage.fixtures import capture, load
                path = capture("push", {"ref": "main"}, "test")
                assert os.path.exists(path)

                filename = os.path.basename(path)
                loaded = load(filename)
                assert loaded["event_type"] == "push"
                assert loaded["payload"]["ref"] == "main"

    def test_list_fixtures_filtered_by_type(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("app.storage.fixtures.FIXTURES_DIR", tmpdir):
                from app.storage.fixtures import capture, list_fixtures
                capture("push", {}, "a")
                capture("push", {}, "b")
                capture("issues", {}, "c")

                push_fixtures = list_fixtures("push")
                assert len(push_fixtures) == 2
                assert all(f.startswith("push") for f in push_fixtures)

    def test_list_fixtures_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("app.storage.fixtures.FIXTURES_DIR", tmpdir):
                from app.storage.fixtures import list_fixtures
                assert list_fixtures() == []
