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


TASKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    importance REAL NOT NULL CHECK (importance BETWEEN 0 AND 10),
    urgency REAL NOT NULL CHECK (urgency BETWEEN 0 AND 10),
    difficulty REAL NOT NULL CHECK (difficulty BETWEEN 0 AND 10),
    time_estimate_minutes INTEGER NOT NULL CHECK (time_estimate_minutes > 0),
    deadline_at TEXT,
    start_window_at TEXT,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    category_snapshot TEXT,
    status TEXT NOT NULL CHECK (status IN ('active', 'archived', 'deleted')) DEFAULT 'active',
    created_at TEXT NOT NULL,
    archived_at TEXT,
    actual_duration_seconds INTEGER
)
"""

CATEGORIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 32),
    parent_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
)
"""

SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS active_session (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL
)
"""

DEPENDENCIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on_task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on_task_id),
    CHECK (task_id != depends_on_task_id)
)
"""


def _table_sql(db: sqlite3.Connection, table_name: str) -> str | None:
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
    ).fetchone()
    return None if row is None else row["sql"]


def _table_columns(db: sqlite3.Connection, table_name: str) -> set[str]:
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _migrate_tasks_schema(db: sqlite3.Connection) -> None:
    session = None
    db.execute("DROP TABLE IF EXISTS task_tags")
    db.execute("DROP TABLE IF EXISTS tags")
    db.execute("DROP TABLE IF EXISTS task_dependencies")
    if _table_sql(db, "active_session") is not None:
        session = db.execute("SELECT task_id, started_at FROM active_session WHERE id = 1").fetchone()
    columns = _table_columns(db, "tasks")
    db.execute("DROP TABLE IF EXISTS active_session")
    db.execute("ALTER TABLE tasks RENAME TO tasks_old")
    db.execute(CATEGORIES_SCHEMA)
    db.execute(TASKS_SCHEMA)
    category_id_expr = "category_id" if "category_id" in columns else "NULL"
    category_snapshot_expr = "category_snapshot" if "category_snapshot" in columns else "NULL"
    deadline_at_expr = "deadline_at" if "deadline_at" in columns else "NULL"
    start_window_at_expr = "start_window_at" if "start_window_at" in columns else "NULL"
    db.execute(
        f"""
        INSERT INTO tasks (
            id,
            title,
            importance,
            urgency,
            difficulty,
            time_estimate_minutes,
            deadline_at,
            start_window_at,
            category_id,
            category_snapshot,
            status,
            created_at,
            archived_at,
            actual_duration_seconds
        )
        SELECT
            id,
            title,
            CAST(importance AS REAL),
            CAST(urgency AS REAL),
            CAST(difficulty AS REAL),
            time_estimate_minutes,
            {deadline_at_expr},
            {start_window_at_expr},
            {category_id_expr},
            {category_snapshot_expr},
            status,
            created_at,
            archived_at,
            actual_duration_seconds
        FROM tasks_old
        """
    )
    db.execute("DROP TABLE tasks_old")
    db.execute(SESSION_SCHEMA)
    if session is not None:
        active_task = db.execute(
            "SELECT id FROM tasks WHERE id = ? AND status = 'active'", (session["task_id"],)
        ).fetchone()
        if active_task is not None:
            db.execute(
                "INSERT INTO active_session (id, task_id, started_at) VALUES (1, ?, ?)",
                (session["task_id"], session["started_at"]),
            )
    db.execute(DEPENDENCIES_SCHEMA)


def init_db() -> None:
    with connect() as db:
        task_sql = _table_sql(db, "tasks")
        db.execute(CATEGORIES_SCHEMA)
        if task_sql is not None and (
            "BETWEEN 1 AND 7" in task_sql
            or "'deleted'" not in task_sql
            or "category_id" not in task_sql
            or "category_snapshot" not in task_sql
            or "deadline_at" not in task_sql
            or "start_window_at" not in task_sql
        ):
            _migrate_tasks_schema(db)
        else:
            db.execute(TASKS_SCHEMA)
            db.execute(SESSION_SCHEMA)
            db.execute(DEPENDENCIES_SCHEMA)
        db.execute("DROP TABLE IF EXISTS task_tags")
        db.execute("DROP TABLE IF EXISTS tags")
