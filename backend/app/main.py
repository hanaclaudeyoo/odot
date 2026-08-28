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
    CompleteRequest,
    DeclineEditRequest,
    PullRequest,
    Task,
    TaskCreate,
    TaskUpdate,
)
from .scoring import task_score


DECLINE_WINDOW_SECONDS = 5 * 60

# Deadline proximity mapped onto the urgency rubric, as (hours until deadline, urgency).
# 10 overdue, 9 next few hours, 8 end of today, 7 tomorrow, 6 next few days,
# 5 end of this week, 4 next week, 3 end of the month, 2 next month, 1 not due soon.
URGENCY_ANCHORS = (
    (0, 10.0),
    (3, 9.0),
    (12, 8.0),
    (24, 7.0),
    (72, 6.0),
    (168, 5.0),
    (336, 4.0),
    (720, 3.0),
    (1440, 2.0),
    (2160, 1.0),
)

# A task with no deadline is rubric level 1, "not due soon".
NO_DEADLINE_URGENCY = 1.0

# urgency is a legacy NOT NULL column that is no longer read; urgency derives from deadline_at.
LEGACY_URGENCY = 0


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


def deadline_urgency(hours_until_deadline: float) -> float:
    if hours_until_deadline <= 0:
        return 10

    previous_hours, previous_urgency = URGENCY_ANCHORS[0]
    for anchor_hours, anchor_urgency in URGENCY_ANCHORS[1:]:
        if hours_until_deadline <= anchor_hours:
            position = (hours_until_deadline - previous_hours) / (anchor_hours - previous_hours)
            return round(previous_urgency + (anchor_urgency - previous_urgency) * position, 2)
        previous_hours, previous_urgency = anchor_hours, anchor_urgency

    return URGENCY_ANCHORS[-1][1]


def task_urgency(task: Row | dict, reference_time: datetime) -> float:
    deadline_at = task["deadline_at"]
    if deadline_at is None:
        return NO_DEADLINE_URGENCY

    hours_until_deadline = (parse_dt(deadline_at) - reference_time).total_seconds() / 3600
    return deadline_urgency(hours_until_deadline)


def start_window_is_open(task: Row | dict) -> bool:
    start_window_at = task["start_window_at"]
    if start_window_at is None:
        return True
    return parse_dt(start_window_at) <= now_utc()


def task_data_from_row(row: Row) -> dict:
    data = dict(row)
    # Archived tasks freeze at the urgency they had when they were archived; active
    # tasks are measured against the present.
    archived_at = data["archived_at"]
    reference_time = now_utc() if data["status"] == "active" or archived_at is None else parse_dt(archived_at)
    data["urgency"] = task_urgency(data, reference_time)
    return data


def task_from_row(row: Row, dependency_ids: list[int] | None = None) -> Task:
    return Task(**task_data_from_row(row), dependency_ids=dependency_ids or [])


def task_dependency_ids(db, task_id: int) -> list[int]:
    rows = db.execute(
        """
        SELECT depends_on_task_id
        FROM task_dependencies
        WHERE task_id = ?
        ORDER BY depends_on_task_id
        """,
        (task_id,),
    ).fetchall()
    return [row["depends_on_task_id"] for row in rows]


def dependency_ids_by_task(db) -> dict[int, list[int]]:
    grouped: dict[int, list[int]] = {}
    rows = db.execute(
        """
        SELECT task_id, depends_on_task_id
        FROM task_dependencies
        ORDER BY task_id, depends_on_task_id
        """
    ).fetchall()
    for row in rows:
        grouped.setdefault(row["task_id"], []).append(row["depends_on_task_id"])
    return grouped


def replace_task_dependencies(db, task_id: int, dependency_ids: list[int]) -> None:
    unique_dependency_ids = list(dict.fromkeys(dependency_ids))
    if task_id in unique_dependency_ids:
        raise HTTPException(status_code=422, detail="A task cannot depend on itself")
    if unique_dependency_ids:
        placeholders = ", ".join("?" for _ in unique_dependency_ids)
        found = db.execute(
            f"SELECT COUNT(*) AS count FROM tasks WHERE id IN ({placeholders})",
            unique_dependency_ids,
        ).fetchone()["count"]
        if found != len(unique_dependency_ids):
            raise HTTPException(status_code=404, detail="Dependency task not found")
    db.execute("DELETE FROM task_dependencies WHERE task_id = ?", (task_id,))
    db.executemany(
        "INSERT INTO task_dependencies (task_id, depends_on_task_id) VALUES (?, ?)",
        [(task_id, dependency_id) for dependency_id in unique_dependency_ids],
    )


def dependencies_are_removed(db, task_id: int) -> bool:
    row = db.execute(
        """
        SELECT COUNT(*) AS count
        FROM task_dependencies
        JOIN tasks ON tasks.id = task_dependencies.depends_on_task_id
        WHERE task_dependencies.task_id = ?
          AND tasks.status = 'active'
        """,
        (task_id,),
    ).fetchone()
    return row["count"] == 0


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
    with connect() as db:
        dependency_ids = task_dependency_ids(db, row["id"])
    return ActiveSession(
        task=Task(**task_data, dependency_ids=dependency_ids),
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
        grouped_dependency_ids = dependency_ids_by_task(db)
    return [task_from_row(row, grouped_dependency_ids.get(row["id"], [])) for row in rows]


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
                start_window_at,
                category_id,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
            """,
            (
                payload.title,
                payload.importance,
                LEGACY_URGENCY,
                payload.difficulty,
                payload.time_estimate_minutes,
                payload.deadline_at,
                payload.start_window_at,
                payload.category_id,
                iso_now(),
            ),
        )
        replace_task_dependencies(db, cursor.lastrowid, payload.dependency_ids)
        row = db.execute("SELECT * FROM tasks WHERE id = ?", (cursor.lastrowid,)).fetchone()
        dependency_ids = task_dependency_ids(db, cursor.lastrowid)
    return task_from_row(row, dependency_ids)


@app.patch("/tasks/{task_id}", response_model=Task)
def update_task(task_id: int, payload: TaskUpdate) -> Task:
    updates = payload.model_dump(exclude_unset=True)
    dependency_ids = updates.pop("dependency_ids", None)
    if not updates and dependency_ids is None:
        row = get_task_row(task_id)
        with connect() as db:
            return task_from_row(row, task_dependency_ids(db, task_id))

    with connect() as db:
        current = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if current is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if "category_id" in updates and updates["category_id"] != current["category_id"]:
            ensure_category_exists(db, updates["category_id"])
        if updates:
            assignments = ", ".join(f"{key} = ?" for key in updates)
            values = [*updates.values(), task_id]
            cursor = db.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", values)
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Task not found")
        if dependency_ids is not None:
            replace_task_dependencies(db, task_id, dependency_ids)
        row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        current_dependency_ids = task_dependency_ids(db, task_id)
    return task_from_row(row, current_dependency_ids)


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


@app.post("/tasks/{task_id}/complete", response_model=Task)
def complete_task(task_id: int, payload: CompleteRequest) -> Task:
    """Archive an active task with a hand-entered duration, bypassing the pull session."""
    actual_duration_seconds = (
        payload.actual_duration_minutes * 60
        if payload.actual_duration_minutes is not None
        else None
    )
    with connect() as db:
        snapshot = category_snapshot_for_task(db, task_id)
        cursor = db.execute(
            """
            UPDATE tasks
            SET status = 'archived',
                archived_at = ?,
                actual_duration_seconds = ?,
                category_snapshot = COALESCE(category_snapshot, ?)
            WHERE id = ? AND status = 'active'
            """,
            (iso_now(), actual_duration_seconds, snapshot, task_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Active task not found")
        db.execute("DELETE FROM active_session WHERE task_id = ?", (task_id,))
        row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        dependency_ids = task_dependency_ids(db, task_id)
    return task_from_row(row, dependency_ids)


@app.post("/tasks/{task_id}/restore", response_model=Task)
def restore_task(task_id: int) -> Task:
    with connect() as db:
        cursor = db.execute(
            """
            UPDATE tasks
            SET status = 'active',
                archived_at = NULL,
                actual_duration_seconds = NULL,
                category_snapshot = NULL
            WHERE id = ? AND status IN ('archived', 'deleted')
            """,
            (task_id,),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Archived task not found")
        row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        dependency_ids = task_dependency_ids(db, task_id)
    return task_from_row(row, dependency_ids)


@app.delete("/tasks/{task_id}/purge", status_code=204, response_model=None)
def purge_task(task_id: int) -> None:
    """Permanently remove an archived task."""
    with connect() as db:
        cursor = db.execute(
            "DELETE FROM tasks WHERE id = ? AND status IN ('archived', 'deleted')", (task_id,)
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Archived task not found")


@app.post("/tasks/pull", response_model=ActiveSession)
def pull_task(payload: PullRequest) -> ActiveSession:
    if get_session_row() is not None:
        raise HTTPException(status_code=409, detail="A task is already in progress")

    with connect() as db:
        if payload.category_id is None:
            tasks = db.execute(
                """
                SELECT *
                FROM tasks
                WHERE status = 'active'
                """
            ).fetchall()
        else:
            ensure_category_exists(db, payload.category_id)
            subtree_ids = category_descendant_ids(db, payload.category_id)
            placeholders = ", ".join("?" for _ in subtree_ids)
            tasks = db.execute(
                f"""
                SELECT *
                FROM tasks
                WHERE status = 'active'
                  AND category_id IN ({placeholders})
                """,
                subtree_ids,
            ).fetchall()
        tasks = [
            task
            for task in tasks
            if task["difficulty"] <= payload.energy_level
            and start_window_is_open(task)
            and dependencies_are_removed(db, task["id"])
        ]
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
            "SELECT * FROM tasks WHERE id = ? AND status = 'active'", (task_id,)
        ).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail="Active task not found")
        if not start_window_is_open(task):
            raise HTTPException(status_code=403, detail="Task cannot be started before its start window")

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
        dependency_ids = updates.pop("dependency_ids", None)
        if not updates and dependency_ids is None:
            raise HTTPException(status_code=422, detail="At least one task change is required")
        if "category_id" in updates:
            ensure_category_exists(db, updates["category_id"])

        if updates:
            assignments = ", ".join(f"{key} = ?" for key in updates)
            db.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", [*updates.values(), task_id])
        if dependency_ids is not None:
            replace_task_dependencies(db, task_id, dependency_ids)
        db.execute("DELETE FROM active_session WHERE id = 1")
        row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        current_dependency_ids = task_dependency_ids(db, task_id)
    return task_from_row(row, current_dependency_ids)


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
        dependency_ids = task_dependency_ids(db, task_id)
    return task_from_row(row, dependency_ids)
