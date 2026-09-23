import json
import re
from datetime import datetime, timezone

from pydantic import ValidationError

from config import ROOT
from model.factory import ModelError
from schemas import KnowledgeSelection

SENSITIVE = re.compile(r"过敏|不耐受|忌口|受伤|伤病|疼|痛|手术|康复|孕|哺乳|儿童|未成年|青少年|(?<!\d)(?:1[0-7]|[0-9])\s*岁|糖尿病|高血压|肾病|疾病|服药|用药|胰岛素|处方|暴食|厌食|催吐|allerg|injur|pregnan|diabet|pain|teen|child", re.I)
PERSONAL = re.compile(r"计划|安排|推荐|菜单|食谱|替换|换一个|分化|怎么练|怎么吃|能吃|能练|适合我|减几|瘦几|meal.plan|workout.plan|recommend", re.I)
PERSONAL_TIME = re.compile(r"(我|今晚|今天|明天|接下来|剩下).*(吃|练|跑|适合|多少卡|多少千卡|多少克)")
NO_CONSTRAINT = {"", "无", "没有", "暂无", "无过敏", "无伤病", "没有过敏", "没有伤病", "无特殊限制", "none", "n/a"}
MESSAGES = {
    "answered": "以下是与你的问题相关的一般资料摘要，不是为你制定的个人建议。",
    "insufficient": "现有资料不足以直接回答这个问题。可以补充更具体的知识问题；不会用猜测补齐。",
    "personal_scope": "本页只回答一般资料问题。普通饮食与训练安排请使用AI助手；医疗、伤病与康复训练不属于本产品范围。",
}


def outside_scope(question, profile, topics=()):
    # Conservative first-version boundary, not a medical or ingredient classifier.
    if SENSITIVE.search(question) or PERSONAL.search(question) or PERSONAL_TIME.search(question):
        return True
    if SENSITIVE.search(profile.get("preferences", "")):
        return True
    if "nutrition" in topics and profile.get("food_allergies", "").strip().lower() not in NO_CONSTRAINT:
        return True
    return False


def answer_question(question, retriever, records, model_factory, model_name):
    def response(status, sources=(), *, called=False):
        return {"status": status, "answer": MESSAGES[status], "sources": [hit.model_dump() for hit in sources],
                "model_called": called, "model": model_name if called else None, "saved": False,
                "answer_mode": "reviewed_extracts", "generated_at": datetime.now(timezone.utc).isoformat()}

    if outside_scope(question, records.get_profile()):
        return response("personal_scope")
    hits = retriever.search(question, limit=6)
    if outside_scope(question, records.get_profile(), {hit.topic for hit in hits}):
        return response("personal_scope")
    if not hits:
        return response("insufficient")
    prompt = (ROOT / "prompts/knowledge_answer.md").read_text(encoding="utf-8")
    prompt += "\nJSON Schema:\n" + json.dumps(KnowledgeSelection.model_json_schema(), ensure_ascii=False)
    message = json.dumps({"question": question, "evidence": [
        {"chunk_id": hit.chunk_id, "title": hit.title, "excerpt": hit.excerpt, "scope": hit.scope}
        for hit in hits]}, ensure_ascii=False)
    model = model_factory()
    raw = model.generate(system_prompt=prompt, message=message)
    try:
        selected = KnowledgeSelection.model_validate_json(raw)
    except ValidationError:
        raise ModelError("MODEL_INVALID_OUTPUT", "资料问答结果未通过校验，未显示未经核对的回答。") from None
    offered = {hit.chunk_id: hit for hit in hits}
    if any(key not in offered for key in selected.chunk_ids):
        raise ModelError("MODEL_INVALID_OUTPUT", "回答引用了本次检索之外的资料，已停止展示。")
    current = {hit.chunk_id: hit for hit in retriever.search(question, limit=6)}
    if any(key not in current or current[key] != offered[key] for key in selected.chunk_ids):
        raise ModelError("KNOWLEDGE_CHANGED", "回答期间资料发生变化或失效，请重新核对后提问。", 409)
    topics = {hit.topic for hit in hits}
    if outside_scope(question, records.get_profile(), topics):
        return response("personal_scope", called=True)
    return response(selected.status, [offered[key] for key in selected.chunk_ids], called=True)
