"""
Event Storage - app/storage/events.py
V3: SQLite-based event log for replay and debugging.

FIXED (ruff F401): Removed unused `from datetime import datetime`.
SQLite uses its own datetime('now') function — Python datetime not needed.
"""

import sqlite3
import json
import os
from app.core.logger import get_logger

log = get_logger(__name__)

DB_PATH = os.environ.get("EVENT_DB_PATH", "data/events.db")


def _get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_id TEXT UNIQUE,
                event_type TEXT NOT NULL,
                repo TEXT,
                payload TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now')),
                processed_at TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_event_type ON events(event_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_repo ON events(repo)")
        conn.commit()
    log.info("storage.db_initialized", path=DB_PATH)


def save_event(delivery_id: str, event_type: str, repo: str, payload: dict):
    try:
        with _get_conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO events
                (delivery_id, event_type, repo, payload)
                VALUES (?, ?, ?, ?)
            """,
                (delivery_id, event_type, repo, json.dumps(payload)),
            )
            conn.commit()
    except Exception as e:
        log.error("storage.save_failed", error=str(e))


def mark_processed(delivery_id: str, status: str = "done"):
    try:
        with _get_conn() as conn:
            conn.execute(
                """
                UPDATE events SET status=?, processed_at=datetime('now')
                WHERE delivery_id=?
            """,
                (status, delivery_id),
            )
            conn.commit()
    except Exception as e:
        log.error("storage.mark_failed", error=str(e))


def get_recent(repo: str, limit: int = 20) -> list:
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                """
                SELECT delivery_id, event_type, status, created_at
                FROM events WHERE repo=?
                ORDER BY created_at DESC LIMIT ?
            """,
                (repo, limit),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        log.error("storage.get_recent_failed", error=str(e))
        return []
