from fastapi import APIRouter, HTTPException, Request

from api.routes import Records, User
from model.factory import ModelError, create_workout_model
from schemas import MealTextRequest, WorkoutBatchCreate, WorkoutParsed, WorkoutPreviewBatch
from services.usage import reserve_call
from services.workouts import WorkoutPreviewStore, parse_workout_text
from services.training_progression import tracking_catalog

router = APIRouter(prefix="/api/workouts")


@router.get("/exercises")
def exercises(user: User):
    return {"items": tracking_catalog()}


@router.post("/batch", status_code=201)
def create_batch(body: WorkoutBatchCreate, service: Records):
    return service.create_many("workouts", body.items)


@router.post("/parse-text", response_model=WorkoutParsed)
def parse_text(body: MealTextRequest, request: Request, user: User):
    try:
        model = create_workout_model(request.app.state.settings)
        reserve_call(request.app.state.database, request.app.state.settings, user["id"])
        return parse_workout_text(body.text, model)
    except ModelError as error:
        raise HTTPException(error.status_code, {"code": error.code, "message": str(error)}) from None


@router.post("/calories/preview-batch")
def preview(body: WorkoutPreviewBatch, request: Request, user: User):
    settings = request.app.state.settings
    try:
        def model_factory(count):
            model = create_workout_model(settings)
            reserve_call(request.app.state.database, settings, user["id"], count=count)
            return model
        return {"items": WorkoutPreviewStore(request.app.state.database, user["id"]).create_many(body.items, model_factory, settings.qwen_model)}
    except ModelError as error:
        raise HTTPException(error.status_code, {"code": error.code, "message": str(error)}) from None
