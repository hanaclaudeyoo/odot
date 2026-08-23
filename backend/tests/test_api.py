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


def create_task(
    client: TestClient,
    title: str,
    importance: float,
    urgency: float,
    difficulty: float,
    category_id: int | None = None,
    deadline_at: str | None = None,
):
    payload = {
        "title": title,
        "importance": importance,
        "urgency": urgency,
        "difficulty": difficulty,
        "time_estimate_minutes": 30,
    }
    if category_id is not None:
        payload["category_id"] = category_id
    if deadline_at is not None:
        payload["deadline_at"] = deadline_at
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
            "urgency": 3,
            "difficulty": 2,
            "time_estimate_minutes": 15,
        },
    )

    assert response.status_code == 422


def test_create_task_accepts_decimal_axes(client):
    task = create_task(client, "Decimal task", 5.67, 4.32, 8.91)

    assert task["importance"] == 5.67
    assert task["urgency"] == 4.32
    assert task["base_urgency"] == 4.32
    assert task["difficulty"] == 8.91


def test_task_deadline_is_stored_and_raises_effective_urgency(client, monkeypatch):
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
    assert task["base_urgency"] == 2
    assert task["urgency"] > 2
    assert task["urgency"] <= 10


def test_completed_task_uses_base_urgency_after_deadline(client, monkeypatch):
    import app.main as main

    current_time = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(main, "now_utc", lambda: current_time)
    task = create_task(
        client,
        "Complete before deadline",
        10,
        2,
        10,
        deadline_at=(current_time + timedelta(minutes=10)).isoformat(),
    )
    client.post(f"/tasks/{task['id']}/start")
    client.post(f"/tasks/{task['id']}/finish")

    monkeypatch.setattr(main, "now_utc", lambda: current_time + timedelta(days=1))
    archived = client.get("/tasks?status=archived").json()[0]

    assert archived["base_urgency"] == 2
    assert archived["urgency"] == 2


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
    create_task(client, "Important and urgent", 5, 5, 10)

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


def test_pull_blocks_when_session_exists(client):
    create_task(client, "First", 5, 5, 3)
    create_task(client, "Second", 5, 5, 3)

    assert client.post("/tasks/pull", json={"energy_level": 5}).status_code == 200
    assert client.post("/tasks/pull", json={"energy_level": 5}).status_code == 409


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
