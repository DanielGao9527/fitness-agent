from datetime import datetime, timedelta, timezone
import time

from fastapi import HTTPException


def shared_calls(connection, day):
    regular = connection.execute("SELECT COALESCE(SUM(a.calls),0) FROM ai_usage a LEFT JOIN guest_accounts g "
                                 "ON a.user_id=g.user_id WHERE a.day=? AND g.user_id IS NULL", (day,)).fetchone()[0]
    guest_total = connection.execute("SELECT COALESCE(SUM(calls),0) FROM guest_usage WHERE day=?", (day,)).fetchone()[0]
    return regular + guest_total


def counts(connection, settings, user_id, day):
    if not connection.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
        raise HTTPException(401, "会话已结束，请重新登录或开始体验")
    guest = connection.execute("SELECT client_hash,expires_at FROM guest_accounts WHERE user_id=?", (user_id,)).fetchone()
    if guest and (guest["expires_at"] <= int(time.time()) or not settings.guest_enabled):
        raise HTTPException(401, "游客体验已结束，请重新进入")
    if guest:
        own = connection.execute("SELECT calls FROM guest_usage WHERE day=? AND client_hash=?", (day, guest["client_hash"])).fetchone()
    else:
        own = connection.execute("SELECT calls FROM ai_usage WHERE day=? AND user_id=?", (day, user_id)).fetchone()
    limit = min(settings.ai_user_daily_limit, settings.guest_ai_daily_limit) if guest else settings.ai_user_daily_limit
    return (own[0] if own else 0), shared_calls(connection, day), limit, guest


def usage_status(database, settings, user_id):
    today = datetime.now(timezone.utc).date()
    with database.connect() as connection:
        connection.execute("BEGIN")
        own, total, limit, guest = counts(connection, settings, user_id, today.isoformat())
    enabled = limit > 0 and settings.ai_global_daily_limit > 0
    return {"day": today.isoformat(), "quota_timezone": "UTC", "used": own,
            "limit": limit, "remaining": max(0, limit - own),
            "scope": "guest_network" if guest else "account",
            "shared_available": total < settings.ai_global_daily_limit,
            "enabled": enabled, "resets_at": f"{today + timedelta(days=1)}T00:00:00+00:00"}


def reserve_call(database, settings, user_id, *, count=1):
    """Count attempts before contacting the provider; failures are not retried/refunded."""
    today = datetime.now(timezone.utc).date()
    day = today.isoformat()
    if type(count) is not int or not 1 <= count <= 6:
        raise ValueError("Invalid call reservation")
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        own, total, limit, guest = counts(connection, settings, user_id, day)
        user_remaining = max(0, limit - own)
        global_remaining = max(0, settings.ai_global_daily_limit - total)
        if count > user_remaining or count > global_remaining:
            if limit == 0 or settings.ai_global_daily_limit == 0:
                message = "本应用已关闭付费模型调用，请联系管理者调整设置；手动记录仍可使用。"
            else:
                label = "当前网络的游客额度" if guest else "个人额度"
                quota = (f"{label}剩余{user_remaining}次（每日上限{limit}次）"
                         if count > user_remaining else "共享调用额度不足")
                message = (f"本应用今日AI调用额度不足：{quota}，本次需要{count}次。"
                           f"北京时间{today + timedelta(days=1)} 08:00恢复；不是供应商余额提示，手动记录仍可使用。")
            raise HTTPException(429, message)
        connection.execute(
            "INSERT INTO ai_usage(day,user_id,calls) VALUES (?,?,?) "
            "ON CONFLICT(day,user_id) DO UPDATE SET calls=calls+excluded.calls", (day, user_id, count)
        )
        if guest:
            connection.execute("INSERT INTO guest_usage(day,client_hash,calls) VALUES (?,?,?) "
                               "ON CONFLICT(day,client_hash) DO UPDATE SET calls=calls+excluded.calls",
                               (day, guest["client_hash"], count))
