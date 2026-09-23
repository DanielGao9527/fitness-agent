"""Persistent, bounded login/register throttles; no raw names, IPs or passwords."""
import hashlib
import sqlite3
import time
from contextlib import closing

from fastapi import HTTPException

WINDOW = 15 * 60


class AccessGuard:
    def __init__(self, database_path):
        self.path = database_path.with_name(database_path.name + ".access.sqlite3")

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS attempts (bucket TEXT PRIMARY KEY, started INTEGER NOT NULL, attempts INTEGER NOT NULL)")

    def reserve(self, address, username):
        now = int(time.time())
        keys = [(hashlib.sha256(("ip:" + address).encode()).hexdigest(), 100),
                (hashlib.sha256(("account:" + username.lower()).encode()).hexdigest(), 20)]
        with closing(sqlite3.connect(self.path, timeout=10)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM attempts WHERE started <= ?", (now - WINDOW,))
            for key, limit in keys:
                old = connection.execute("SELECT started,attempts FROM attempts WHERE bucket=?", (key,)).fetchone()
                if old and old[1] >= limit:
                    raise HTTPException(429, "登录或注册尝试过于频繁，请稍后再试", headers={"Retry-After": str(max(1, old[0] + WINDOW - now))})
            for key, _ in keys:
                connection.execute("INSERT INTO attempts VALUES (?,?,1) ON CONFLICT(bucket) DO UPDATE SET attempts=attempts+1", (key, now))


def reserve_auth(request, username):
    address = request.client.host if request.client else "unknown"
    request.app.state.access_guard.reserve(address, username)
