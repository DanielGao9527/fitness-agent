import json
import re
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import ValidationError

from config import ROOT
from model.factory import ModelError
from rag.rag_service import KnowledgeUnavailable
from schemas import TrainingPlanRequest, TrainingPlanSelection
from services.profile_context import read_profile
from services.meal_plans import digest, encode
from services.coach import plan_binding, require_plan_binding
from services.training_context import recent_training

REQUIRED = ("cdc-activities:moderate", "cdc-start:gradual", "cdc-intensity:talk-test", "nia-activity:preparation")
POLICY = "2026-09-19.3"
ACTIVITIES = {"walk": "平地步行", "cycle": "平地骑行"}
HEALTH = re.compile(r"伤|疼|痛|病|不舒服|不适|肿胀|麻木|孕|哺乳|术后|手术|康复|头晕|胸闷|心脏|血压|血糖|哮喘|癫痫|服药|用药|处方|残疾|行动不便|未成年|儿童|青少年|老人|老年|(?<!\d)(?:[0-9]|1[0-7]|6[5-9]|[7-9]\d|\d{3})\s*岁|injur|pain|pregnan|disease|surgery|dizz|diabet|asthma|cardiac|blood.pressure|child|teen|elder", re.I)


def limits(facts, body):
    profile = facts["profile"]
    legacy = profile["preferences"] + " " + profile["food_allergies"]
    if HEALTH.search(legacy):
        raise ModelError("TRAINING_SCOPE", "本产品仅面向无伤病的一般成人健身，不提供医疗、伤病或康复训练方案。", 409)
    if any(row["status"] == "planned" for row in facts["workouts"]):
        raise ModelError("TRAINING_PENDING", "这一天已有尚未完成的训练记录，请先核对原安排，避免重复增加训练。", 409)
    completed = sum(row["minutes"] for row in facts["workouts"] if row["status"] == "completed")
    available = body.daily_minutes - completed if body.time_basis == "daily" else body.daily_minutes
    maximum = min(available - 10, 10 if profile["experience"] == "beginner" else 30)
    if maximum < 5:
        raise ModelError("TRAINING_TIME", "扣除已完成训练后，剩余时间不足以容纳当前基础安排，未追加训练。", 409)
    allowed = ["walk"] + (["cycle"] if body.bicycle_available else [])
    if body.activity != "either":
        allowed = [key for key in allowed if key == body.activity]
    if not allowed:
        raise ModelError("TRAINING_EQUIPMENT", "平地骑行需要确认可用自行车和安全骑行条件。", 409)
    return completed, maximum, allowed


class TrainingPlanService:
    def __init__(self, database, user_id, retriever):
        self.database, self.user_id, self.retriever = database, user_id, retriever

    def evidence(self):
        found = {hit.chunk_id: hit.model_dump(mode="json") for hit in self.retriever.get_chunks(REQUIRED, topic="training")}
        if any(key not in found for key in REQUIRED):
            raise ModelError("TRAINING_EVIDENCE", "训练所需依据缺失、已撤回或待复核，暂不能生成或采纳。", 503)
        return [found[key] for key in REQUIRED]

    def snapshot(self, connection, day, sources, original=None):
        profile = read_profile(connection, self.user_id)
        rows = connection.execute("SELECT id,payload FROM workouts WHERE user_id=? AND day=? ORDER BY id", (self.user_id, day)).fetchall()
        facts = {"profile": profile, "workouts": [{"id": row["id"], **json.loads(row["payload"])} for row in rows]}
        binding = plan_binding(connection, self.user_id, original, day, "training")
        if binding is not None:
            facts["coach"] = binding
            facts["recent_training"] = recent_training(connection, self.user_id, day)
        latest = connection.execute("SELECT COALESCE(MAX(version),0) FROM training_plans WHERE user_id=? AND day=?", (self.user_id, day)).fetchone()[0]
        return facts, digest({"facts": facts, "sources": sources, "policy": POLICY, "activities": ACTIVITIES}), latest

    @staticmethod
    def view(row, fingerprint, latest):
        payload = json.loads(row["payload"])
        original = json.loads(row["input_payload"])
        stale = row["context_hash"] != fingerprint or (row["status"] == "draft" and payload.get("base_version") != latest)
        return {**payload, "id": row["id"], "day": row["day"], "status": row["status"], "version": row["version"],
                "coach_id": original.get("coach_id"), "coach_version": original.get("coach_version"),
                "stale": stale, "current": row["status"] == "accepted" and row["version"] == latest and not stale,
                "created_at": row["created_at"], "accepted_at": row["accepted_at"]}

    def list(self, day):
        try:
            sources = self.evidence()
        except (ModelError, KnowledgeUnavailable):
            sources = None
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            rows = connection.execute("SELECT * FROM training_plans WHERE user_id=? AND day=? AND status IN ('draft','accepted') ORDER BY rowid DESC LIMIT 20", (self.user_id, day)).fetchall()
            result = []
            for row in rows:
                _, fingerprint, latest = self.snapshot(connection, day, sources, json.loads(row["input_payload"]))
                result.append(self.view(row, fingerprint if sources else None, latest))
            return result

    @staticmethod
    def validate(selection, maximum, allowed):
        if selection.activity_id not in allowed or selection.main_minutes > maximum or set(selection.chunk_ids) != set(REQUIRED):
            raise ModelError("MODEL_INVALID_OUTPUT", "训练项目、时长或依据未通过核对，未显示或采纳。")

    def generate(self, body, model_factory, model_name):
        day, original = body.day.isoformat(), encode(body.model_dump(mode="json"))
        sources = self.evidence()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            facts, fingerprint, version = self.snapshot(connection, day, sources, body.model_dump(mode="json"))
            require_plan_binding(facts)
            row = connection.execute("SELECT * FROM training_plans WHERE user_id=? AND client_id=?", (self.user_id, str(body.client_id))).fetchone()
            if row:
                if TrainingPlanRequest.model_validate_json(row["input_payload"]).model_dump(mode="json") != body.model_dump(mode="json"):
                    raise ModelError("PLAN_REQUEST_CONFLICT", "请求内容已改变，请重新生成。", 409)
                if row["status"] in ("draft", "accepted"):
                    return self.view(row, fingerprint, version)
                raise ModelError("PLAN_REQUEST_PENDING", "这次请求正在处理或未完成，可刷新查看；不会自动重复调用模型。", 409)
            completed, maximum, allowed = limits(facts, body)
            plan_id = str(uuid4())
            connection.execute("INSERT INTO training_plans(id,user_id,client_id,day,input_payload,context_hash,status) VALUES (?,?,?,?,?,?,'generating')", (plan_id, self.user_id, str(body.client_id), day, original, fingerprint))
        try:
            message = {"day": day, "profile": {key: facts["profile"][key] for key in ("goal", "experience")},
                       "daily_minutes": body.daily_minutes, "time_basis": body.time_basis, "completed_minutes": completed, "max_main_minutes": maximum,
                       "allowed_activities": [{"id": key, "name": ACTIVITIES[key]} for key in allowed],
                       "evidence": [{key: hit[key] for key in ("chunk_id", "title", "excerpt", "scope")} for hit in sources]}
            prompt = (ROOT / "prompts/training_plan.md").read_text(encoding="utf-8")
            prompt += "\nJSON Schema:\n" + json.dumps(TrainingPlanSelection.model_json_schema(), ensure_ascii=False)
            raw = model_factory().generate(system_prompt=prompt, message=encode(message))
            try:
                selection = TrainingPlanSelection.model_validate_json(raw)
            except ValidationError:
                raise ModelError("MODEL_INVALID_OUTPUT", "训练建议格式未通过核对，未显示或采纳。") from None
            self.validate(selection, maximum, allowed)
            current_sources = self.evidence()
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current, current_hash, latest = self.snapshot(connection, day, current_sources, body.model_dump(mode="json"))
                if current_hash != fingerprint or latest != version:
                    raise ModelError("PLAN_CONTEXT_CHANGED", "生成期间档案、训练记录、已采纳版本或依据改变，请重新核对生成。", 409)
                limits(current, body)
                payload = {**selection.model_dump(), "activity_name": ACTIVITIES[selection.activity_id],
                           "warmup_minutes": 5, "cooldown_minutes": 5, "total_minutes": selection.main_minutes + 10,
                           "daily_minutes": body.daily_minutes, "completed_minutes": completed,
                           "time_basis": body.time_basis,
                           "remaining_minutes": body.daily_minutes - completed if body.time_basis == "daily" else body.daily_minutes, "bicycle_available": body.bicycle_available,
                           "request_context": facts.get("coach", {}).get("training_context", {}),
                           "sources": sources, "base_version": version, "model": model_name,
                           "generated_at": datetime.now(timezone.utc).isoformat()}
                connection.execute("UPDATE training_plans SET status='draft',payload=? WHERE id=? AND user_id=?", (encode(payload), plan_id, self.user_id))
                row = connection.execute("SELECT * FROM training_plans WHERE id=? AND user_id=?", (plan_id, self.user_id)).fetchone()
                return self.view(row, current_hash, latest)
        except Exception:
            with self.database.connect() as connection:
                connection.execute("UPDATE training_plans SET status='failed' WHERE id=? AND user_id=? AND status='generating'", (plan_id, self.user_id))
            raise

    def accept(self, plan_id):
        sources = self.evidence()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM training_plans WHERE id=? AND user_id=?", (plan_id, self.user_id)).fetchone()
            if not row:
                raise ModelError("PLAN_NOT_FOUND", "建议不存在或无权访问。", 404)
            facts, fingerprint, latest = self.snapshot(connection, row["day"], sources, json.loads(row["input_payload"]))
            view = self.view(row, fingerprint, latest)
            if row["status"] == "accepted":
                return view
            if row["status"] != "draft" or view["stale"]:
                raise ModelError("PLAN_CONTEXT_CHANGED", "草稿已失效，请按最新档案、训练记录和依据重新生成。", 409)
            body = TrainingPlanRequest.model_validate_json(row["input_payload"])
            require_plan_binding(facts)
            _, maximum, allowed = limits(facts, body)
            selection = TrainingPlanSelection(**{key: view[key] for key in ("activity_id", "main_minutes", "chunk_ids")})
            self.validate(selection, maximum, allowed)
            connection.execute("UPDATE training_plans SET status='accepted',version=?,accepted_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?", (latest + 1, plan_id, self.user_id))
            row = connection.execute("SELECT * FROM training_plans WHERE id=? AND user_id=?", (plan_id, self.user_id)).fetchone()
            return self.view(row, fingerprint, latest + 1)
