from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def database_path() -> Path:
    configured = os.environ.get("ODOT_DB_PATH")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[1] / "odot.sqlite3"


def connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_db() -> None:
    with connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                importance INTEGER NOT NULL CHECK (importance BETWEEN 1 AND 7),
                urgency INTEGER NOT NULL CHECK (urgency BETWEEN 1 AND 7),
                difficulty INTEGER NOT NULL CHECK (difficulty BETWEEN 1 AND 7),
                time_estimate_minutes INTEGER NOT NULL CHECK (time_estimate_minutes > 0),
                status TEXT NOT NULL CHECK (status IN ('active', 'archived')) DEFAULT 'active',
                created_at TEXT NOT NULL,
                archived_at TEXT,
                actual_duration_seconds INTEGER
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS active_session (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                started_at TEXT NOT NULL
            )
            """
        )
