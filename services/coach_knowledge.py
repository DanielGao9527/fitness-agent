"""Read-only knowledge turns, persisted independently of planning reviews."""
import json

from fastapi import HTTPException

from model.factory import ModelError
from rag.rag_service import KnowledgeUnavailable
from services.coach import CoachService, conversation_constraints, pending_turns
from services.knowledge_answers import answer_question, outside_scope
from services.meal_plans import digest, encode
from services.records import RecordService
from services.knowledge_feedback import answer_signature


class CoachKnowledgeService:
    def __init__(self, database, user_id, retriever):
        self.database, self.user_id, self.retriever = database, user_id, retriever
        self.coach = CoachService(database, user_id)

    def snapshot(self, connection, conversation_id):
        row = self.coach.owned(connection, conversation_id)
        turn = connection.execute("SELECT message,response FROM coach_turns WHERE conversation_id=? AND version=?",
                                  (row["id"], row["version"])).fetchone()
        if not turn or json.loads(turn["response"]).get("intent") != "knowledge":
            raise ModelError("COACH_NOT_KNOWLEDGE", "这条消息不是独立知识提问，未调用资料问答。", 409)
        profile = connection.execute("SELECT payload FROM profiles WHERE user_id=?", (self.user_id,)).fetchone()
        facts = {"version": row["version"], "question": turn["message"],
                 "profile": json.loads(profile[0]) if profile else {},
                 "constraints": conversation_constraints(connection, row["id"]),
                 "pending": pending_turns(connection, row["id"])}
        return row, json.loads(turn["response"]), facts, digest(facts)

    @staticmethod
    def boundary(facts, topics=()):
        if facts["pending"]:
            return "此前仍有未核对的条件或请求，本次知识问答未解除它们；请先核对下方待办。"
        limits = facts["constraints"]
        if any(limits.get(key) for key in ("training_caution", "diet_caution")):
            return "对话含特殊健康条件，当前一般资料摘答不能判断本人适用性，未生成个人建议。"
        if "nutrition" in topics and limits.get("food_avoid"):
            return "对话中已有食物禁忌；当前营养资料摘答暂不处理这类适用性判断，禁忌仍保留。"
        return ""

    def save(self, connection, conversation_id, version, response):
        connection.execute("UPDATE coach_turns SET response=? WHERE conversation_id=? AND version=?",
                           (encode(response), str(conversation_id), version))

    def answer(self, conversation_id, body, factory, model_name, availability):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row, response, facts, fingerprint = self.snapshot(connection, conversation_id)
            if row["version"] != body.version:
                raise ModelError("COACH_CONTEXT_CHANGED", "对话已变化，请按最新消息提问。", 409)
            reused = connection.execute("SELECT version FROM coach_turns WHERE conversation_id=? "
                "AND json_extract(response,'$.knowledge.request_id')=?", (row["id"], str(body.client_id))).fetchone()
            if reused and reused[0] != body.version:
                raise ModelError("COACH_REQUEST_CONFLICT", "本次问答标识已用于其他消息。", 409)
            # One attempt per turn, including failures/uncertain interrupted calls.
            # Re-sending a new message is explicit; HTTP retries never call twice.
            if response.get("knowledge"):
                return
            response["knowledge"] = {"request_id": str(body.client_id), "context_hash": fingerprint,
                "status": "generating", "answer": "资料问答已提交；未确认完成时不会自动重试，可刷新查看。", "sources": []}
            self.save(connection, row["id"], body.version, response)
        try:
            if availability != "configured_unverified":
                raise ModelError("KNOWLEDGE_NOT_CONFIGURED" if availability == "not_configured" else "KNOWLEDGE_CONFIGURATION_ERROR",
                                 "知识问答未启用或配置不可用；这不代表资料库为空，手动记录仍可使用。", 503)
            if len(facts["question"]) > 1000:
                raise ModelError("KNOWLEDGE_QUESTION_TOO_LONG", "知识问题请控制在1000字以内，原消息已保留。", 422)
            boundary = self.boundary(facts)
            if boundary:
                value = {"status": "context_pending", "answer": boundary, "sources": [],
                         "model_called": False}
            else:
                value = answer_question(facts["question"], self.retriever,
                    RecordService(self.database, self.user_id), factory, model_name,
                    candidate_boundary=lambda topics: self.boundary(facts, topics))
                if value["status"] == "personal_scope":
                    value["answer"] = "当前资料摘答只覆盖一般知识。这个问题或已保存的健康、饮食限制超出当前范围；未生成个人建议，也未清除限制。"
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                _, current, _, current_hash = self.snapshot(connection, conversation_id)
                if current_hash != fingerprint:
                    raise ModelError("COACH_CONTEXT_CHANGED", "问答期间消息、档案或条件已变化，未显示旧结果；请重新提问。", 409)
                if (hasattr(self.retriever, "valid_snapshot") and value.get("status") == "answered"
                        and not self.retriever.valid_snapshot(value.get("retrieval", {}), connection=connection)):
                    raise ModelError("KNOWLEDGE_CHANGED", "保存前知识版本已变化，未保存旧回答。", 409)
                current["knowledge"].update(value)
                self.save(connection, conversation_id, body.version, current)
        except Exception as error:
            if isinstance(error, KnowledgeUnavailable):
                error = ModelError("KNOWLEDGE_UNAVAILABLE", str(error), 503)
            elif isinstance(error, HTTPException):
                error = (ModelError("KNOWLEDGE_CALL_LIMIT", "模型调用次数已达上限，问题已保留，手动记录仍可使用。", 429)
                         if error.status_code == 429 else ModelError("COACH_CONTEXT_CHANGED", "对话或账号已不可用，未保存旧回答。", 409))
            elif not isinstance(error, ModelError):
                error = ModelError("KNOWLEDGE_UNAVAILABLE", "知识问答未确认完成，未自动重试；原问题已保留。")
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                saved = connection.execute("SELECT response FROM coach_turns WHERE conversation_id=? AND version=?",
                                           (str(conversation_id), body.version)).fetchone()
                if saved:
                    failed = json.loads(saved[0])
                    failed["knowledge"].update(status="failed", code=error.code, answer=str(error), sources=[])
                    self.save(connection, conversation_id, body.version, failed)
            raise error

    def decorate(self, data):
        profile = RecordService(self.database, self.user_id).get_profile()
        for turn in data["turns"]:
            value = turn["response"].get("knowledge")
            if value and value.get("status") in ("answered", "insufficient"):
                turn["feedback_target"] = {"answer_hash": answer_signature(turn["message"], value)}
            if not value or value.get("status") != "answered":
                continue
            sources = value["sources"]
            try:
                current = {hit.chunk_id: hit.model_dump() for hit in self.retriever.get_chunks([hit["chunk_id"] for hit in sources])}
                valid = all(current.get(hit["chunk_id"]) == hit for hit in sources)
                if hasattr(self.retriever, "valid_snapshot"):
                    valid = valid and self.retriever.valid_snapshot(value.get("retrieval", {}))
            except KnowledgeUnavailable:
                valid = False
            if not valid:
                value.update(status="sources_changed", answer="这条历史回答的依据已变化、失效或暂不可用，摘要已隐藏；请重新提问。", sources=[])
            elif outside_scope(turn["message"], profile, {hit["topic"] for hit in sources}) or self.boundary(
                    {"pending": [], "constraints": data["constraints"]}, {hit["topic"] for hit in sources}):
                value.update(status="personal_scope", answer="当前档案或对话条件已超出一般资料摘答范围，历史摘要暂不展示；未生成个人建议。", sources=[])
        return data
