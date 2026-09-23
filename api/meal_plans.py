from datetime import date
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from api.routes import User
from model.factory import ModelError, create_meal_plan_model, create_nutrition_model
from rag.rag_service import KnowledgeUnavailable
from schemas import MealConsent, MealPlanAccept, MealPlanRequest
from schemas import MealIntakeReview, MealIntakeReset, PlanNutritionRequest
from services.meal_consent import MealConsentService
from services.meal_intake import MealIntakeService
from services.meal_plans import MealPlanService
from services.meal_plan_nutrition import MealPlanNutritionService
from services.meal_plan_actions import MealPlanActions
from schemas import PlanPortionEdit
from services.plan_foods import catalog
from services.usage import reserve_call

router = APIRouter(prefix="/api/meal-plans")


def service(request, user):
    return MealPlanService(request.app.state.database, user["id"], request.app.state.knowledge)


def run(operation):
    try:
        return operation()
    except ModelError as error:
        raise HTTPException(error.status_code, {"code": error.code, "message": str(error)}) from None
    except KnowledgeUnavailable:
        raise HTTPException(503, {"code": "KNOWLEDGE_UNAVAILABLE", "message": "搭配依据暂不可用，未生成或采纳新建议。"}) from None


@router.get("/foods")
def foods(user: User):
    return catalog()


@router.get("/consent")
def consent(request: Request, user: User):
    return run(lambda: MealConsentService(request, user).get())


@router.put("/consent")
def confirm_consent(body: MealConsent, request: Request, user: User):
    return run(lambda: MealConsentService(request, user).confirm(body))


@router.delete("/consent")
def revoke_consent(request: Request, user: User):
    return run(lambda: MealConsentService(request, user).revoke())


@router.get("")
def history(day: date, request: Request, user: User):
    return run(lambda: service(request, user).list(day.isoformat()))


@router.get("/intake-context")
def intake_context(day: date, request: Request, user: User):
    return MealIntakeService(request.app.state.database, user["id"]).get(day)


@router.put("/intake-context")
def confirm_intake_context(body: MealIntakeReview, request: Request, user: User):
    return run(lambda: MealIntakeService(request.app.state.database, user["id"]).confirm(body))


@router.post("/intake-context/revoke")
def revoke_intake_context(body: MealIntakeReset, request: Request, user: User):
    return run(lambda: MealIntakeService(request.app.state.database, user["id"]).revoke(body))


@router.post("")
def generate(body: MealPlanRequest, request: Request, user: User):
    if body.quick_recommendation:
        raise HTTPException(409, {"code": "JOINT_MEAL_REQUIRED", "message": "快速餐单需通过助手联合核对所选餐次，请回到助手发送请求。"})
    settings = request.app.state.settings

    def factory():
        model = create_meal_plan_model(settings)
        reserve_call(request.app.state.database, settings, user["id"])
        return model

    guard = None if body.adult_general_diet is True and body.constraints_reviewed is True else MealConsentService(request, user).require
    return run(lambda: service(request, user).generate(body, factory, settings.qwen_model, guard=guard))


@router.post("/{plan_id}/accept")
def accept(plan_id: UUID, body: MealPlanAccept, request: Request, user: User):
    return run(lambda: service(request, user).accept(str(plan_id)))


@router.post("/nutrition")
def estimate_plan_nutrition(body: PlanNutritionRequest, request: Request, user: User):
    settings = request.app.state.settings

    def factory(count):
        model = create_nutrition_model(settings)
        reserve_call(request.app.state.database, settings, user["id"], count=count)
        return model

    nutrition = MealPlanNutritionService(request.app.state.database, user["id"], request.app.state.knowledge)
    return run(lambda: nutrition.estimate(body, factory, settings.qwen_model))


@router.post("/{plan_id}/portions")
def edit_portions(plan_id: UUID, body: PlanPortionEdit, request: Request, user: User):
    service = MealPlanActions(request.app.state.database, user["id"], request.app.state.knowledge)
    return run(lambda: service.edit_portions(plan_id, body, MealConsentService(request, user).require))


@router.post("/{plan_id}/record-draft")
def record_plan_draft(plan_id: UUID, body: MealPlanAccept, request: Request, user: User):
    service = MealPlanActions(request.app.state.database, user["id"], request.app.state.knowledge)
    return run(lambda: service.record_draft(plan_id))
