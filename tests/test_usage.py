from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from config import Settings
from services import usage
from test_foundation import application, client, register


def counters(application):
    with application.state.database.connect() as connection:
        return [tuple(row) for row in connection.execute("SELECT day,user_id,calls FROM ai_usage ORDER BY day,user_id")]


def test_defaults_and_explicit_override(tmp_path, monkeypatch):
    monkeypatch.delenv("FITNESS_AI_USER_DAILY_LIMIT", raising=False)
    monkeypatch.delenv("FITNESS_AI_GLOBAL_DAILY_LIMIT", raising=False)
    assert Settings(database_path=tmp_path / "db").ai_user_daily_limit == 100
    assert Settings.from_env().ai_user_daily_limit == Settings.from_env().ai_global_daily_limit == 100
    monkeypatch.setenv("FITNESS_AI_USER_DAILY_LIMIT", "7")
    assert Settings.from_env().ai_user_daily_limit == 7
    monkeypatch.setenv("FITNESS_AI_USER_DAILY_LIMIT", "-1")
    assert Settings.from_env().ai_user_daily_limit == 0


def test_existing_count_remains_when_limit_increases(client, application):
    user = register(client)
    settings = replace(application.state.settings, ai_user_daily_limit=30)
    for _ in range(5):
        usage.reserve_call(application.state.database, settings, user["id"], count=6)
    with pytest.raises(HTTPException) as error:
        usage.reserve_call(application.state.database, settings, user["id"])
    assert "个人额度剩余0次" in error.value.detail and "每日上限30次" in error.value.detail
    assert "08:00" in error.value.detail and "不是供应商余额" in error.value.detail
    usage.reserve_call(application.state.database, replace(settings, ai_user_daily_limit=100), user["id"])
    assert counters(application)[0][2] == 31


def test_insufficient_batch_does_not_consume_remaining(client, application):
    user = register(client)
    settings = replace(application.state.settings, ai_user_daily_limit=4)
    usage.reserve_call(application.state.database, settings, user["id"], count=3)
    with pytest.raises(HTTPException) as error:
        usage.reserve_call(application.state.database, settings, user["id"], count=2)
    assert "剩余1次" in error.value.detail and "本次需要2次" in error.value.detail
    assert counters(application)[0][2] == 3


def test_shared_budget_is_preserved(client, application):
    first = register(client)
    settings = replace(application.state.settings, ai_global_daily_limit=1)
    usage.reserve_call(application.state.database, settings, first["id"])
    second = register(client, "bob")
    with pytest.raises(HTTPException) as error:
        usage.reserve_call(application.state.database, settings, second["id"])
    assert "共享调用额度不足" in error.value.detail
    assert len(counters(application)) == 1


@pytest.mark.parametrize("field", ["ai_user_daily_limit", "ai_global_daily_limit"])
def test_zero_explicitly_disables(client, application, field):
    user = register(client)
    with pytest.raises(HTTPException) as error:
        usage.reserve_call(application.state.database, replace(application.state.settings, **{field: 0}), user["id"])
    assert "已关闭付费模型调用" in error.value.detail
    assert "恢复" not in error.value.detail
    assert counters(application) == []


@pytest.mark.parametrize("count", [0, 7, True, 1.0, "1"])
def test_invalid_counts_do_not_write(client, application, count):
    user = register(client)
    with pytest.raises(ValueError):
        usage.reserve_call(application.state.database, application.state.settings, user["id"], count=count)
    assert counters(application) == []


def test_utc_rollover_and_history_preserved(client, application, monkeypatch):
    class Clock:
        value = datetime(2026, 9, 19, 23, 59, 59, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz):
            return cls.value

    monkeypatch.setattr(usage, "datetime", Clock)
    user = register(client)
    settings = replace(application.state.settings, ai_user_daily_limit=1)
    usage.reserve_call(application.state.database, settings, user["id"])
    with pytest.raises(HTTPException) as error:
        usage.reserve_call(application.state.database, settings, user["id"])
    assert "2026-09-20 08:00" in error.value.detail
    Clock.value = datetime(2026, 9, 20, tzinfo=timezone.utc)
    usage.reserve_call(application.state.database, settings, user["id"])
    assert [row[0] for row in counters(application)] == ["2026-09-19", "2026-09-20"]


def test_concurrent_reservations_respect_limit(client, application):
    user = register(client)
    settings = replace(application.state.settings, ai_user_daily_limit=3)

    def attempt(_):
        try:
            usage.reserve_call(application.state.database, settings, user["id"])
            return True
        except HTTPException as error:
            assert error.status_code == 429
            return False

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(attempt, range(10))) == 3
    assert counters(application)[0][2] == 3
