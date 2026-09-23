"""Expiring isolated accounts; deleting records never refunds provider attempts."""
import hashlib
import hmac
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

IDLE_SECONDS = 2 * 60 * 60
MAX_SECONDS = 24 * 60 * 60
GUEST_HEADER = "X-Fitness-Guest"


def tab_key(request):
    value = request.headers.get(GUEST_HEADER, "")
    return value if re.fullmatch(r"[a-f0-9]{64}", value) else ""


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def delete_guest(connection, user_id):
    if not connection.execute("SELECT 1 FROM guest_accounts WHERE user_id=?", (user_id,)).fetchone():
        return
    # guest_usage is aggregate accounting, deliberately not linked by a user FK.
    connection.execute("DELETE FROM ai_usage WHERE user_id=?", (user_id,))
    connection.execute("DELETE FROM users WHERE id=?", (user_id,))


def cleanup(database, *, now=None):
    now = int(time.time()) if now is None else now
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute("SELECT user_id FROM guest_accounts WHERE expires_at<=?", (now,)).fetchall()
        for row in rows:
            delete_guest(connection, row["user_id"])
        cutoff = (datetime.fromtimestamp(now, timezone.utc).date() - timedelta(days=6)).isoformat()
        connection.execute("DELETE FROM guest_usage WHERE day<?", (cutoff,))
    return len(rows)


def create_guest(request):
    if not request.app.state.settings.guest_enabled:
        raise HTTPException(403, "游客体验暂未开放，请登录或注册")
    key = tab_key(request)
    if not key:
        raise HTTPException(422, "请从游客体验入口重新开始")
    address = request.client.host if request.client else "unknown"
    request.app.state.access_guard.reserve_guest(address)
    database = request.app.state.database
    cleanup(database)
    now = int(time.time())
    with database.connect() as connection:
        # Keep late guest writes separate from newly allocated permanent account IDs.
        user_id = -(secrets.randbelow(2**52 - 1) + 1)
        connection.execute("INSERT INTO users(id,username,password_hash) VALUES (?,?,?)",
                           (user_id, "~guest_" + secrets.token_hex(16), "!guest-no-password"))
        connection.execute("INSERT INTO guest_accounts VALUES (?,?,?,?,?)",
                           (user_id, digest(key), digest(address), now, now + IDLE_SECONDS))
    return {"id": user_id, "username": "游客", "is_guest": True,
            "expires_at": now + IDLE_SECONDS, "max_expires_at": now + MAX_SECONDS}


def check_guest(connection, user_id, request, now):
    row = connection.execute("SELECT * FROM guest_accounts WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return None
    if not request.app.state.settings.guest_enabled or row["expires_at"] <= now:
        raise HTTPException(401, "游客体验已结束，请重新进入；临时记录不会保留")
    key = tab_key(request)
    if not key or not hmac.compare_digest(row["tab_hash"], digest(key)):
        raise HTTPException(401, "请从当前页面重新进入游客体验")
    expires = min(now + IDLE_SECONDS, row["created_at"] + MAX_SECONDS)
    connection.execute("UPDATE guest_accounts SET expires_at=? WHERE user_id=?", (expires, user_id))
    connection.execute("UPDATE sessions SET expires_at=? WHERE user_id=?", (expires, user_id))
    return {"id": user_id, "username": "游客", "is_guest": True,
            "expires_at": expires, "max_expires_at": row["created_at"] + MAX_SECONDS}
