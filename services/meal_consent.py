"""Reuse an explicit dietary attestation only for its authenticated login."""
import hashlib
import json
import time

from model.factory import ModelError
from security import COOKIE_NAME, token_hash
from services.plan_foods import CATALOG_VERSION
from services.business_time import business_today


class MealConsentService:
    def __init__(self, request, user):
        self.database = request.app.state.database
        self.user_id = user["id"]
        self.session_hash = token_hash(request.cookies.get(COOKIE_NAME, ""))

    def context(self, connection):
        session = connection.execute(
            "SELECT 1 FROM sessions WHERE token_hash=? AND user_id=? AND expires_at>?",
            (self.session_hash, self.user_id, int(time.time())),
        ).fetchone()
        if not session:
            raise ModelError("SESSION_EXPIRED", "请重新登录后核对饮食适用条件。", 401)
        row = connection.execute("SELECT payload FROM profiles WHERE user_id=?", (self.user_id,)).fetchone()
        profile = json.loads(row[0]) if row else {}
        fields = {key: profile.get(key, "") for key in ("food_allergies", "preferences")}
        fields["nutrition_reference"] = profile.get("nutrition_reference", "general")
        value = json.dumps({"terms": "daily-assistant-v2", "day": business_today().isoformat(),
                            "catalog": CATALOG_VERSION, **fields}, sort_keys=True)
        return hashlib.sha256(value.encode()).hexdigest()

    def get(self):
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            fingerprint = self.context(connection)
            row = connection.execute("SELECT context_hash FROM meal_consents WHERE session_hash=?", (self.session_hash,)).fetchone()
            saved = connection.execute("SELECT payload FROM profiles WHERE user_id=?", (self.user_id,)).fetchone()
            profile = json.loads(saved[0]) if saved else {}
            return {"confirmed": bool(row and row[0] == fingerprint), "context_hash": fingerprint,
                    "profile": {key: profile.get(key, "") for key in ("food_allergies", "preferences")}}

    def confirm(self, body):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            fingerprint = self.context(connection)
            if fingerprint != body.context_hash:
                raise ModelError("MEAL_CONFIRMATION_CHANGED", "档案或适用条件已变化，请刷新后重新核对。", 409)
            connection.execute(
                "INSERT INTO meal_consents(session_hash,context_hash) VALUES (?,?) "
                "ON CONFLICT(session_hash) DO UPDATE SET context_hash=excluded.context_hash,confirmed_at=CURRENT_TIMESTAMP",
                (self.session_hash, fingerprint),
            )
            return {"confirmed": True, "context_hash": fingerprint}

    def revoke(self):
        with self.database.connect() as connection:
            self.context(connection)
            connection.execute("DELETE FROM meal_consents WHERE session_hash=?", (self.session_hash,))
        return {"confirmed": False}

    def require(self):
        if not self.get()["confirmed"]:
            raise ModelError("MEAL_CONFIRMATION_REQUIRED", "请先完成本日助手适用确认；次日、相关档案变化或重新登录后需再次确认。", 409)
