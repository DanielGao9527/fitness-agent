import pytest
from fastapi.testclient import TestClient

from app import create_app
from test_foundation import DAY, application, client, register, workout
from test_workouts import model, preview, record
from test_knowledge import library
from test_training_plans import plans, generate, accept


def week(client, day=DAY):
    response = client.get("/api/workouts/week", params={"day": day})
    assert response.status_code == 200, response.text
    return response.json()


def test_authentication_required(client):
    assert client.get("/api/workouts/week", params={"day": DAY}).status_code == 401


@pytest.mark.parametrize("day", ["", "not-a-day", "2026-02-30", "10000-01-01", "2026-09-15T12:00:00"])
def test_invalid_date(client, day):
    register(client)
    assert client.get("/api/workouts/week", params={"day": day}).status_code == 422


@pytest.mark.parametrize("day,start,end,size", [
    ("2026-09-15", "2026-09-14", "2026-09-20", 7),
    ("2026-09-20", "2026-09-14", "2026-09-20", 7),
    ("2025-12-31", "2025-12-29", "2026-01-04", 7),
    ("2024-02-29", "2024-02-26", "2024-03-03", 7),
    ("0001-01-01", "0001-01-01", "0001-01-07", 7),
    ("9999-12-31", "9999-12-27", "9999-12-31", 5),
])
def test_week_boundaries_and_empty_days(client, day, start, end, size):
    register(client)
    result = week(client, day)
    assert (result["start"], result["end"], len(result["days"])) == (start, end, size)
    assert result["selected_day"] == day and result["basis"] == "saved_records_only"
    assert all(row["workout_count"] == 0 for row in result["days"])
    assert all(row["estimated_workout_calories"]["lower_total"] is None for row in result["days"])
    assert result["totals"]["completed_days"] == result["totals"]["planned_days"] == 0


def test_plan_actual_separation_and_summary_agree(client, application, model):
    register(client)
    p = preview(client)
    rows = [record(p, day="2026-09-14"), workout(day=DAY, status="completed", minutes=45),
            workout(day=DAY, status="planned", minutes=60), record(p, day="2026-09-20", status="planned"),
            workout(day="2026-09-13", status="completed", minutes=100),
            workout(day="2026-09-21", status="completed", minutes=100)]
    for row in rows:
        response = client.post("/api/workouts", json=row)
        assert response.status_code == 201, response.text
    before_calls = len(model.calls)
    result = week(client)
    totals = result["totals"]
    assert totals["completed_count"] == totals["planned_count"] == 2
    assert totals["completed_minutes"] == 75 and totals["planned_minutes"] == 90
    assert totals["completed_days"] == totals["planned_days"] == 2
    assert totals["estimated_workout_calories"] == {
        "lower_total": 90, "upper_total": 150, "count": 1, "unknown_count": 1, "basis": "gross_activity"}
    for row in result["days"]:
        summary = client.get("/api/summary", params={"day": row["day"]}).json()
        assert all(row[key] == summary[key] for key in row if key != "day")
    assert len(model.calls) == before_calls


def test_cross_user_and_restart_no_key(client, application):
    owner = register(client)
    assert client.post("/api/workouts", json=workout(status="completed")).status_code == 201
    expected = week(client)
    with TestClient(create_app(application.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert week(restarted) == expected
        restarted.cookies.clear()
        register(restarted, "bob")
        assert week(restarted)["totals"]["workout_count"] == 0
        assert restarted.get("/api/workouts/week", params={"day": DAY, "user_id": owner["id"]}).json()["totals"]["workout_count"] == 0
    with application.state.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_usage").fetchone()[0] == 0


def test_edit_move_complete_delete_updates_week(client):
    register(client)
    body = workout(status="planned")
    created = client.post("/api/workouts", json=body).json()
    assert week(client)["totals"]["planned_minutes"] == 30
    values = {key: value for key, value in created.items() if key != "id"}
    changed = {**values, "day": "2026-09-22", "status": "completed", "minutes": 20}
    assert client.put(f"/api/workouts/{created['id']}", json=changed).status_code == 200
    assert week(client)["totals"]["workout_count"] == 0
    assert week(client, "2026-09-22")["totals"]["completed_minutes"] == 20
    assert client.delete(f"/api/workouts/{created['id']}").status_code == 204
    assert week(client, "2026-09-22")["totals"]["workout_count"] == 0


def test_unestimated_completed_is_not_zero_or_planned(client):
    register(client)
    assert client.post("/api/workouts", json=workout(status="completed")).status_code == 201
    totals = week(client)["totals"]
    assert totals["completed_minutes"] == 30 and totals["planned_count"] == 0
    assert totals["estimated_workout_calories"]["unknown_count"] == 1
    assert totals["estimated_workout_calories"]["lower_total"] is None


def test_read_does_not_write_and_batch_replay_not_double_count(client, application):
    register(client)
    body = {"items": [workout(name="Chest", status="completed"), workout(name="Shoulder", status="planned")]}
    assert client.post("/api/workouts/batch", json=body).status_code == 201
    assert client.post("/api/workouts/batch", json=body).status_code == 201
    with application.state.database.connect() as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        before = {table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')] for table in tables}
    for _ in range(3):
        assert week(client)["totals"]["workout_count"] == 2
    with application.state.database.connect() as connection:
        after = {table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')] for table in tables}
    assert before == after


def test_suggestions_and_execution_drafts_are_not_actual_or_manual_plans(plans):
    client, application, model, _ = plans
    generated = generate(client).json()
    assert accept(client, generated).status_code == 200
    assert client.post(f"/api/training-plans/{generated['id']}/execution", json={"reviewed": True}).status_code == 200
    result = week(client)
    assert result["totals"]["workout_count"] == 0
    assert result["totals"]["completed_minutes"] == result["totals"]["planned_minutes"] == 0
    assert len(model.messages) == 1
