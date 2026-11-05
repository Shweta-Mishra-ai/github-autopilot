"""Audit round 2: A21 (large backup restored nothing), A22 (first
maintenance check an hour after boot)."""

import base64
from unittest.mock import patch


def test_a_backup_over_one_megabyte_is_restored_from_the_blob():
    from app.core import memory_backup as mb

    blob = base64.b64encode(b"ciphertext").decode()

    def get(path, token):
        if "/contents/" in path:
            return {"content": "", "encoding": "none", "size": 2_000_000, "sha": "blobsha"}
        assert path.endswith("/git/blobs/blobsha")
        return {"content": blob, "encoding": "base64"}

    with (
        patch("app.github.client.gh_get", side_effect=get),
        patch.object(mb, "import_encrypted", return_value=3) as imp,
    ):
        assert mb.restore_from_github("o/backups", "memory.bin", "t") == 3
    imp.assert_called_once_with(b"ciphertext")


def test_the_first_maintenance_check_is_soon_after_boot(monkeypatch):
    from app.core import maintenance

    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise SystemExit  # stop the loop thread

    started = {}

    class FakeThread:
        def __init__(self, target, daemon, name):
            started["target"] = target

        def start(self):
            pass

        def is_alive(self):
            return False

    monkeypatch.setattr(maintenance, "_thread", None)
    monkeypatch.setattr(maintenance, "enabled", lambda: True)
    monkeypatch.setattr(maintenance, "tick", lambda: None)
    monkeypatch.setattr(maintenance.time, "sleep", fake_sleep)
    with patch("threading.Thread", FakeThread):
        maintenance.start_scheduler()
    try:
        started["target"]()
    except SystemExit:
        pass
    monkeypatch.setattr(maintenance, "_thread", None)
    assert sleeps == [maintenance.FIRST_TICK_SECONDS, maintenance.TICK_SECONDS]
    assert maintenance.FIRST_TICK_SECONDS < maintenance.TICK_SECONDS
