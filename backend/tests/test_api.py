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


def create_task(client: TestClient, title: str, importance: int, urgency: int, difficulty: int):
    response = client.post(
        "/tasks",
        json={
            "title": title,
            "importance": importance,
            "urgency": urgency,
            "difficulty": difficulty,
            "time_estimate_minutes": 30,
        },
    )
    assert response.status_code == 201
    return response.json()


def test_create_task_requires_valid_axes(client):
    response = client.post(
        "/tasks",
        json={
            "title": "Invalid task",
            "importance": 8,
            "urgency": 3,
            "difficulty": 2,
            "time_estimate_minutes": 15,
        },
    )

    assert response.status_code == 422


def test_lists_active_and_archived_tasks(client):
    task = create_task(client, "Write draft", 5, 5, 3)
    client.post("/tasks/pull", json={"energy_level": 4})
    client.post(f"/tasks/{task['id']}/finish")

    active = client.get("/tasks?status=active")
    archived = client.get("/tasks?status=archived")

    assert active.json() == []
    assert archived.json()[0]["title"] == "Write draft"
    assert archived.json()[0]["actual_duration_seconds"] is not None


def test_pull_respects_quadrant_priority(client):
    create_task(client, "Low everything", 1, 1, 1)
    create_task(client, "Urgent only", 2, 7, 1)
    create_task(client, "Important and urgent", 4, 4, 7)

    response = client.post("/tasks/pull", json={"energy_level": 1})

    assert response.status_code == 200
    assert response.json()["task"]["title"] == "Important and urgent"


def test_scoring_uses_continuous_urgency_and_importance_values():
    from app.scoring import task_score

    base = {"difficulty": 4}
    assert task_score({**base, "urgency": 7, "importance": 5}, 4) > task_score(
        {**base, "urgency": 6, "importance": 5}, 4
    )
    assert task_score({**base, "urgency": 2, "importance": 3}, 4) > task_score(
        {**base, "urgency": 2, "importance": 2}, 4
    )


def test_high_energy_prioritizes_difficult_task_when_other_axes_match(client):
    create_task(client, "Easy peer", 4, 4, 1)
    create_task(client, "Hard peer", 4, 4, 7)

    response = client.post("/tasks/pull", json={"energy_level": 7})

    assert response.json()["task"]["title"] == "Hard peer"


def test_low_energy_favors_easy_task_when_other_axes_match(client):
    create_task(client, "Easy peer", 4, 4, 1)
    create_task(client, "Hard peer", 4, 4, 7)

    response = client.post("/tasks/pull", json={"energy_level": 1})

    assert response.json()["task"]["title"] == "Easy peer"


def test_pull_blocks_when_session_exists(client):
    create_task(client, "First", 5, 5, 3)
    create_task(client, "Second", 5, 5, 3)

    assert client.post("/tasks/pull", json={"energy_level": 4}).status_code == 200
    assert client.post("/tasks/pull", json={"energy_level": 4}).status_code == 409


def test_decline_update_clears_session_before_five_minutes(client):
    task = create_task(client, "Original", 5, 5, 3)
    client.post("/tasks/pull", json={"energy_level": 4})

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
    client.post("/tasks/pull", json={"energy_level": 4})

    response = client.post("/session/decline-edit", json={"action": "delete"})

    assert response.status_code == 200
    assert response.json() is None
    assert client.get("/session").json() is None
    assert client.get("/tasks?status=active").json() == []


def test_decline_fails_after_five_minutes(client, monkeypatch):
    create_task(client, "Expired decline", 5, 5, 3)
    session = client.post("/tasks/pull", json={"energy_level": 4}).json()
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
    pulled = client.post("/tasks/pull", json={"energy_level": 4}).json()

    session = client.get("/session")

    assert session.status_code == 200
    assert session.json()["started_at"] == pulled["started_at"]
    assert session.json()["task"]["title"] == "Resume me"


def test_energy_level_must_be_one_through_seven(client):
    create_task(client, "Energy", 5, 5, 3)

    response = client.post("/tasks/pull", json={"energy_level": 0})

    assert response.status_code == 422
