from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response

from api.routes import User
from schemas import CoachCreate, CoachMessage, CoachVersion, CoachUnderstand, CoachAutoUnderstand, CoachReviewConfirm
from model.factory import ModelError, coach_status, create_coach_model
from services.coach_reviews import CoachReviewService
from services.usage import reserve_call
from services.coach import CoachService
from services.meal_plans import MealPlanService
from services.training_plans import TrainingPlanService
from services.quick_meals import QuickMealService
from services.meal_consent import MealConsentService
from model.factory import create_meal_plan_model
from rag.rag_service import KnowledgeUnavailable
from services.meal_nutrient_reference import current_context
from services.training_recommendations import TrainingRecommendationService

router = APIRouter(prefix="/api/coach/conversations")


def service(request, user):
    return CoachService(request.app.state.database, user["id"])


def with_plans(request, user, data):
    data = TrainingRecommendationService(request.app.state.database, user["id"], request.app.state.knowledge).decorate(data)
    plans = MealPlanService(request.app.state.database, user["id"], request.app.state.knowledge).list(data["day"])
    review = CoachReviewService(request.app.state.database, user["id"]).latest(data["id"])
    training = TrainingPlanService(request.app.state.database, user["id"], request.app.state.knowledge).list(data["day"])
    calculation = None
    if data["intent"] == "meal":
        try:
            with request.app.state.database.connect() as connection:
                connection.execute("BEGIN")
                calculation = current_context(connection, user["id"], data["day"])
        except ModelError as error:
            calculation = {"ready": False, "reason": "reference_unavailable", "message": str(error)}
    return {**data, "meal_consent": MealConsentService(request, user).get(),
            "meal_calculation": calculation,
            "meal_plans": [plan for plan in plans if plan["coach_id"] == data["id"]],
            "training_plans": [plan for plan in training if plan["coach_id"] == data["id"]],
            "review": review, "understanding_status": coach_status(request.app.state.settings)}


def run(operation):
    try:
        return operation()
    except ModelError as error:
        raise HTTPException(error.status_code, {"code": error.code, "message": str(error)}) from None
    except KnowledgeUnavailable:
        raise HTTPException(503, "推荐依据暂不可用，原记录保留。") from None


@router.post("/{conversation_id}/recommend-meals")
def recommend_meals(conversation_id: UUID, body: CoachUnderstand, request: Request, user: User):
    settings = request.app.state.settings

    def factory():
        model = create_meal_plan_model(settings)
        reserve_call(request.app.state.database, settings, user["id"])
        return model

    recommender = QuickMealService(request.app.state.database, user["id"], request.app.state.knowledge)
    run(lambda: recommender.generate(conversation_id, body, factory, settings.qwen_model, MealConsentService(request, user).require))
    return with_plans(request, user, service(request, user).get(conversation_id))


@router.post("/{conversation_id}/recommend-training")
def recommend_training(conversation_id: UUID, body: CoachUnderstand, request: Request, user: User):
    if not request.app.state.settings.training_plan_enabled:
        raise HTTPException(503, "训练建议尚未启用，手动记录仍可使用。")
    recommender = TrainingRecommendationService(request.app.state.database, user["id"], request.app.state.knowledge)
    run(lambda: recommender.generate(conversation_id, body))
    return with_plans(request, user, service(request, user).get(conversation_id))


@router.get("")
def list_conversations(request: Request, user: User):
    return service(request, user).list()


@router.post("", status_code=201)
def create(body: CoachCreate, request: Request, user: User):
    return with_plans(request, user, service(request, user).create(body))


@router.get("/{conversation_id}")
def get(conversation_id: UUID, request: Request, user: User):
    return with_plans(request, user, service(request, user).get(conversation_id))


@router.post("/{conversation_id}/messages")
def send(conversation_id: UUID, body: CoachMessage, request: Request, user: User):
    return with_plans(request, user, run(lambda: service(request, user).send(conversation_id, body)))


@router.delete("/{conversation_id}", status_code=204)
def delete(conversation_id: UUID, body: CoachVersion, request: Request, user: User):
    service(request, user).delete(conversation_id, body.version)
    return Response(status_code=204)


@router.post("/{conversation_id}/understand")
def understand(conversation_id: UUID, body: CoachAutoUnderstand, request: Request, user: User):
    settings = request.app.state.settings

    def factory():
        model = create_coach_model(settings)
        try:
            reserve_call(request.app.state.database, settings, user["id"])
        except HTTPException as error:
            raise ModelError("COACH_CALL_LIMIT", "今日模型调用次数已达上限，原话已保留，手动记录仍可使用。", error.status_code) from None
        return model

    reviewer = CoachReviewService(request.app.state.database, user["id"])
    if body.auto_apply:
        run(MealConsentService(request, user).require)
    run(lambda: reviewer.understand(conversation_id, body, factory, settings.qwen_model))
    if body.auto_apply:
        run(MealConsentService(request, user).require)
        run(lambda: reviewer.apply_ready(conversation_id, body.version))
    return with_plans(request, user, service(request, user).get(conversation_id))


@router.post("/{conversation_id}/confirm-understanding")
def confirm_understanding(conversation_id: UUID, body: CoachReviewConfirm, request: Request, user: User):
    data = run(lambda: CoachReviewService(request.app.state.database, user["id"]).confirm(conversation_id, body))
    return with_plans(request, user, data)
