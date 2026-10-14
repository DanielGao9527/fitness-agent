from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response

from api.routes import User
from rag.rag_service import KnowledgeUnavailable
from schemas import (KnowledgeFeedbackCreate, KnowledgeCandidateCreate, KnowledgeCandidateUpdate,
                     KnowledgeCandidateReject, KnowledgeWorkbenchVersion)
from services.knowledge_feedback import KnowledgeWorkbench

router = APIRouter(prefix="/api/knowledge-workbench")


def service(request, user):
    # Local source lookup only: never binds a paid query encoder to a write request.
    return KnowledgeWorkbench(request.app.state.database, user["id"], request.app.state.knowledge)


def run(operation):
    try:
        return operation()
    except KnowledgeUnavailable:
        raise HTTPException(503, "公共来源暂不可核对，原反馈和候选保留。") from None


@router.get("/sources")
def sources(request: Request, user: User):
    return {"items": run(lambda: service(request, user).public_sources()), "model_called": False}


@router.get("/feedback")
def feedback(request: Request, user: User):
    return {"items": service(request, user).feedback()}


@router.post("/feedback", status_code=201)
def create_feedback(body: KnowledgeFeedbackCreate, request: Request, user: User):
    return service(request, user).create_feedback(body)


@router.delete("/feedback/{identifier}", status_code=204)
def delete_feedback(identifier: UUID, body: KnowledgeWorkbenchVersion, request: Request, user: User):
    service(request, user).delete("knowledge_feedback", identifier, body.version)
    return Response(status_code=204)


@router.get("/candidates")
def candidates(request: Request, user: User):
    return {"items": run(lambda: service(request, user).candidates())}


@router.post("/candidates", status_code=201)
def create_candidate(body: KnowledgeCandidateCreate, request: Request, user: User):
    return run(lambda: service(request, user).create_candidate(body))


@router.put("/candidates/{identifier}")
def revise_candidate(identifier: UUID, body: KnowledgeCandidateUpdate, request: Request, user: User):
    return run(lambda: service(request, user).revise(identifier, body))


@router.post("/candidates/{identifier}/reject")
def reject_candidate(identifier: UUID, body: KnowledgeCandidateReject, request: Request, user: User):
    return run(lambda: service(request, user).reject(identifier, body))


@router.delete("/candidates/{identifier}", status_code=204)
def delete_candidate(identifier: UUID, body: KnowledgeWorkbenchVersion, request: Request, user: User):
    service(request, user).delete("knowledge_candidates", identifier, body.version)
    return Response(status_code=204)
