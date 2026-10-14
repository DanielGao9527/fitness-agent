from fastapi import APIRouter, HTTPException, Request

from agent.react_agent import FitnessAgent
from agent.tools import FitnessTools
from api.routes import Records, User
from model.factory import create_chat_model, create_knowledge_model, ModelError
from rag.rag_service import KnowledgeUnavailable
from schemas import AgentRequest, KnowledgeQuery, KnowledgeQuestion
from services.knowledge_answers import answer_question
from services.usage import reserve_call
from services.knowledge_runtime import request_retriever

router = APIRouter(prefix="/api")


def unavailable(error):
    return HTTPException(503, {"code": "KNOWLEDGE_UNAVAILABLE", "message": str(error)})


@router.get("/knowledge")
def catalog(request: Request, user: User):
    try:
        return request.app.state.knowledge.catalog()
    except KnowledgeUnavailable as error:
        raise unavailable(error) from None


@router.post("/knowledge/search")
def search(body: KnowledgeQuery, request: Request, user: User):
    try:
        retriever = request_retriever(request, user)
        catalog = retriever.catalog()
        if hasattr(retriever, "search_snapshot"):
            snapshot = retriever.search_snapshot(body.query, body.limit, topic=body.topic)
            hits, retrieval = snapshot.hits, snapshot.metadata
        else:
            hits, retrieval = retriever.search(body.query, body.limit, topic=body.topic), {}
        return {"status": catalog["status"], "version": catalog["version"],
                "method": "hybrid_rrf" if retrieval.get("mode") == "hybrid" else catalog["method"],
                "hits": [hit.model_dump() for hit in hits], "source_count": catalog["source_count"],
                "chunk_count": catalog["chunk_count"], "unavailable_source_count": catalog["unavailable_source_count"],
                "model_called": bool(retrieval.get("embedding_called", False)), "saved": False,
                "retrieval": retrieval, "semantic_search_enabled": bool(retrieval.get("embedding_called"))
                or retrieval.get("mode") == "hybrid"}
    except KnowledgeUnavailable as error:
        raise unavailable(error) from None
    except ModelError as error:
        raise HTTPException(error.status_code, {"code": error.code, "message": str(error)}) from None


@router.post("/agent/context")
def preview_context(body: AgentRequest, service: Records, request: Request):
    try:
        retriever = request.app.state.knowledge
        agent = FitnessAgent(FitnessTools(service), create_chat_model(), getattr(retriever, "lexical", retriever))
        return {"day": body.day, "context": agent.prepare_context(body),
                "generation_available": False, "model_called": False, "saved": False}
    except KnowledgeUnavailable as error:
        raise unavailable(error) from None


@router.post("/knowledge/ask")
def ask(body: KnowledgeQuestion, request: Request, service: Records, user: User):
    settings = request.app.state.settings

    def model_factory():
        model = create_knowledge_model(settings)
        reserve_call(request.app.state.database, settings, user["id"])
        return model

    try:
        retriever = request_retriever(request, user)
        if hasattr(retriever, "for_request"):
            # Configuration-only preflight: an unavailable answer feature must not
            # consume a paid query embedding before reporting that it is disabled.
            create_knowledge_model(settings)
        return answer_question(body.question, retriever, service, model_factory, settings.qwen_model)
    except KnowledgeUnavailable as error:
        raise unavailable(error) from None
    except ModelError as error:
        raise HTTPException(error.status_code, {"code": error.code, "message": str(error)}) from None
