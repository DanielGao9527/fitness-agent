from datetime import date
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from api.routes import User
from model.factory import ModelError, create_training_plan_model
from rag.rag_service import KnowledgeUnavailable
from schemas import MealPlanAccept, TrainingPlanRequest, TrainingExecutionUpdate, TrainingExecutionCommit
from services.training_execution import TrainingExecutionService
from services.training_plans import TrainingPlanService
from services.usage import reserve_call

router = APIRouter(prefix="/api/training-plans")


def service(request, user):
    return TrainingPlanService(request.app.state.database, user["id"], request.app.state.knowledge)


def run(operation):
    try:
        return operation()
    except ModelError as error:
        raise HTTPException(error.status_code, {"code": error.code, "message": str(error)}) from None
    except KnowledgeUnavailable:
        raise HTTPException(503, {"code": "KNOWLEDGE_UNAVAILABLE", "message": "训练依据暂不可用，未生成或采纳新建议。"}) from None


@router.get("")
def history(day: date, request: Request, user: User):
    return run(lambda: service(request, user).list(day.isoformat()))


@router.post("")
def generate(body: TrainingPlanRequest, request: Request, user: User):
    settings = request.app.state.settings

    def factory():
        model = create_training_plan_model(settings)
        reserve_call(request.app.state.database, settings, user["id"])
        return model

    return run(lambda: service(request, user).generate(body, factory, settings.qwen_model))


@router.post("/{plan_id}/accept")
def accept(plan_id: UUID, body: MealPlanAccept, request: Request, user: User):
    return run(lambda: service(request, user).accept(str(plan_id)))


@router.post("/{plan_id}/execution")
def open_execution(plan_id: UUID, body: MealPlanAccept, request: Request, user: User):
    return run(lambda: TrainingExecutionService(request.app.state.database, user["id"]).open(str(plan_id)))


@router.get("/executions")
def list_executions(request: Request, user: User):
    return run(lambda: TrainingExecutionService(request.app.state.database, user["id"]).list())


@router.put("/{plan_id}/execution")
def save_execution(plan_id: UUID, body: TrainingExecutionUpdate, request: Request, user: User):
    return run(lambda: TrainingExecutionService(request.app.state.database, user["id"]).save(str(plan_id), body))


@router.post("/{plan_id}/execution/commit")
def commit_execution(plan_id: UUID, body: TrainingExecutionCommit, request: Request, user: User):
    return run(lambda: TrainingExecutionService(request.app.state.database, user["id"]).commit(str(plan_id), body))


@router.delete("/{plan_id}/execution")
def discard_execution(plan_id: UUID, body: TrainingExecutionCommit, request: Request, user: User):
    return run(lambda: TrainingExecutionService(request.app.state.database, user["id"]).discard(str(plan_id), body))
