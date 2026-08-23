from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from sqlite3 import Row
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .database import connect, init_db
from .models import (
    ActiveSession,
    Category,
    CategoryCreate,
    CategoryDeletePreview,
    CategoryUpdate,
    DeclineEditRequest,
    PullRequest,
    Task,
    TaskCreate,
    TaskUpdate,
)
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
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def dynamic_urgency(task: Row | dict) -> float:
    base_urgency = float(task["urgency"])
    deadline_at = task["deadline_at"]
    if deadline_at is None:
        return base_urgency

    deadline = parse_dt(deadline_at)
    hours_until_deadline = (deadline - now_utc()).total_seconds() / 3600
    if hours_until_deadline <= 0:
        return 10

    intensity = (float(task["difficulty"]) + float(task["importance"])) / 20
    horizon_hours = 12 + intensity * 324
    if hours_until_deadline >= horizon_hours:
        return base_urgency

    pressure = 1 - (hours_until_deadline / horizon_hours)
    curve = 2 - intensity * 1.3
    urgency_lift = (10 - base_urgency) * (pressure ** curve)
    return min(10, max(base_urgency, round(base_urgency + urgency_lift, 2)))


def task_data_from_row(row: Row) -> dict:
    data = dict(row)
    base_urgency = data["urgency"]
    data["base_urgency"] = base_urgency
    if data["status"] == "active":
        data["urgency"] = dynamic_urgency(data)
    return data


def task_from_row(row: Row) -> Task:
    return Task(**task_data_from_row(row))


def category_from_row(row: Row) -> Category:
    return Category(**dict(row))


def normalize_category_order(db, parent_id: int | None) -> None:
    if parent_id is None:
        rows = db.execute(
            "SELECT id FROM categories WHERE parent_id IS NULL ORDER BY sort_order, id"
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id FROM categories WHERE parent_id = ? ORDER BY sort_order, id",
            (parent_id,),
        ).fetchall()
    for index, row in enumerate(rows):
        db.execute("UPDATE categories SET sort_order = ? WHERE id = ?", (index, row["id"]))


def category_exists(db, category_id: int | None) -> bool:
    if category_id is None:
        return True
    return db.execute("SELECT id FROM categories WHERE id = ?", (category_id,)).fetchone() is not None


def ensure_category_exists(db, category_id: int | None) -> None:
    if not category_exists(db, category_id):
        raise HTTPException(status_code=422, detail="Category not found")


def sibling_name_exists(
    db,
    name: str,
    parent_id: int | None,
    exclude_id: int | None = None,
) -> bool:
    params: list[object] = [name]
    exclude_clause = ""
    if exclude_id is not None:
        exclude_clause = " AND id != ?"
        params.append(exclude_id)
    if parent_id is None:
        row = db.execute(
            f"SELECT id FROM categories WHERE lower(name) = lower(?) AND parent_id IS NULL{exclude_clause}",
            params,
        ).fetchone()
    else:
        row = db.execute(
            f"SELECT id FROM categories WHERE lower(name) = lower(?) AND parent_id = ?{exclude_clause}",
            [name, parent_id, *(params[1:])],
        ).fetchone()
    return row is not None


def ensure_unique_sibling_name(
    db,
    name: str,
    parent_id: int | None,
    exclude_id: int | None = None,
) -> None:
    if sibling_name_exists(db, name, parent_id, exclude_id):
        raise HTTPException(status_code=409, detail="A sibling category already has that name")


def category_descendant_ids(db, category_id: int) -> list[int]:
    rows = db.execute(
        """
        WITH RECURSIVE subtree(id) AS (
            SELECT id FROM categories WHERE id = ?
            UNION ALL
            SELECT categories.id
            FROM categories
            JOIN subtree ON categories.parent_id = subtree.id
        )
        SELECT id FROM subtree
        """,
        (category_id,),
    ).fetchall()
    return [row["id"] for row in rows]


def ensure_not_descendant_parent(db, category_id: int, parent_id: int | None) -> None:
    if parent_id is None:
        return
    if parent_id in category_descendant_ids(db, category_id):
        raise HTTPException(status_code=422, detail="Category cannot move into itself")


def reorder_category(db, category_id: int, parent_id: int | None, sort_order: int) -> None:
    if parent_id is None:
        rows = db.execute(
            "SELECT id FROM categories WHERE parent_id IS NULL AND id != ? ORDER BY sort_order, id",
            (category_id,),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id FROM categories WHERE parent_id = ? AND id != ? ORDER BY sort_order, id",
            (parent_id, category_id),
        ).fetchall()
    ids = [row["id"] for row in rows]
    index = max(0, min(sort_order, len(ids)))
    ids.insert(index, category_id)
    for order, sibling_id in enumerate(ids):
        db.execute(
            "UPDATE categories SET parent_id = ?, sort_order = ? WHERE id = ?",
            (parent_id, order, sibling_id),
        )


def category_path(db, category_id: int | None) -> str | None:
    if category_id is None:
        return None
    rows = db.execute(
        """
        WITH RECURSIVE ancestors(id, name, parent_id, depth) AS (
            SELECT id, name, parent_id, 0 FROM categories WHERE id = ?
            UNION ALL
            SELECT categories.id, categories.name, categories.parent_id, ancestors.depth + 1
            FROM categories
            JOIN ancestors ON ancestors.parent_id = categories.id
        )
        SELECT name FROM ancestors ORDER BY depth DESC
        """,
        (category_id,),
    ).fetchall()
    if not rows:
        return None
    return " / ".join(row["name"] for row in rows)


def category_snapshot_for_task(db, task_id: int) -> str | None:
    row = db.execute("SELECT category_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    return category_path(db, row["category_id"])


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
    task_data = task_data_from_row(row)
    task_data.pop("started_at", None)
    return ActiveSession(
        task=Task(**task_data),
        started_at=row["started_at"],
        decline_available_until=(started + timedelta(seconds=DECLINE_WINDOW_SECONDS)).isoformat(),
    )


@app.get("/categories", response_model=list[Category])
def list_categories() -> list[Category]:
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM categories ORDER BY COALESCE(parent_id, 0), sort_order, name"
        ).fetchall()
    return [category_from_row(row) for row in rows]


@app.post("/categories", response_model=Category, status_code=201)
def create_category(payload: CategoryCreate) -> Category:
    with connect() as db:
        ensure_category_exists(db, payload.parent_id)
        ensure_unique_sibling_name(db, payload.name, payload.parent_id)
        if payload.parent_id is None:
            next_order = db.execute(
                "SELECT COALESCE(MAX(sort_order) + 1, 0) AS next_order FROM categories WHERE parent_id IS NULL"
            ).fetchone()["next_order"]
        else:
            next_order = db.execute(
                "SELECT COALESCE(MAX(sort_order) + 1, 0) AS next_order FROM categories WHERE parent_id = ?",
                (payload.parent_id,),
            ).fetchone()["next_order"]
        cursor = db.execute(
            """
            INSERT INTO categories (name, parent_id, sort_order, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (payload.name, payload.parent_id, next_order, iso_now()),
        )
        row = db.execute("SELECT * FROM categories WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return category_from_row(row)


@app.patch("/categories/{category_id}", response_model=Category)
def update_category(category_id: int, payload: CategoryUpdate) -> Category:
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        with connect() as db:
            row = db.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Category not found")
        return category_from_row(row)

    with connect() as db:
        current = db.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
        if current is None:
            raise HTTPException(status_code=404, detail="Category not found")

        next_name = updates.get("name", current["name"])
        next_parent_id = updates.get("parent_id", current["parent_id"])
        next_sort_order = updates.get("sort_order", current["sort_order"])

        ensure_category_exists(db, next_parent_id)
        ensure_not_descendant_parent(db, category_id, next_parent_id)
        ensure_unique_sibling_name(db, next_name, next_parent_id, category_id)

        db.execute("UPDATE categories SET name = ? WHERE id = ?", (next_name, category_id))
        if "parent_id" in updates or "sort_order" in updates:
            old_parent_id = current["parent_id"]
            reorder_category(db, category_id, next_parent_id, next_sort_order)
            normalize_category_order(db, old_parent_id)
        row = db.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
    return category_from_row(row)


@app.get("/categories/{category_id}/delete-preview", response_model=CategoryDeletePreview)
def category_delete_preview(category_id: int) -> CategoryDeletePreview:
    with connect() as db:
        if not category_exists(db, category_id):
            raise HTTPException(status_code=404, detail="Category not found")
        subtree_ids = category_descendant_ids(db, category_id)
        placeholders = ", ".join("?" for _ in subtree_ids)
        active_task_count = db.execute(
            f"SELECT COUNT(*) AS count FROM tasks WHERE status = 'active' AND category_id IN ({placeholders})",
            subtree_ids,
        ).fetchone()["count"]
    return CategoryDeletePreview(active_task_count=active_task_count, category_count=len(subtree_ids))


@app.delete("/categories/{category_id}", status_code=204, response_model=None)
def delete_category(category_id: int) -> None:
    with connect() as db:
        category = db.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
        if category is None:
            raise HTTPException(status_code=404, detail="Category not found")
        subtree_ids = category_descendant_ids(db, category_id)
        placeholders = ", ".join("?" for _ in subtree_ids)
        db.execute(
            f"UPDATE tasks SET category_id = NULL WHERE status = 'active' AND category_id IN ({placeholders})",
            subtree_ids,
        )
        db.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        normalize_category_order(db, category["parent_id"])


@app.get("/tasks", response_model=list[Task])
def list_tasks(status: str = Query(default="active", pattern="^(active|archived)$")) -> list[Task]:
    with connect() as db:
        if status == "archived":
            rows = db.execute(
                """
                SELECT *
                FROM tasks
                WHERE status IN ('archived', 'deleted')
                ORDER BY COALESCE(archived_at, created_at) DESC
                """
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM tasks WHERE status = 'active' ORDER BY created_at DESC"
            ).fetchall()
    return [task_from_row(row) for row in rows]


@app.post("/tasks", response_model=Task, status_code=201)
def create_task(payload: TaskCreate) -> Task:
    with connect() as db:
        ensure_category_exists(db, payload.category_id)
        cursor = db.execute(
            """
            INSERT INTO tasks (
                title,
                importance,
                urgency,
                difficulty,
                time_estimate_minutes,
                deadline_at,
                category_id,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
            """,
            (
                payload.title,
                payload.importance,
                payload.urgency,
                payload.difficulty,
                payload.time_estimate_minutes,
                payload.deadline_at,
                payload.category_id,
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

    with connect() as db:
        if "category_id" in updates:
            ensure_category_exists(db, updates["category_id"])
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = [*updates.values(), task_id]
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
        snapshot = category_snapshot_for_task(db, task_id)
        cursor = db.execute(
            """
            UPDATE tasks
            SET status = 'deleted',
                archived_at = ?,
                actual_duration_seconds = NULL,
                category_snapshot = COALESCE(category_snapshot, ?)
            WHERE id = ? AND status = 'active'
            """,
            (iso_now(), snapshot, task_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Active task not found")
        db.execute("DELETE FROM active_session WHERE task_id = ?", (task_id,))


@app.post("/tasks/pull", response_model=ActiveSession)
def pull_task(payload: PullRequest) -> ActiveSession:
    if get_session_row() is not None:
        raise HTTPException(status_code=409, detail="A task is already in progress")

    with connect() as db:
        tasks = db.execute("SELECT * FROM tasks WHERE status = 'active'").fetchall()
        if not tasks:
            raise HTTPException(status_code=404, detail="No active tasks available")

        selected = max(
            tasks,
            key=lambda task: task_score(task_data_from_row(task), payload.energy_level),
        )
        started_at = iso_now()
        db.execute(
            "INSERT INTO active_session (id, task_id, started_at) VALUES (1, ?, ?)",
            (selected["id"], started_at),
        )

    return session_response(get_session_row())


@app.post("/tasks/{task_id}/start", response_model=ActiveSession)
def start_task(task_id: int) -> ActiveSession:
    if get_session_row() is not None:
        raise HTTPException(status_code=409, detail="A task is already in progress")

    with connect() as db:
        task = db.execute(
            "SELECT id FROM tasks WHERE id = ? AND status = 'active'", (task_id,)
        ).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail="Active task not found")

        db.execute(
            "INSERT INTO active_session (id, task_id, started_at) VALUES (1, ?, ?)",
            (task_id, iso_now()),
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
            snapshot = category_snapshot_for_task(db, task_id)
            db.execute(
                """
                UPDATE tasks
                SET status = 'deleted',
                    archived_at = ?,
                    actual_duration_seconds = NULL,
                    category_snapshot = COALESCE(category_snapshot, ?)
                WHERE id = ?
                """,
                (iso_now(), snapshot, task_id),
            )
            db.execute("DELETE FROM active_session WHERE id = 1")
            return None

        if payload.task is None:
            raise HTTPException(status_code=422, detail="Task changes are required")
        updates = payload.task.model_dump(exclude_unset=True)
        if not updates:
            raise HTTPException(status_code=422, detail="At least one task change is required")
        if "category_id" in updates:
            ensure_category_exists(db, updates["category_id"])

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
        snapshot = category_snapshot_for_task(db, task_id)
        db.execute(
            """
            UPDATE tasks
            SET status = 'archived',
                archived_at = ?,
                actual_duration_seconds = ?,
                category_snapshot = COALESCE(category_snapshot, ?)
            WHERE id = ?
            """,
            (iso_now(), elapsed, snapshot, task_id),
        )
        db.execute("DELETE FROM active_session WHERE id = 1")
        row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return task_from_row(row)
