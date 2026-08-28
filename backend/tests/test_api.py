from __future__ import annotations

from datetime import datetime, timedelta, timezone
from importlib import reload

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ODOT_DB_PATH", str(tmp_path / "test.sqlite3"))
    import app.database as database
    import app.main as main

    reload(database)
    reload(main)
    database.init_db()
    with TestClient(main.app) as test_client:
        yield test_client


def deadline_for_urgency(urgency: float, reference_time: datetime) -> str | None:
    """Inverse of the rubric: the deadline that yields the given urgency."""
    import app.main as main

    if urgency <= main.NO_DEADLINE_URGENCY:
        return None
    anchors = main.URGENCY_ANCHORS
    for (earlier_hours, earlier_urgency), (later_hours, later_urgency) in zip(anchors, anchors[1:]):
        if urgency >= later_urgency:
            position = (urgency - earlier_urgency) / (later_urgency - earlier_urgency)
            hours = earlier_hours + (later_hours - earlier_hours) * position
            return (reference_time + timedelta(hours=hours)).isoformat()
    return None


def create_task(
    client: TestClient,
    title: str,
    importance: float,
    urgency: float,
    difficulty: float,
    category_id: int | None = None,
    deadline_at: str | None = None,
    start_window_at: str | None = None,
    dependency_ids: list[int] | None = None,
):
    payload = {
        "title": title,
        "importance": importance,
        "difficulty": difficulty,
        "time_estimate_minutes": 30,
    }
    if category_id is not None:
        payload["category_id"] = category_id
    # Urgency is deadline-only now, so express the requested urgency as a deadline.
    if deadline_at is None:
        deadline_at = deadline_for_urgency(urgency, datetime.now(timezone.utc))
    if deadline_at is not None:
        payload["deadline_at"] = deadline_at
    if start_window_at is not None:
        payload["start_window_at"] = start_window_at
    if dependency_ids is not None:
        payload["dependency_ids"] = dependency_ids
    response = client.post(
        "/tasks",
        json=payload,
    )
    assert response.status_code == 201
    return response.json()


def create_category(client: TestClient, name: str, parent_id: int | None = None):
    response = client.post("/categories", json={"name": name, "parent_id": parent_id})
    assert response.status_code == 201
    return response.json()


def test_create_task_requires_valid_axes(client):
    response = client.post(
        "/tasks",
        json={
            "title": "Invalid task",
            "importance": 10.01,
            "difficulty": 2,
            "time_estimate_minutes": 15,
        },
    )

    assert response.status_code == 422


def test_create_task_accepts_decimal_axes(client):
    task = create_task(client, "Decimal task", 5.67, 4.32, 8.91)

    assert task["importance"] == 5.67
    assert task["difficulty"] == 8.91


def test_tags_endpoint_and_tables_are_removed(client):
    import app.database as database

    assert client.get("/tags").status_code == 404
    with database.connect() as db:
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('tags', 'task_tags')"
        ).fetchall()
    assert rows == []


def test_manual_urgency_is_rejected(client):
    response = client.post(
        "/tasks",
        json={
            "title": "Manual urgency",
            "importance": 5,
            "urgency": 7,
            "difficulty": 5,
            "time_estimate_minutes": 15,
        },
    )

    assert response.status_code == 422


def test_manual_urgency_is_rejected_on_update(client):
    task = create_task(client, "No manual urgency", 5, 5, 3)

    response = client.patch(f"/tasks/{task['id']}", json={"urgency": 9})

    assert response.status_code == 422


def test_task_without_deadline_is_not_due_soon(client):
    task = create_task(client, "No deadline", 8, 0, 4)

    assert task["deadline_at"] is None
    assert task["urgency"] == 1


def test_task_deadline_is_stored_and_drives_urgency(client, monkeypatch):
    import app.main as main

    current_time = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(main, "now_utc", lambda: current_time)

    task = create_task(
        client,
        "Deadline task",
        10,
        2,
        10,
        deadline_at=(current_time + timedelta(hours=24)).isoformat(),
    )

    assert task["deadline_at"] == (current_time + timedelta(hours=24)).isoformat()
    assert task["urgency"] == 7


@pytest.mark.parametrize(
    "hours_until_deadline, expected_urgency",
    [
        (-1, 10),
        (0, 10),
        (3, 9),
        (12, 8),
        (24, 7),
        (72, 6),
        (168, 5),
        (336, 4),
        (720, 3),
        (1440, 2),
        (2160, 1),
        (9000, 1),
    ],
)
def test_deadline_urgency_matches_rubric_anchors(hours_until_deadline, expected_urgency):
    import app.main as main

    assert main.deadline_urgency(hours_until_deadline) == expected_urgency


def test_deadline_urgency_decreases_as_deadline_recedes():
    import app.main as main

    urgencies = [main.deadline_urgency(hours) for hours in range(0, 2400, 6)]
    assert all(
        later <= earlier for earlier, later in zip(urgencies, urgencies[1:])
    )
    assert min(urgencies) >= 1
    assert max(urgencies) <= 10


def test_deadline_tomorrow_sets_urgency_to_seven(client, monkeypatch):
    import app.main as main

    current_time = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    monkeypatch.setattr(main, "now_utc", lambda: current_time)

    task = create_task(
        client,
        "Due tomorrow",
        10,
        2,
        10,
        deadline_at=(current_time + timedelta(hours=24)).isoformat(),
    )

    assert task["urgency"] == 7


def test_task_start_window_is_stored(client):
    start_window = datetime(2026, 1, 2, 12, tzinfo=timezone.utc).isoformat()

    task = create_task(client, "Start later", 5, 5, 5, start_window_at=start_window)

    assert task["start_window_at"] == start_window


def test_clearing_deadline_returns_task_to_not_due_soon(client, monkeypatch):
    import app.main as main

    current_time = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(main, "now_utc", lambda: current_time)

    task = create_task(
        client,
        "Deadline then cleared",
        5,
        4,
        5,
        deadline_at=(current_time + timedelta(hours=3)).isoformat(),
    )
    assert task["urgency"] == 9

    cleared = client.patch(f"/tasks/{task['id']}", json={"deadline_at": None})
    assert cleared.status_code == 200
    assert cleared.json()["urgency"] == 1


def test_deadline_urgency_ignores_difficulty_and_importance(client, monkeypatch):
    import app.main as main

    current_time = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(main, "now_utc", lambda: current_time)
    deadline_at = (current_time + timedelta(hours=24)).isoformat()

    easy = create_task(client, "Easy", 1, 0, 1, deadline_at=deadline_at)
    hard = create_task(client, "Hard", 10, 0, 10, deadline_at=deadline_at)

    assert easy["urgency"] == hard["urgency"] == 7


def test_archived_task_freezes_urgency_at_archive_time(client, monkeypatch):
    import app.main as main

    current_time = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(main, "now_utc", lambda: current_time)
    task = create_task(
        client,
        "Complete before deadline",
        10,
        2,
        10,
        deadline_at=(current_time + timedelta(hours=24)).isoformat(),
    )
    client.post(f"/tasks/{task['id']}/start")
    client.post(f"/tasks/{task['id']}/finish")

    # Long after the deadline has passed, the archived task still reads as it did on finish.
    monkeypatch.setattr(main, "now_utc", lambda: current_time + timedelta(days=30))
    archived = client.get("/tasks?status=archived").json()[0]

    assert archived["urgency"] == 7


def archive_task(client: TestClient, task: dict) -> None:
    client.post(f"/tasks/{task['id']}/start")
    client.post(f"/tasks/{task['id']}/finish")


def test_complete_archives_task_with_entered_duration(client):
    task = create_task(client, "Done by hand", 5, 5, 3)

    response = client.post(f"/tasks/{task['id']}/complete", json={"actual_duration_minutes": 25})

    assert response.status_code == 200
    assert response.json()["status"] == "archived"
    assert response.json()["actual_duration_seconds"] == 25 * 60
    assert client.get("/tasks?status=active").json() == []
    assert [t["id"] for t in client.get("/tasks?status=archived").json()] == [task["id"]]


def test_complete_does_not_require_a_pull_session(client):
    task = create_task(client, "Never pulled", 5, 5, 3)

    assert client.get("/session").json() is None
    assert client.post(
        f"/tasks/{task['id']}/complete", json={"actual_duration_minutes": 5}
    ).status_code == 200


def test_complete_clears_an_active_session_for_that_task(client):
    task = create_task(client, "Pulled then completed", 5, 5, 3)
    client.post(f"/tasks/{task['id']}/start")

    client.post(f"/tasks/{task['id']}/complete", json={"actual_duration_minutes": 5})

    assert client.get("/session").json() is None


def test_complete_can_archive_with_unknown_duration(client):
    task = create_task(client, "Unknown duration", 5, 5, 3)

    response = client.post(f"/tasks/{task['id']}/complete", json={})

    assert response.status_code == 200
    assert response.json()["status"] == "archived"
    assert response.json()["actual_duration_seconds"] is None


def test_complete_rejects_zero_duration(client):
    task = create_task(client, "Bad duration", 5, 5, 3)

    assert client.post(
        f"/tasks/{task['id']}/complete", json={"actual_duration_minutes": 0}
    ).status_code == 422


def test_complete_rejects_an_already_archived_task(client):
    task = create_task(client, "Twice", 5, 5, 3)
    client.post(f"/tasks/{task['id']}/complete", json={"actual_duration_minutes": 5})

    response = client.post(f"/tasks/{task['id']}/complete", json={"actual_duration_minutes": 5})

    assert response.status_code == 404


def test_update_can_edit_archived_task(client):
    task = create_task(client, "Archived before edit", 5, 5, 3)
    client.post(f"/tasks/{task['id']}/complete", json={"actual_duration_minutes": 5})

    response = client.patch(
        f"/tasks/{task['id']}",
        json={"title": "Edited archived task", "importance": 7.25},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "archived"
    assert response.json()["title"] == "Edited archived task"
    assert response.json()["importance"] == 7.25


def test_restore_returns_archived_task_to_active(client):
    task = create_task(client, "Bring me back", 5, 5, 3)
    archive_task(client, task)
    assert client.get("/tasks?status=active").json() == []

    response = client.post(f"/tasks/{task['id']}/restore")

    assert response.status_code == 200
    assert response.json()["status"] == "active"
    assert response.json()["archived_at"] is None
    assert response.json()["actual_duration_seconds"] is None
    assert [t["id"] for t in client.get("/tasks?status=active").json()] == [task["id"]]
    assert client.get("/tasks?status=archived").json() == []


def test_restore_works_on_a_deleted_task(client):
    task = create_task(client, "Deleted then restored", 5, 5, 3)
    client.delete(f"/tasks/{task['id']}")

    response = client.post(f"/tasks/{task['id']}/restore")

    assert response.status_code == 200
    assert response.json()["status"] == "active"


def test_restore_rejects_an_active_task(client):
    task = create_task(client, "Already active", 5, 5, 3)

    assert client.post(f"/tasks/{task['id']}/restore").status_code == 404


def test_purge_permanently_removes_an_archived_task(client):
    task = create_task(client, "Erase me", 5, 5, 3)
    archive_task(client, task)

    response = client.delete(f"/tasks/{task['id']}/purge")

    assert response.status_code == 204
    assert client.get("/tasks?status=archived").json() == []
    assert client.post(f"/tasks/{task['id']}/restore").status_code == 404


def test_purge_rejects_an_active_task(client):
    task = create_task(client, "Still active", 5, 5, 3)

    assert client.delete(f"/tasks/{task['id']}/purge").status_code == 404
    assert len(client.get("/tasks?status=active").json()) == 1


def test_lists_active_and_archived_tasks(client):
    task = create_task(client, "Write draft", 5, 5, 3)
    client.post("/tasks/pull", json={"energy_level": 5})
    client.post(f"/tasks/{task['id']}/finish")

    active = client.get("/tasks?status=active")
    archived = client.get("/tasks?status=archived")

    assert active.json() == []
    assert archived.json()[0]["title"] == "Write draft"
    assert archived.json()[0]["status"] == "archived"
    assert archived.json()[0]["actual_duration_seconds"] is not None


def test_delete_moves_active_task_to_archive_as_deleted(client):
    task = create_task(client, "Remove later", 5, 5, 3)

    response = client.delete(f"/tasks/{task['id']}")

    assert response.status_code == 204
    assert client.get("/tasks?status=active").json() == []
    archived = client.get("/tasks?status=archived").json()
    assert archived[0]["title"] == "Remove later"
    assert archived[0]["status"] == "deleted"
    assert archived[0]["actual_duration_seconds"] is None


def test_pull_respects_quadrant_priority(client):
    create_task(client, "Low everything", 0, 0, 0)
    create_task(client, "Urgent only", 4.99, 10, 0)
    create_task(client, "Important and urgent", 5, 5, 0)

    response = client.post("/tasks/pull", json={"energy_level": 0})

    assert response.status_code == 200
    assert response.json()["task"]["title"] == "Important and urgent"


def test_scoring_uses_continuous_urgency_and_importance_values():
    from app.scoring import task_score

    base = {"difficulty": 5}
    assert task_score({**base, "urgency": 7.01, "importance": 6}, 5) > task_score(
        {**base, "urgency": 7, "importance": 6}, 5
    )
    assert task_score({**base, "urgency": 2, "importance": 3.33}, 5) > task_score(
        {**base, "urgency": 2, "importance": 3.32}, 5
    )


def test_high_energy_prioritizes_difficult_task_when_other_axes_match(client):
    create_task(client, "Easy peer", 5, 5, 0)
    create_task(client, "Hard peer", 5, 5, 10)

    response = client.post("/tasks/pull", json={"energy_level": 10})

    assert response.json()["task"]["title"] == "Hard peer"


def test_low_energy_favors_easy_task_when_other_axes_match(client):
    create_task(client, "Easy peer", 5, 5, 0)
    create_task(client, "Hard peer", 5, 5, 10)

    response = client.post("/tasks/pull", json={"energy_level": 0})

    assert response.json()["task"]["title"] == "Easy peer"


def test_pull_excludes_tasks_harder_than_energy(client):
    create_task(client, "Too hard", 10, 10, 6)
    create_task(client, "Eligible", 4, 4, 5)

    response = client.post("/tasks/pull", json={"energy_level": 5})

    assert response.status_code == 200
    assert response.json()["task"]["title"] == "Eligible"


def test_pull_blocks_when_session_exists(client):
    create_task(client, "First", 5, 5, 3)
    create_task(client, "Second", 5, 5, 3)

    assert client.post("/tasks/pull", json={"energy_level": 5}).status_code == 200
    assert client.post("/tasks/pull", json={"energy_level": 5}).status_code == 409


def test_pull_respects_category_filter_including_descendants(client):
    parent = create_category(client, "Parent")
    child = create_category(client, "Child", parent["id"])
    other = create_category(client, "Other")
    create_task(client, "Outside but higher priority", 10, 10, 0, category_id=other["id"])
    child_task = create_task(client, "Inside child", 1, 1, 0, category_id=child["id"])

    response = client.post(
        "/tasks/pull",
        json={"energy_level": 5, "category_id": parent["id"]},
    )

    assert response.status_code == 200
    assert response.json()["task"]["id"] == child_task["id"]


def test_pull_category_filter_returns_404_when_no_candidates(client):
    empty_category = create_category(client, "Empty")
    other_category = create_category(client, "Other")
    create_task(client, "Other task", 10, 10, 0, category_id=other_category["id"])

    response = client.post(
        "/tasks/pull",
        json={"energy_level": 5, "category_id": empty_category["id"]},
    )

    assert response.status_code == 404


def test_create_and_update_task_dependencies(client):
    dependency = create_task(client, "Dependency", 5, 5, 3)
    task = create_task(client, "Dependent", 5, 5, 3, dependency_ids=[dependency["id"]])

    assert task["dependency_ids"] == [dependency["id"]]

    response = client.patch(f"/tasks/{task['id']}", json={"dependency_ids": []})

    assert response.status_code == 200
    assert response.json()["dependency_ids"] == []


def test_task_cannot_depend_on_itself(client):
    task = create_task(client, "Self", 5, 5, 3)

    response = client.patch(f"/tasks/{task['id']}", json={"dependency_ids": [task["id"]]})

    assert response.status_code == 422


def test_unknown_dependency_is_rejected(client):
    response = client.post(
        "/tasks",
        json={
            "title": "Bad dependency",
            "importance": 5,
            "difficulty": 5,
            "time_estimate_minutes": 15,
            "dependency_ids": [9999],
        },
    )

    assert response.status_code == 404


def test_pull_excludes_task_until_dependencies_are_removed(client):
    dependency = create_task(client, "Do first", 1, 1, 1)
    dependent = create_task(
        client,
        "Do second",
        10,
        10,
        10,
        dependency_ids=[dependency["id"]],
    )

    first_pull = client.post("/tasks/pull", json={"energy_level": 10})

    assert first_pull.status_code == 200
    assert first_pull.json()["task"]["id"] == dependency["id"]
    client.post(f"/tasks/{dependency['id']}/finish")

    second_pull = client.post("/tasks/pull", json={"energy_level": 10})

    assert second_pull.status_code == 200
    assert second_pull.json()["task"]["id"] == dependent["id"]


def test_pull_excludes_tasks_before_start_window(client, monkeypatch):
    import app.main as main

    current_time = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(main, "now_utc", lambda: current_time)
    create_task(
        client,
        "Blocked high priority",
        10,
        10,
        0,
        start_window_at=(current_time + timedelta(days=1)).isoformat(),
    )
    available = create_task(client, "Available lower priority", 1, 1, 0)

    response = client.post("/tasks/pull", json={"energy_level": 5})

    assert response.status_code == 200
    assert response.json()["task"]["id"] == available["id"]


def test_pull_returns_404_when_all_tasks_before_start_window(client, monkeypatch):
    import app.main as main

    current_time = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(main, "now_utc", lambda: current_time)
    create_task(
        client,
        "Blocked only",
        10,
        10,
        0,
        start_window_at=(current_time + timedelta(days=1)).isoformat(),
    )

    response = client.post("/tasks/pull", json={"energy_level": 5})

    assert response.status_code == 404


def test_start_task_starts_specific_active_task(client):
    create_task(client, "Other", 10, 10, 10)
    task = create_task(client, "Start this one", 0, 0, 0)

    response = client.post(f"/tasks/{task['id']}/start")

    assert response.status_code == 200
    assert response.json()["task"]["id"] == task["id"]
    assert client.get("/session").json()["task"]["title"] == "Start this one"


def test_start_task_blocks_when_session_exists(client):
    first = create_task(client, "First", 5, 5, 3)
    second = create_task(client, "Second", 5, 5, 3)

    assert client.post(f"/tasks/{first['id']}/start").status_code == 200
    assert client.post(f"/tasks/{second['id']}/start").status_code == 409


def test_start_task_fails_before_start_window(client, monkeypatch):
    import app.main as main

    current_time = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(main, "now_utc", lambda: current_time)
    task = create_task(
        client,
        "Cannot start yet",
        5,
        5,
        5,
        start_window_at=(current_time + timedelta(hours=1)).isoformat(),
    )

    response = client.post(f"/tasks/{task['id']}/start")

    assert response.status_code == 403


def test_decline_update_clears_session_before_five_minutes(client):
    task = create_task(client, "Original", 5, 5, 3)
    client.post("/tasks/pull", json={"energy_level": 5})

    response = client.post(
        "/session/decline-edit",
        json={"action": "update", "task": {"title": "Rewritten"}},
    )

    assert response.status_code == 200
    assert response.json()["title"] == "Rewritten"
    assert client.get("/session").json() is None
    assert client.get("/tasks?status=active").json()[0]["id"] == task["id"]


def test_decline_delete_clears_session_before_five_minutes(client):
    create_task(client, "Delete me", 5, 5, 3)
    client.post("/tasks/pull", json={"energy_level": 5})

    response = client.post("/session/decline-edit", json={"action": "delete"})

    assert response.status_code == 200
    assert response.json() is None
    assert client.get("/session").json() is None
    assert client.get("/tasks?status=active").json() == []
    archived = client.get("/tasks?status=archived").json()
    assert archived[0]["title"] == "Delete me"
    assert archived[0]["status"] == "deleted"


def test_decline_fails_after_five_minutes(client, monkeypatch):
    create_task(client, "Expired decline", 5, 5, 3)
    session = client.post("/tasks/pull", json={"energy_level": 5}).json()
    expired_now = datetime.fromisoformat(session["started_at"]) + timedelta(minutes=5, seconds=1)

    import app.main as main

    monkeypatch.setattr(main, "now_utc", lambda: expired_now)
    response = client.post(
        "/session/decline-edit",
        json={"action": "update", "task": {"title": "Too late"}},
    )

    assert response.status_code == 403
    assert client.get("/session").json()["task"]["title"] == "Expired decline"


def test_session_resumes_from_stored_started_at(client):
    create_task(client, "Resume me", 5, 5, 3)
    pulled = client.post("/tasks/pull", json={"energy_level": 5}).json()

    session = client.get("/session")

    assert session.status_code == 200
    assert session.json()["started_at"] == pulled["started_at"]
    assert session.json()["task"]["title"] == "Resume me"


def test_energy_level_must_be_zero_through_ten(client):
    create_task(client, "Energy", 5, 5, 3)

    response = client.post("/tasks/pull", json={"energy_level": -0.01})

    assert response.status_code == 422


def test_category_name_validates_length(client):
    response = client.post("/categories", json={"name": "x" * 33, "parent_id": None})

    assert response.status_code == 422


def test_create_nested_categories_and_assign_task(client):
    work = create_category(client, "Work")
    project = create_category(client, "Project", work["id"])

    task = create_task(client, "Categorized", 5, 5, 3, project["id"])

    assert task["category_id"] == project["id"]
    categories = client.get("/categories").json()
    assert [category["name"] for category in categories] == ["Work", "Project"]


def test_rejects_invalid_task_category(client):
    response = client.post(
        "/tasks",
        json={
            "title": "Bad category",
            "importance": 5,
            "urgency": 5,
            "difficulty": 5,
            "time_estimate_minutes": 30,
            "category_id": 999,
        },
    )

    assert response.status_code == 422


def test_rename_duplicate_sibling_category_fails(client):
    parent = create_category(client, "Parent")
    first = create_category(client, "First", parent["id"])
    create_category(client, "Second", parent["id"])

    response = client.patch(f"/categories/{first['id']}", json={"name": "Second"})

    assert response.status_code == 409


def test_move_category_rejects_descendant_parent(client):
    parent = create_category(client, "Parent")
    child = create_category(client, "Child", parent["id"])

    response = client.patch(f"/categories/{parent['id']}", json={"parent_id": child["id"]})

    assert response.status_code == 422


def test_reorder_category_within_siblings(client):
    first = create_category(client, "First")
    second = create_category(client, "Second")
    third = create_category(client, "Third")

    response = client.patch(f"/categories/{third['id']}", json={"parent_id": None, "sort_order": 0})

    assert response.status_code == 200
    categories = client.get("/categories").json()
    top_level = [category for category in categories if category["parent_id"] is None]
    assert [category["id"] for category in top_level] == [third["id"], first["id"], second["id"]]


def test_category_delete_preview_counts_active_tasks_in_descendants(client):
    parent = create_category(client, "Parent")
    child = create_category(client, "Child", parent["id"])
    create_task(client, "Direct", 5, 5, 3, parent["id"])
    create_task(client, "Nested", 5, 5, 3, child["id"])

    preview = client.get(f"/categories/{parent['id']}/delete-preview")

    assert preview.status_code == 200
    assert preview.json()["active_task_count"] == 2
    assert preview.json()["category_count"] == 2


def test_deleting_category_moves_active_tasks_to_uncategorized(client):
    parent = create_category(client, "Parent")
    child = create_category(client, "Child", parent["id"])
    create_task(client, "Nested", 5, 5, 3, child["id"])

    response = client.delete(f"/categories/{parent['id']}")

    assert response.status_code == 204
    assert client.get("/categories").json() == []
    tasks = client.get("/tasks?status=active").json()
    assert tasks[0]["category_id"] is None


def test_archived_task_keeps_category_snapshot_after_category_delete(client):
    parent = create_category(client, "Parent")
    child = create_category(client, "Child", parent["id"])
    task = create_task(client, "Done", 5, 5, 3, child["id"])
    client.post("/tasks/pull", json={"energy_level": 5})
    client.post(f"/tasks/{task['id']}/finish")

    response = client.delete(f"/categories/{parent['id']}")

    assert response.status_code == 204
    archived = client.get("/tasks?status=archived").json()
    assert archived[0]["category_id"] is None
    assert archived[0]["category_snapshot"] == "Parent / Child"
