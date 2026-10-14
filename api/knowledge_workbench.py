from uuid import UUID
import sqlite3

from fastapi import APIRouter, HTTPException, Request, Response

from api.routes import User
from rag.rag_service import KnowledgeUnavailable
from schemas import (KnowledgeFeedbackCreate, KnowledgeCandidateCreate, KnowledgeCandidateUpdate,
                     KnowledgeCandidateReject, KnowledgeWorkbenchVersion, KnowledgeReviewSubmit,
                     KnowledgeReviewApprove, KnowledgeReviewReject, KnowledgeBuildCreate)
from services.knowledge_feedback import KnowledgeWorkbench
from services.knowledge_review import KnowledgeReview
from services.knowledge_builds import KnowledgeBuilds
from rag.vector_store import VectorCacheUnavailable

router = APIRouter(prefix="/api/knowledge-workbench")


def service(request, user):
    # Local source lookup only: never binds a paid query encoder to a write request.
    return KnowledgeWorkbench(request.app.state.database, user["id"], request.app.state.knowledge)


def review_service(request, user):
    return KnowledgeReview(request.app.state.database, user["id"], request.app.state.knowledge)


def build_service(request, user):
    return KnowledgeBuilds(request.app.state.database, user["id"], request.app.state.knowledge,
                          request.app.state.settings, request.app.state.knowledge_build_lock)


def run_build(operation):
    try:
        return operation()
    except VectorCacheUnavailable as error:
        raise HTTPException(409, {"code": "KNOWLEDGE_BUILD_VECTOR_UNAVAILABLE", "reason": error.reason,
            "message": "缺少完整且匹配的已有向量缓存；未调用云模型，在线知识库未切换。"}) from None
    except KnowledgeUnavailable:
        raise HTTPException(503, "公共资料暂不可核对，在线知识库未切换。") from None
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        raise HTTPException(503, "构建资料或版本完整性检查失败，在线知识库未切换；请在本机检查。") from None


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


@router.get("/permissions")
def permissions(request: Request, user: User):
    return review_service(request, user).permissions()


@router.get("/submissions")
def submissions(request: Request, user: User):
    return {"items": run(lambda: review_service(request, user).list())}


@router.post("/candidates/{identifier}/submit", status_code=201)
def submit(identifier: UUID, body: KnowledgeReviewSubmit, request: Request, user: User):
    return run(lambda: review_service(request, user).submit(identifier, body))


@router.post("/submissions/{identifier}/withdraw")
def withdraw(identifier: UUID, body: KnowledgeWorkbenchVersion, request: Request, user: User):
    return review_service(request, user).withdraw(identifier, body)


@router.get("/review-queue")
def review_queue(request: Request, user: User):
    return {"items": run(lambda: review_service(request, user).list(queue=True))}


@router.post("/review-queue/{identifier}/check")
def check_submission(identifier: UUID, body: KnowledgeWorkbenchVersion, request: Request, user: User):
    return run(lambda: review_service(request, user).check(identifier, body))


@router.post("/review-queue/{identifier}/approve")
def approve_submission(identifier: UUID, body: KnowledgeReviewApprove, request: Request, user: User):
    return run(lambda: review_service(request, user).decide(identifier, body, approve=True))


@router.post("/review-queue/{identifier}/reject")
def reject_submission(identifier: UUID, body: KnowledgeReviewReject, request: Request, user: User):
    return review_service(request, user).decide(identifier, body, approve=False)


@router.get("/builds")
def builds(request: Request, user: User):
    service = build_service(request, user)
    return {"items": run_build(service.list), "failures": run_build(service.failures), "publication_enabled": False}


@router.post("/builds", status_code=201)
def create_build(body: KnowledgeBuildCreate, request: Request, user: User):
    return run_build(lambda: build_service(request, user).build(body))
