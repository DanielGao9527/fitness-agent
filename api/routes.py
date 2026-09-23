import sqlite3
from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from agent.react_agent import FitnessAgent
from agent.tools import FitnessTools
from model.factory import ModelError, ModelNotConfigured, create_chat_model, create_meal_text_model, meal_text_status
from model.factory import create_nutrition_model, nutrition_status
from model.factory import workout_status, knowledge_qa_status, meal_plan_status, training_plan_status, coach_status
from schemas import NutritionPreviewRequest, NutritionPreviewBatch
from services.nutrition import NutritionPreviewStore
from rag.rag_service import KnowledgeUnavailable
from schemas import AgentRequest, Credentials, MealBatchCreate, MealCreate, MealDraft, MealInput, MealTextRequest, Profile, WorkoutCreate, WorkoutInput
from schemas import DraftConfirm, DraftCreate, DraftUpdate, DraftVersion
from security import COOKIE_NAME, DUMMY_HASH, current_user, hash_password, issue_session, token_hash, verify_password
from services.records import RecordService
from services.meal_drafts import MealDraftService, MealDraftStore
from services.intake_targets import IntakeTargetService
from schemas import IntakeTargetChange, EnergyEstimateRequest, EnergyEstimateConfirm
from schemas import EnergyAdjustmentRequest, EnergyAdjustmentConfirm
from schemas import NutritionTargetPreview, NutritionTargetSave, NutritionDaySave
from services.nutrition_targets import NutritionTargetService
from services.usage import reserve_call, usage_status
from services.access_guard import reserve_auth
from services.business_time import BUSINESS_TIMEZONE, business_today
from model.speech import QwenSpeechModel, speech_status, validate_audio, MAX_AUDIO_BYTES
from starlette.concurrency import run_in_threadpool
from model.vision import photo_status

router = APIRouter(prefix="/api")
User = Annotated[dict, Depends(current_user)]


def records(request: Request, user: User) -> RecordService:
    return RecordService(request.app.state.database, user["id"])


Records = Annotated[RecordService, Depends(records)]


def drafts(request: Request, user: User) -> MealDraftStore:
    return MealDraftStore(request.app.state.database, user["id"])


Drafts = Annotated[MealDraftStore, Depends(drafts)]


@router.get("/health")
def health(request: Request):
    settings = request.app.state.settings
    try:
        knowledge = request.app.state.knowledge.catalog()["status"]
    except KnowledgeUnavailable:
        knowledge = "unavailable"
    capabilities = {
        "status": "ok",
        "business_timezone": BUSINESS_TIMEZONE, "business_day": business_today().isoformat(),
        "registration_enabled": request.app.state.settings.registration_enabled,
        "coach_understanding": coach_status(request.app.state.settings),
        "meal_text": meal_text_status(request.app.state.settings),
        "knowledge": knowledge, "photo": photo_status(request.app.state.settings),
        "speech": speech_status(request.app.state.settings),
        "nutrition": nutrition_status(request.app.state.settings),
        "workout": workout_status(request.app.state.settings),
        "knowledge_qa": knowledge_qa_status(request.app.state.settings),
        "meal_plan": meal_plan_status(request.app.state.settings),
        "training_plan": training_plan_status(request.app.state.settings),
    }
    if settings.access_mode == "local":
        capabilities.update(stage="r1.30-training-references", model="not_configured",
                            coach="guided_workflow", text_model=settings.qwen_model)
    return capabilities


@router.post("/meal-drafts/parse-text", response_model=MealDraft)
def parse_meal_text(body: MealTextRequest, request: Request, user: User):
    from services.meal_input import require_single_meal
    require_single_meal(body.text)
    try:
        model = create_meal_text_model(request.app.state.settings)
        reserve_call(request.app.state.database, request.app.state.settings, user["id"])
        return MealDraftService(model).parse_text(body.text)
    except ModelError as error:
        raise HTTPException(error.status_code, {"code": error.code, "message": str(error)}) from None


@router.post("/meal-drafts", status_code=201)
def create_draft(body: DraftCreate, service: Drafts):
    return service.create(body)


@router.get("/meal-drafts")
def list_drafts(service: Drafts):
    return service.list_active()


@router.get("/meal-drafts/{draft_id}")
def get_draft(draft_id: UUID, service: Drafts):
    return service.get(draft_id)


@router.put("/meal-drafts/{draft_id}")
def update_draft(draft_id: UUID, body: DraftUpdate, service: Drafts):
    return service.update(draft_id, body)


@router.post("/meal-drafts/{draft_id}/parse")
def parse_draft(draft_id: UUID, body: DraftVersion, service: Drafts, request: Request):
    try:
        def model_factory():
            model = create_meal_text_model(request.app.state.settings)
            reserve_call(request.app.state.database, request.app.state.settings, service.user_id)
            return model
        return service.parse(draft_id, body.version, model_factory)
    except ModelError as error:
        raise HTTPException(error.status_code, {"code": error.code, "message": str(error)}) from None


@router.post("/speech/transcribe")
async def transcribe_speech(request: Request, user: User):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "录音超过大小限制")
        data.extend(chunk)
    audio = bytes(data)
    validate_audio(audio)
    try:
        model = QwenSpeechModel(request.app.state.settings)
        await run_in_threadpool(reserve_call, request.app.state.database, request.app.state.settings, user["id"])
        text = await run_in_threadpool(model.transcribe, audio)
        return {"text": text}
    except ModelError as error:
        raise HTTPException(error.status_code, {"code": error.code, "message": str(error)}) from None


@router.post("/meal-drafts/{draft_id}/nutrition")
def estimate_draft_nutrition(draft_id: UUID, body: DraftVersion, service: Drafts, request: Request):
    settings = request.app.state.settings
    try:
        def model_factory(count=1):
            model = create_nutrition_model(settings)
            reserve_call(request.app.state.database, settings, service.user_id, count=count)
            return model
        return service.estimate_nutrition(draft_id, body.version, model_factory, settings.qwen_model)
    except ModelError as error:
        raise HTTPException(error.status_code, {"code": error.code, "message": str(error)}) from None


@router.post("/meal-drafts/{draft_id}/nutrition/clear")
def clear_draft_nutrition(draft_id: UUID, body: DraftVersion, service: Drafts):
    return service.clear_nutrition(draft_id, body.version)


@router.post("/nutrition/preview")
def nutrition_preview(body: NutritionPreviewRequest, request: Request, user: User):
    settings = request.app.state.settings
    try:
        def model_factory(count=1):
            model = create_nutrition_model(settings)
            reserve_call(request.app.state.database, settings, user["id"], count=count)
            return model
        return NutritionPreviewStore(request.app.state.database, user["id"]).create(body, model_factory, settings.qwen_model)
    except ModelError as error:
        raise HTTPException(error.status_code, {"code": error.code, "message": str(error)}) from None


@router.post("/nutrition/preview-batch")
def nutrition_preview_batch(body: NutritionPreviewBatch, request: Request, user: User):
    settings = request.app.state.settings
    try:
        def model_factory(count=1):
            model = create_nutrition_model(settings)
            reserve_call(request.app.state.database, settings, user["id"], count=count)
            return model
        return {"items": NutritionPreviewStore(request.app.state.database, user["id"]).create_many(body.items, model_factory, settings.qwen_model)}
    except ModelError as error:
        raise HTTPException(error.status_code, {"code": error.code, "message": str(error)}) from None


@router.post("/meal-drafts/{draft_id}/confirm")
def confirm_draft(draft_id: UUID, body: DraftConfirm, service: Drafts):
    return service.confirm(draft_id, body)


@router.post("/meal-drafts/{draft_id}/cancel")
def cancel_draft(draft_id: UUID, body: DraftVersion, service: Drafts):
    return service.cancel(draft_id, body.version)


@router.post("/auth/register", status_code=201)
def register(body: Credentials, request: Request, response: Response):
    if not request.app.state.settings.registration_enabled:
        raise HTTPException(403, "当前暂不开放注册，请联系管理者")
    reserve_auth(request, body.username)
    password_hash = hash_password(body.password)
    try:
        with request.app.state.database.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO users(username, password_hash) VALUES (?, ?)", (body.username.lower(), password_hash)
            )
            user = {"id": cursor.lastrowid, "username": body.username.lower()}
    except sqlite3.IntegrityError:
        raise HTTPException(409, "用户名已被使用") from None
    issue_session(request, response, user["id"])
    return user


@router.post("/auth/login")
def login(body: Credentials, request: Request, response: Response):
    reserve_auth(request, body.username)
    with request.app.state.database.connect() as connection:
        row = connection.execute("SELECT * FROM users WHERE username = ?", (body.username.lower(),)).fetchone()
    valid = verify_password(body.password, row["password_hash"] if row else DUMMY_HASH)
    if row is None or not valid:
        raise HTTPException(401, "用户名或密码不正确")
    issue_session(request, response, row["id"])
    return {"id": row["id"], "username": row["username"]}


@router.post("/auth/logout", status_code=204)
def logout(request: Request, response: Response):
    with request.app.state.database.connect() as connection:
        connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(request.cookies.get(COOKIE_NAME, "")),))
    response.delete_cookie(COOKIE_NAME, path="/")


@router.get("/auth/me")
def me(user: User):
    return user


@router.get("/usage")
def own_usage(request: Request, user: User):
    return usage_status(request.app.state.database, request.app.state.settings, user["id"])


@router.get("/profile")
def get_profile(service: Records):
    return service.get_profile()


@router.put("/profile")
def put_profile(body: Profile, service: Records):
    return service.save_profile(body)


@router.get("/meals")
def list_meals(day: date, service: Records):
    return service.list_records("meals", day)


@router.post("/meals", status_code=201)
def create_meal(body: MealCreate, service: Records):
    return service.create("meals", body)


@router.post("/meals/batch", status_code=201)
def create_meal_batch(body: MealBatchCreate, service: Records):
    return service.create_many("meals", body.items)


@router.put("/meals/{record_id}")
def update_meal(record_id: int, body: MealInput, service: Records):
    return service.update("meals", record_id, body)


@router.delete("/meals/{record_id}", status_code=204)
def delete_meal(record_id: int, service: Records):
    service.delete("meals", record_id)


@router.get("/workouts")
def list_workouts(day: date, service: Records):
    return service.list_records("workouts", day)


@router.get("/workouts/week")
def workout_week(day: date, service: Records):
    return service.workout_week(day)


@router.get("/meals/week")
def meal_week(day: date, service: Records):
    return service.meal_week(day)


@router.post("/workouts", status_code=201)
def create_workout(body: WorkoutCreate, service: Records):
    return service.create("workouts", body)


@router.put("/workouts/{record_id}")
def update_workout(record_id: int, body: WorkoutInput, service: Records):
    return service.update("workouts", record_id, body)


@router.delete("/workouts/{record_id}", status_code=204)
def delete_workout(record_id: int, service: Records):
    service.delete("workouts", record_id)


@router.get("/summary")
def summary(day: date, service: Records):
    return service.summary(day)


@router.get("/intake-target")
def intake_target(day: date, request: Request, user: User):
    return IntakeTargetService(request.app.state.database, user["id"]).get(day)


@router.post("/intake-target")
def change_intake_target(body: IntakeTargetChange, request: Request, user: User):
    return IntakeTargetService(request.app.state.database, user["id"]).change(body)


@router.post("/intake-target/estimate")
def estimate_intake_target(body: EnergyEstimateRequest, request: Request, user: User):
    return IntakeTargetService(request.app.state.database, user["id"]).estimate(body)


@router.post("/intake-target/confirm-estimate")
def confirm_intake_estimate(body: EnergyEstimateConfirm, request: Request, user: User):
    return IntakeTargetService(request.app.state.database, user["id"]).change(body, estimated=True)


@router.post("/intake-target/adjustment")
def preview_intake_adjustment(body: EnergyAdjustmentRequest, request: Request, user: User):
    return IntakeTargetService(request.app.state.database, user["id"]).estimate(body, adjusted=True)


@router.post("/intake-target/confirm-adjustment")
def confirm_intake_adjustment(body: EnergyAdjustmentConfirm, request: Request, user: User):
    return IntakeTargetService(request.app.state.database, user["id"]).change(body, adjusted=True)


@router.post("/nutrition-target/preview")
def preview_nutrition_target(body: NutritionTargetPreview, request: Request, user: User):
    return NutritionTargetService(request.app.state.database, user["id"]).preview(body)


@router.post("/nutrition-target/standard")
def save_nutrition_standard(body: NutritionTargetSave, request: Request, user: User):
    return NutritionTargetService(request.app.state.database, user["id"]).save(body)


@router.post("/nutrition-target/day")
def save_nutrition_day(body: NutritionDaySave, request: Request, user: User):
    return NutritionTargetService(request.app.state.database, user["id"]).save(body, daily=True)


@router.post("/agent/chat")
def chat(body: AgentRequest, service: Records, request: Request):
    agent = FitnessAgent(FitnessTools(service), create_chat_model(), request.app.state.knowledge)
    try:
        return agent.respond(body)
    except ModelNotConfigured as error:
        raise HTTPException(503, {"code": "MODEL_NOT_CONFIGURED", "message": str(error)}) from None
    except KnowledgeUnavailable as error:
        raise HTTPException(503, {"code": "KNOWLEDGE_UNAVAILABLE", "message": str(error)}) from None
