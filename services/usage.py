from datetime import datetime, timedelta, timezone

from fastapi import HTTPException


def usage_status(database, settings, user_id):
    today = datetime.now(timezone.utc).date()
    with database.connect() as connection:
        rows = connection.execute("SELECT user_id,calls FROM ai_usage WHERE day=?", (today.isoformat(),)).fetchall()
    own = next((row["calls"] for row in rows if row["user_id"] == user_id), 0)
    enabled = settings.ai_user_daily_limit > 0 and settings.ai_global_daily_limit > 0
    return {"day": today.isoformat(), "quota_timezone": "UTC", "used": own,
            "limit": settings.ai_user_daily_limit, "remaining": max(0, settings.ai_user_daily_limit - own),
            "shared_available": sum(row["calls"] for row in rows) < settings.ai_global_daily_limit,
            "enabled": enabled, "resets_at": f"{today + timedelta(days=1)}T00:00:00+00:00"}


def reserve_call(database, settings, user_id, *, count=1):
    """Count attempts before contacting the provider; failures are not retried/refunded."""
    today = datetime.now(timezone.utc).date()
    day = today.isoformat()
    if type(count) is not int or not 1 <= count <= 6:
        raise ValueError("Invalid call reservation")
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute("SELECT user_id,calls FROM ai_usage WHERE day=?", (day,)).fetchall()
        total = sum(row["calls"] for row in rows)
        own = next((row["calls"] for row in rows if row["user_id"] == user_id), 0)
        user_remaining = max(0, settings.ai_user_daily_limit - own)
        global_remaining = max(0, settings.ai_global_daily_limit - total)
        if count > user_remaining or count > global_remaining:
            if settings.ai_user_daily_limit == 0 or settings.ai_global_daily_limit == 0:
                message = "本应用已关闭付费模型调用，请联系管理者调整设置；手动记录仍可使用。"
            else:
                quota = (f"个人额度剩余{user_remaining}次（每日上限{settings.ai_user_daily_limit}次）"
                         if count > user_remaining else "共享调用额度不足")
                message = (f"本应用今日AI调用额度不足：{quota}，本次需要{count}次。"
                           f"北京时间{today + timedelta(days=1)} 08:00恢复；不是供应商余额提示，手动记录仍可使用。")
            raise HTTPException(429, message)
        connection.execute(
            "INSERT INTO ai_usage(day,user_id,calls) VALUES (?,?,?) "
            "ON CONFLICT(day,user_id) DO UPDATE SET calls=calls+excluded.calls", (day, user_id, count)
        )
