import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request, Response
from services.guests import IDLE_SECONDS, check_guest, delete_guest

COOKIE_NAME = "fitness_session"


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=32768, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


DUMMY_HASH = hash_password("unused-login-timing-placeholder")


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, salt, _ = stored.split("$")
        return algorithm == "scrypt" and hmac.compare_digest(hash_password(password, bytes.fromhex(salt)), stored)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_session(request: Request, response: Response, user_id: int, *, guest=False):
    token = secrets.token_urlsafe(32)
    settings = request.app.state.settings
    with request.app.state.database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(time.time()),))
        previous = request.cookies.get(COOKIE_NAME)
        if previous:
            old = connection.execute("SELECT user_id FROM sessions WHERE token_hash=?", (token_hash(previous),)).fetchone()
            if old and old["user_id"] != user_id:
                delete_guest(connection, old["user_id"])
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(previous),))
        connection.execute(
            "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (token_hash(token), user_id, int(time.time()) + (IDLE_SECONDS if guest else settings.session_seconds)),
        )
    response.set_cookie(
        COOKIE_NAME, token, max_age=None if guest else settings.session_seconds, httponly=True,
        secure=settings.cookie_secure, samesite="strict", path="/",
    )


def current_user(request: Request) -> dict:
    token = request.cookies.get(COOKIE_NAME, "")
    if not token or len(token) > 128:
        raise HTTPException(401, "请先登录")
    with request.app.state.database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT users.id, users.username FROM sessions JOIN users ON users.id = sessions.user_id "
            "WHERE sessions.token_hash = ? AND sessions.expires_at > ?",
            (token_hash(token), int(time.time())),
        ).fetchone()
        if row is None:
            raise HTTPException(401, "登录已过期，请重新登录")
        guest = check_guest(connection, row["id"], request, int(time.time()))
    if guest:
        return guest
    return dict(row)
