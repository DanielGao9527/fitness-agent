import json
import re
import unicodedata
from datetime import datetime, timezone

from pydantic import ValidationError

from config import ROOT
from model.factory import ModelError
from rag.rag_service import RetrievalSnapshot
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


def retrieve_candidates(question, retriever):
    """Capture one search result; later source checks must not repeat ranking or embedding."""
    search_snapshot = getattr(retriever, "search_snapshot", None)
    if callable(search_snapshot):
        snapshot = search_snapshot(question, limit=6)
        metadata = public_retrieval_metadata(snapshot.metadata)
        hits = snapshot.hits
    else:
        # Keep injected/test retrievers implementing only the original protocol working.
        hits = retriever.search(question, limit=6)
        metadata = {"mode": "lexical", "requested_mode": "lexical", "rankings": [
            {"chunk_id": hit.chunk_id, "lexical_rank": rank} for rank, hit in enumerate(hits, 1)]}
    # A retriever may cache mutable hit objects. Freeze their values for this answer.
    return RetrievalSnapshot(hits=[hit.model_copy(deep=True) for hit in hits], metadata=metadata)


def public_retrieval_metadata(metadata):
    """Only operational metadata belongs in an answer, never raw queries or vectors."""
    keys = {"mode", "requested_mode", "tokenizer", "corpus_version", "corpus_hash", "text_template",
            "embedding_identity", "embedding_called", "dimensions", "degraded", "reason", "candidate_limit", "rrf_k"}
    public = {key: value for key, value in metadata.items()
              if key in keys and isinstance(value, (str, int, float, bool))}
    rank_keys = {"chunk_id", "lexical_rank", "dense_rank", "rrf_score", "content_hash"}
    public["rankings"] = [{key: value for key, value in item.items()
                           if key in rank_keys and (value is None or isinstance(value, (str, int, float)))}
                          for item in metadata.get("rankings", []) if isinstance(item, dict)]
    return public


def normalized_query_key(value):
    """Ignore display-only width, case and punctuation before spending a retry."""
    return "".join(char for char in unicodedata.normalize("NFKC", value).casefold() if char.isalnum())


def answer_question(question, retriever, records, model_factory, model_name, *, candidate_snapshot=None,
                    candidate_boundary=None):
    retrieval = None
    attempts = []
    called = False
    stop_reason = None

    def response(status, sources=(), *, message=None):
        value = {"status": status, "answer": message or MESSAGES[status],
                "sources": [hit.model_dump() for hit in sources],
                "model_called": called or any(item.get("embedding_called") for item in attempts),
                "model": model_name if called else None, "saved": False,
                "answer_mode": "reviewed_extracts", "generated_at": datetime.now(timezone.utc).isoformat()}
        if retrieval is not None:
            value["retrieval"] = dict(retrieval, embedding_called=any(
                item.get("embedding_called") for item in attempts), attempts=attempts,
                stop_reason=stop_reason)
        return value

    profile = records.get_profile()
    if outside_scope(question, profile):
        return response("personal_scope")
    snapshot = candidate_snapshot if candidate_snapshot is not None else retrieve_candidates(question, retriever)
    prompt = (ROOT / "prompts/knowledge_answer.md").read_text(encoding="utf-8")
    prompt += "\nJSON Schema:\n" + json.dumps(KnowledgeSelection.model_json_schema(), ensure_ascii=False)
    previous_hits = None
    for number in (1, 2):
        hits = snapshot.hits
        retrieval = public_retrieval_metadata(snapshot.metadata)
        attempt = {"number": number, "candidate_ids": [hit.chunk_id for hit in hits],
                   "mode": retrieval.get("mode"), "degraded_reason": retrieval.get("reason"),
                   "embedding_called": bool(retrieval.get("embedding_called"))}
        attempts.append(attempt)
        topics = {hit.topic for hit in hits}
        if outside_scope(question, records.get_profile(), topics):
            attempt["decision"] = stop_reason = "personal_scope"
            return response("personal_scope")
        if candidate_boundary:
            boundary = candidate_boundary(topics)
            if boundary:
                attempt["decision"] = stop_reason = "context_pending"
                return response("context_pending", message=boundary)
        if not hits:
            attempt["decision"] = stop_reason = "no_candidates"
            return response("insufficient")
        if previous_hits == hits:
            attempt["decision"] = stop_reason = "no_new_evidence"
            return response("insufficient")
        message = json.dumps({"question": question, "evidence": [
            {"chunk_id": hit.chunk_id, "title": hit.title, "excerpt": hit.excerpt, "scope": hit.scope}
            for hit in hits]}, ensure_ascii=False)
        model = model_factory()
        called = True
        raw = model.generate(system_prompt=prompt, message=message)
        try:
            selected = KnowledgeSelection.model_validate_json(raw)
        except ValidationError:
            raise ModelError("MODEL_INVALID_OUTPUT", "资料问答结果未通过校验，未显示未经核对的回答。") from None
        attempt["decision"] = selected.status
        if selected.status == "insufficient" and number == 1 and selected.retry_query:
            revised = selected.retry_query
            if normalized_query_key(revised) == normalized_query_key(question) or outside_scope(revised, profile):
                stop_reason = "rewrite_rejected"
                return response("insufficient")
            attempt["rewrite_reason"] = selected.retry_reason
            previous_hits = hits
            snapshot = retrieve_candidates(revised, retriever)
            continue
        if selected.status != "answered":
            stop_reason = "retry_budget_exhausted" if number == 2 and selected.retry_query else selected.status
            return response(selected.status)
        offered = {hit.chunk_id: hit for hit in hits}
        if any(key not in offered for key in selected.chunk_ids):
            raise ModelError("MODEL_INVALID_OUTPUT", "回答引用了本次检索之外的资料，已停止展示。")
        # Recheck selected IDs against current sources; a retry cannot resurrect
        # a withdrawn or edited public excerpt.
        current = {hit.chunk_id: hit for hit in retriever.get_chunks(selected.chunk_ids)}
        if any(key not in current or current[key] != offered[key] for key in selected.chunk_ids):
            raise ModelError("KNOWLEDGE_CHANGED", "回答期间资料发生变化或失效，请重新核对后提问。", 409)
        if outside_scope(question, records.get_profile(), topics):
            stop_reason = "personal_scope"
            return response("personal_scope")
        stop_reason = "answered"
        return response("answered", [offered[key] for key in selected.chunk_ids])
    raise AssertionError("Bounded knowledge search exhausted without a decision")
