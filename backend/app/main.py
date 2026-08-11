from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from sqlite3 import Row
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .database import connect, init_db
from .models import ActiveSession, DeclineEditRequest, PullRequest, Task, TaskCreate, TaskUpdate
from .scoring import task_score


DECLINE_WINDOW_SECONDS = 5 * 60


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(title="Odot API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat()


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def task_from_row(row: Row) -> Task:
    return Task(**dict(row))


def get_task_row(task_id: int) -> Row:
    with connect() as db:
        row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return row


def get_session_row() -> Row | None:
    with connect() as db:
        return db.execute(
            """
            SELECT
                active_session.started_at,
                tasks.*
            FROM active_session
            JOIN tasks ON tasks.id = active_session.task_id
            WHERE active_session.id = 1
            """
        ).fetchone()


def session_response(row: Row | None) -> ActiveSession | None:
    if row is None:
        return None
    started = parse_dt(row["started_at"])
    task_data = {key: row[key] for key in row.keys() if key != "started_at"}
    return ActiveSession(
        task=Task(**task_data),
        started_at=row["started_at"],
        decline_available_until=(started + timedelta(seconds=DECLINE_WINDOW_SECONDS)).isoformat(),
    )


@app.get("/tasks", response_model=list[Task])
def list_tasks(status: str = Query(default="active", pattern="^(active|archived)$")) -> list[Task]:
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC", (status,)
        ).fetchall()
    return [task_from_row(row) for row in rows]


@app.post("/tasks", response_model=Task, status_code=201)
def create_task(payload: TaskCreate) -> Task:
    with connect() as db:
        cursor = db.execute(
            """
            INSERT INTO tasks (
                title, importance, urgency, difficulty, time_estimate_minutes, status, created_at
            )
            VALUES (?, ?, ?, ?, ?, 'active', ?)
            """,
            (
                payload.title,
                payload.importance,
                payload.urgency,
                payload.difficulty,
                payload.time_estimate_minutes,
                iso_now(),
            ),
        )
        row = db.execute("SELECT * FROM tasks WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return task_from_row(row)


@app.patch("/tasks/{task_id}", response_model=Task)
def update_task(task_id: int, payload: TaskUpdate) -> Task:
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return task_from_row(get_task_row(task_id))

    assignments = ", ".join(f"{key} = ?" for key in updates)
    values = [*updates.values(), task_id]
    with connect() as db:
        cursor = db.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ? AND status = 'active'", values
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Active task not found")
        row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return task_from_row(row)


@app.delete("/tasks/{task_id}", status_code=204, response_model=None)
def delete_task(task_id: int) -> None:
    with connect() as db:
        cursor = db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Task not found")


@app.post("/tasks/pull", response_model=ActiveSession)
def pull_task(payload: PullRequest) -> ActiveSession:
    if get_session_row() is not None:
        raise HTTPException(status_code=409, detail="A task is already in progress")

    with connect() as db:
        tasks = db.execute("SELECT * FROM tasks WHERE status = 'active'").fetchall()
        if not tasks:
            raise HTTPException(status_code=404, detail="No active tasks available")

        selected = max(tasks, key=lambda task: task_score(task, payload.energy_level))
        started_at = iso_now()
        db.execute(
            "INSERT INTO active_session (id, task_id, started_at) VALUES (1, ?, ?)",
            (selected["id"], started_at),
        )

    return session_response(get_session_row())


@app.get("/session", response_model=ActiveSession | None)
def get_session() -> ActiveSession | None:
    return session_response(get_session_row())


@app.post("/session/decline-edit", response_model=Task | None)
def decline_edit(payload: DeclineEditRequest) -> Task | None:
    session = get_session_row()
    if session is None:
        raise HTTPException(status_code=404, detail="No active session")

    if now_utc() - parse_dt(session["started_at"]) > timedelta(seconds=DECLINE_WINDOW_SECONDS):
        raise HTTPException(status_code=403, detail="Decline window has expired")

    task_id = session["id"]
    with connect() as db:
        if payload.action == "delete":
            db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            db.execute("DELETE FROM active_session WHERE id = 1")
            return None

        if payload.task is None:
            raise HTTPException(status_code=422, detail="Task changes are required")
        updates = payload.task.model_dump(exclude_unset=True)
        if not updates:
            raise HTTPException(status_code=422, detail="At least one task change is required")

        assignments = ", ".join(f"{key} = ?" for key in updates)
        db.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", [*updates.values(), task_id])
        db.execute("DELETE FROM active_session WHERE id = 1")
        row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return task_from_row(row)


@app.post("/tasks/{task_id}/finish", response_model=Task)
def finish_task(task_id: int) -> Task:
    session = get_session_row()
    if session is None or session["id"] != task_id:
        raise HTTPException(status_code=409, detail="Task is not the active pulled task")

    elapsed = max(0, int((now_utc() - parse_dt(session["started_at"])).total_seconds()))
    with connect() as db:
        db.execute(
            """
            UPDATE tasks
            SET status = 'archived', archived_at = ?, actual_duration_seconds = ?
            WHERE id = ?
            """,
            (iso_now(), elapsed, task_id),
        )
        db.execute("DELETE FROM active_session WHERE id = 1")
        row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return task_from_row(row)
