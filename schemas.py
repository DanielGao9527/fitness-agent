from datetime import date
import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator
from services.business_time import business_today


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


def portion_issue(name, grams, amount_description=""):
    vague = {"菜", "炒菜", "一道菜", "一盘菜", "一盘炒菜", "食物", "东西", "饭菜", "不知道", "不清楚", "未知"}
    if not name.strip() or name.strip() in vague:
        return "请填写具体食材或菜名，例如鸡蛋、麻婆豆腐"
    if grams is not None:
        return "" if 0 < grams <= 10000 else "请检查克数范围"
    amount = amount_description.strip()
    if re.search(r"[-负]\s*\d|(?:^|\D)0+(?:\.0+)?\s*(?:个|盘|份|碗|杯|克|g|ml)", amount) or any(word in amount for word in ("不知道", "不清楚", "未知", "随便", "若干")):
        return "请补充基本份量，例如2个、半碗、一盘或单人份"
    quantity = r"(?:[1-9]\d*(?:\.\d+)?|0\.\d*[1-9]\d*|[一二两三四五六七八九十百半]+)\s*(?:[大小中小半]*)(?:个|只|枚|片|块|碗|杯|盘|份|人份|勺|袋|盒|瓶|根|串|把|锅|克|毫升|斤|两|g\b|ml\b)"
    english = r"\b(?:one|two|three|half|[1-9]\d*)\s+(?:eggs?|cups?|bowls?|plates?|servings?|pieces?)\b"
    if re.search(quantity, amount, re.I) or re.search(english, amount, re.I) or amount in {"单人份", "双人份", "半份", "小份", "中份", "大份"}:
        return ""
    return "请补充基本份量，例如2个、半碗、一盘或单人份"


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_]+$")
    password: str = Field(min_length=8, max_length=128)


class Profile(InputModel):
    display_name: str = Field(default="", max_length=40)
    goal: Literal["fat_loss", "muscle_gain", "maintain"] = "maintain"
    experience: Literal["beginner", "experienced"] = "beginner"
    height_cm: float | None = Field(default=None, ge=50, le=250)
    weight_kg: float | None = Field(default=None, ge=20, le=400)
    body_fat_percent: float | None = Field(default=None, ge=1, le=75)
    training_days: int = Field(default=3, ge=0, le=7, strict=True)
    training_split: Literal["ppl", "four", "five"] = "ppl"
    minutes_per_session: int = Field(default=30, ge=5, le=300, strict=True)
    equipment: str = Field(default="", max_length=300)
    preferences: str = Field(default="", max_length=1000)
    food_allergies: str = Field(default="", max_length=1000)
    nutrition_reference: Literal["general", "regular_training"] = "general"
    age: int | None = Field(default=None, ge=19, le=100, strict=True)
    equation_sex: Literal["male", "female"] | None = None
    activity: Literal["inactive", "low_active", "active", "very_active"] | None = None


class BodyMeasurementInput(InputModel):
    day: date
    weight_kg: float | None = Field(default=None, ge=20, le=400, strict=True)
    body_fat_percent: float | None = Field(default=None, ge=1, le=75, strict=True)
    notes: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def measured_value_required(self):
        if self.weight_kg is None and self.body_fat_percent is None:
            raise ValueError("At least one measurement required")
        if self.day > business_today():
            raise ValueError("Measurements cannot be in the future")
        return self


class BodyMeasurementCreate(BodyMeasurementInput):
    client_id: UUID


class BodyMeasurementUpdate(BodyMeasurementInput):
    version: int = Field(ge=1, strict=True)


class EnergyEstimateInputs(InputModel):
    age: int = Field(ge=19, le=100, strict=True)
    equation_sex: Literal["male", "female"]
    activity: Literal["inactive", "low_active", "active", "very_active"]
    general_adult: Literal[True]

    @field_validator("general_adult", mode="before")
    @classmethod
    def explicit_scope(cls, value):
        if value is not True:
            raise ValueError("Explicit eligibility confirmation required")
        return value


class EnergyEstimateRequest(InputModel):
    day: date
    context_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    inputs: EnergyEstimateInputs


class EnergyEstimateConfirm(InputModel):
    client_id: UUID
    version: int = Field(ge=0, strict=True)
    context_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    effective_from: date
    kcal: int = Field(ge=1000, le=5000, strict=True)
    method: Literal["dri2023-adult-v1"]
    inputs: EnergyEstimateInputs
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit target confirmation required")
        return value


class EnergyAdjustment(InputModel):
    purpose: Literal["fat_loss", "muscle_gain"]
    amount_kcal: int = Field(ge=50, le=500, multiple_of=50, strict=True)
    source: str = Field(min_length=1, max_length=200)
    review_on: date


class EnergyAdjustmentRequest(EnergyEstimateRequest):
    adjustment: EnergyAdjustment


class EnergyAdjustmentConfirm(EnergyEstimateConfirm):
    method: Literal["dri2023-user-adjustment-v1"]
    adjustment: EnergyAdjustment


class IntakeTargetChange(InputModel):
    client_id: UUID
    version: int = Field(ge=0, strict=True)
    context_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    effective_from: date
    kcal: int | None = Field(ge=1000, le=5000, strict=True)
    source: str = Field(default="", max_length=200)
    confirmed: Literal[True]
    general_adult: Literal[True] | None = None

    @field_validator("confirmed", "general_adult", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation required")
        return value

    @model_validator(mode="after")
    def active_target_requirements(self):
        if self.kcal is not None and (not self.source or self.general_adult is not True):
            raise ValueError("Target source and adult confirmation required")
        if self.kcal is None and self.source:
            raise ValueError("A paused target has no source")
        return self


class NutritionStandard(InputModel):
    mode: Literal["calculated", "fixed"] = "calculated"
    use_profile: bool = Field(default=False, strict=True)
    inputs: EnergyEstimateInputs | None = None
    fixed_kcal: int | None = Field(default=None, gt=1200, le=5000, strict=True)
    fat_loss_kcal: int | None = Field(default=None, ge=0, le=500, strict=True)
    muscle_gain_kcal: int | None = Field(default=None, ge=0, le=500, strict=True)
    offset_kcal: int = Field(default=0, ge=-500, le=500, strict=True)

    @model_validator(mode="after")
    def mode_fields(self):
        if self.mode == "calculated" and ((self.inputs is None and not self.use_profile) or self.fixed_kcal is not None):
            raise ValueError("Calculated targets require inputs, not a fixed target")
        if self.mode == "fixed" and (self.fixed_kcal is None or self.offset_kcal != 0):
            raise ValueError("Fixed targets require a calorie value and no extra offset")
        if self.mode == "fixed" and self.use_profile:
            raise ValueError("Profile rules must be calculated")
        return self


class NutritionTargetPreview(InputModel):
    day: date
    context_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    standard: NutritionStandard


class NutritionTargetSave(NutritionTargetPreview):
    client_id: UUID
    version: int = Field(ge=0, strict=True)
    kcal: int = Field(gt=1200, le=5000, strict=True)
    confirmed: Literal[True]
    general_adult: Literal[True]

    @field_validator("confirmed", "general_adult", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation required")
        return value


class NutritionDaySave(InputModel):
    client_id: UUID
    version: int = Field(ge=0, strict=True)
    context_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    day: date
    kcal: int | None = Field(gt=1200, le=5000, strict=True)
    confirmed: Literal[True]
    general_adult: Literal[True]

    @field_validator("confirmed", "general_adult", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation required")
        return value


class CoachBoundRequest(InputModel):
    coach_id: UUID | None = None
    coach_version: int | None = Field(default=None, ge=1, le=40, strict=True)

    @model_validator(mode="after")
    def paired_coach_reference(self):
        if (self.coach_id is None) != (self.coach_version is None):
            raise ValueError("Conversation identity and version must be paired")
        return self


class CoachCreate(InputModel):
    client_id: UUID
    day: date


class CoachVersion(InputModel):
    version: int = Field(ge=0, le=40, strict=True)


class CoachMessage(CoachVersion):
    client_id: UUID
    message: str = Field(min_length=1, max_length=2000)


class CoachUnderstand(CoachVersion):
    version: int = Field(ge=1, le=40, strict=True)
    client_id: UUID


class CoachAutoUnderstand(CoachUnderstand):
    auto_apply: bool = Field(default=False, strict=True)


class CoachReviewConfirm(CoachVersion):
    review_id: UUID
    reviewed: Literal[True]

    @field_validator("reviewed", mode="before")
    @classmethod
    def explicit_review(cls, value):
        if value is not True:
            raise ValueError("Explicit review required")
        return value


class CoachNote(InputModel):
    version: int = Field(ge=1, le=40, strict=True)
    avoid: list[str] = Field(max_length=100)
    preferences: list[str] = Field(max_length=5)
    training_caution: bool = Field(strict=True)
    diet_caution: bool = Field(strict=True)
    unresolved: bool = Field(strict=True)


class CoachTrainingUpdate(InputModel):
    source_text: str = Field(min_length=1, max_length=2000)
    minutes: int | None = Field(default=None, ge=1, le=300, strict=True)
    time_basis: Literal["daily", "session", "unspecified"] | None = None
    activity: Literal["walk", "cycle", "either", "swim"] | None = None
    focus: str | None = Field(default=None, min_length=1, max_length=100)
    equipment: str | None = Field(default=None, min_length=1, max_length=300)
    split: Literal["ppl", "four", "five"] | None = None
    weekly: bool | None = Field(default=None, strict=True)
    replace_from: str | None = Field(default=None, min_length=1, max_length=100)
    replace_with: str | None = Field(default=None, min_length=1, max_length=100)


class CoachMealChange(InputModel):
    meal_type: Literal["breakfast", "lunch", "dinner"]
    source_text: str = Field(min_length=1, max_length=2000)
    commands: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(min_length=1, max_length=3)


class CoachUnderstanding(InputModel):
    scope: Literal["meal", "aerobic", "strength", "other", "unclear"]
    command: str = Field(max_length=120)
    commands: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(default_factory=list, max_length=3)
    meal_changes: list[CoachMealChange] = Field(default_factory=list, max_length=3)
    training: CoachTrainingUpdate | None = None
    meal_types: list[Literal["breakfast", "lunch", "dinner"]] = Field(default_factory=list, max_length=3)
    clarification: Literal["none", "which_food", "which_meal", "which_request", "which_training", "multiple_requests", "unhandled_constraint"]
    notes: list[CoachNote] = Field(min_length=1, max_length=40)


class MealPlanRequest(CoachBoundRequest):
    client_id: UUID
    day: date
    meal_type: Literal["breakfast", "lunch", "dinner"] = "dinner"
    adult_general_diet: Literal[True] | None = None
    constraints_reviewed: Literal[True] | None = None
    excluded_foods: list[str] = Field(default_factory=list, max_length=100)
    plant_only: bool = Field(default=False, strict=True)
    intake_context_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    quick_recommendation: bool = Field(default=False, strict=True)

    @model_validator(mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        keys = ("adult_general_diet", "constraints_reviewed")
        if isinstance(value, dict) and any(key in value for key in keys) and any(value.get(key) is not True for key in keys):
            raise ValueError("Explicit confirmation required")
        return value


class MealIntakeReview(InputModel):
    day: date
    version: int = Field(ge=0, strict=True)
    context_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    record_state: Literal["complete", "none_yet"]
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit intake review required")
        return value


class MealIntakeReset(InputModel):
    day: date
    version: int = Field(ge=0, strict=True)


class PlanNutritionItem(InputModel):
    plan_id: UUID
    version: int = Field(ge=0, strict=True)


class PlanNutritionRequest(InputModel):
    client_id: UUID
    items: list[PlanNutritionItem] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def unique_plans(self):
        if len({item.plan_id for item in self.items}) != len(self.items):
            raise ValueError("Duplicate meal plans")
        return self


class MealConsent(InputModel):
    context_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    adult_general_diet: Literal[True]
    constraints_reviewed: Literal[True]

    @field_validator("adult_general_diet", "constraints_reviewed", mode="before")
    @classmethod
    def checked(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation required")
        return value


class PlanPortion(InputModel):
    food_id: str = Field(min_length=1, max_length=30)
    lower: int = Field(ge=1, le=600, strict=True)
    upper: int = Field(ge=1, le=600, strict=True)


class MealPlanSelection(InputModel):
    items: list[PlanPortion] = Field(min_length=3, max_length=7)
    chunk_ids: list[str] = Field(min_length=6, max_length=6)


class PlanPortionEdit(InputModel):
    client_id: UUID
    items: list[PlanPortion] = Field(min_length=3, max_length=7)
    reviewed: Literal[True]

    @field_validator("reviewed", mode="before")
    @classmethod
    def checked(cls, value):
        if value is not True:
            raise ValueError("Explicit portion review required")
        return value


class MealPlanAccept(InputModel):
    reviewed: Literal[True]

    @model_validator(mode="before")
    @classmethod
    def explicit_review(cls, value):
        if not isinstance(value, dict) or value.get("reviewed") is not True:
            raise ValueError("Explicit review required")
        return value


class MealInput(InputModel):
    day: date
    meal_type: Literal["breakfast", "lunch", "dinner", "snack"]
    name: str = Field(min_length=1, max_length=120)
    grams: float | None = Field(default=None, gt=0, le=10000)
    amount_description: str = Field(default="", max_length=120)
    kcal_per_100g: float | None = Field(default=None, ge=0, le=1000)
    protein_per_100g: float | None = Field(default=None, ge=0, le=100)
    carbs_per_100g: float | None = Field(default=None, ge=0, le=100)
    fat_per_100g: float | None = Field(default=None, ge=0, le=100)
    source: str = Field(default="", max_length=300)
    notes: str = Field(default="", max_length=500)
    nutrition_preview_id: UUID | None = Field(default=None, exclude=True)
    clear_nutrition_estimate: bool = Field(default=False, strict=True, exclude=True)

    @model_validator(mode="after")
    def check_nutrition(self):
        issue = portion_issue(self.name, self.grams, self.amount_description)
        if issue:
            raise ValueError(issue)
        fields = (self.kcal_per_100g, self.protein_per_100g, self.carbs_per_100g, self.fat_per_100g)
        if any(value is not None for value in fields) and not self.source:
            raise ValueError("Nutrition values require a source")
        macros = (self.protein_per_100g, self.carbs_per_100g, self.fat_per_100g)
        if sum(value or 0 for value in macros) > 100:
            raise ValueError("Macronutrients cannot exceed 100 g per 100 g")
        if self.nutrition_preview_id and (self.clear_nutrition_estimate or any(value is not None for value in fields)):
            raise ValueError("Choose either a model estimate or manually supplied nutrition")
        return self


class MealCreate(MealInput):
    client_id: UUID


class MealBatchCreate(InputModel):
    items: list[MealCreate] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def check_batch(self):
        if len({item.client_id for item in self.items}) != len(self.items):
            raise ValueError("Each food needs a distinct submission identifier")
        if len({(item.day, item.meal_type) for item in self.items}) != 1:
            raise ValueError("A meal batch must share one date and meal type")
        return self


WorkoutIntensity = Literal["unknown", "normal_assumed", "normal", "light", "moderate", "vigorous"]


class PerformanceSet(InputModel):
    load_kg: float = Field(gt=0, le=500, strict=True)
    reps: int = Field(ge=1, le=100, strict=True)
    rir: int | None = Field(default=None, ge=0, le=10, strict=True)


class WorkoutPerformance(InputModel):
    exercise_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,49}$")
    load_basis: Literal["total", "each", "stack"]
    equipment_label: str = Field(min_length=1, max_length=80)
    increment_kg: float | None = Field(default=None, gt=0, le=50, strict=True)
    technique_stable: bool = Field(default=False, strict=True)
    sets: list[PerformanceSet] = Field(min_length=1, max_length=12)


class WorkoutInput(InputModel):
    day: date
    name: str = Field(min_length=1, max_length=120)
    minutes: int = Field(gt=0, le=600, strict=True)
    status: Literal["planned", "completed"] = "planned"
    notes: str = Field(default="", max_length=500)
    intensity: WorkoutIntensity = "unknown"
    details: str = Field(default="", max_length=300)
    weight_kg: float | None = Field(default=None, ge=20, le=400)
    sync_weight: bool = Field(default=False, strict=True, exclude=True)
    performance: WorkoutPerformance | None = None
    calorie_preview_id: UUID | None = Field(default=None, exclude=True)
    clear_calorie_estimate: bool = Field(default=False, strict=True, exclude=True)

    @model_validator(mode="after")
    def valid_estimate_command(self):
        if self.sync_weight and (self.weight_kg is None or self.status != "completed"):
            raise ValueError("Weight synchronization requires a completed record and a weight")
        if self.calorie_preview_id and self.clear_calorie_estimate:
            raise ValueError("Choose a preview or clear the estimate")
        return self


class WorkoutCreate(WorkoutInput):
    client_id: UUID


class WorkoutBatchCreate(InputModel):
    items: list[WorkoutCreate] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def consistent_batch(self):
        if len({item.weight_kg for item in self.items if item.sync_weight}) > 1:
            raise ValueError("Use one current body weight per batch")
        if len({item.client_id for item in self.items}) != len(self.items) or len({item.day for item in self.items}) != 1:
            raise ValueError("Use unique identifiers and the same date")
        return self


class WorkoutParsedItem(InputModel):
    name: str = Field(min_length=1, max_length=120)
    minutes: int | None = Field(default=None, ge=1, le=600, strict=True)
    status: Literal["unknown", "planned", "completed"] = "unknown"
    intensity: WorkoutIntensity = "normal_assumed"
    details: str = Field(default="", max_length=300)

    @field_validator("intensity", mode="before")
    @classmethod
    def normalize_intensity(cls, value):
        # Only known equivalents from model output; do not coerce arbitrary values.
        aliases = {"unknown": "normal_assumed", "正常": "normal", "正常强度": "normal",
                   "低强度": "light", "较低强度": "light", "高强度": "vigorous", "过高强度": "vigorous"}
        return aliases.get(value, value) if isinstance(value, str) else value


class TrainingExecutionItem(InputModel):
    name: str = Field(default="", max_length=120)
    minutes: int | None = Field(default=None, ge=1, le=600, strict=True)
    intensity: WorkoutIntensity = "normal_assumed"
    details: str = Field(default="", max_length=300)
    calorie_preview_id: UUID | None = None


class TrainingExecutionUpdate(InputModel):
    version: int = Field(ge=1, strict=True)
    day: date
    weight_kg: float | None = Field(default=None, ge=20, le=400)
    sync_weight: bool = Field(default=False, strict=True)
    notes: str = Field(default="", max_length=500)
    items: list[TrainingExecutionItem] = Field(min_length=1, max_length=30)


class TrainingExecutionCommit(MealPlanAccept):
    version: int = Field(ge=1, strict=True)


class WorkoutParsed(InputModel):
    items: list[WorkoutParsedItem] = Field(default_factory=list, max_length=30)
    questions: list[Annotated[str, Field(min_length=1, max_length=240)]] = Field(default_factory=list, max_length=10)


class WorkoutExtractedItem(WorkoutParsedItem):
    source_text: str = Field(min_length=1, max_length=2000)


class WorkoutExtraction(WorkoutParsed):
    items: list[WorkoutExtractedItem] = Field(default_factory=list, max_length=30)


class WorkoutPreviewRequest(InputModel):
    client_id: UUID
    name: str = Field(min_length=1, max_length=120)
    minutes: int = Field(ge=1, le=600, strict=True)
    intensity: WorkoutIntensity = "unknown"
    details: str = Field(default="", max_length=300)
    weight_kg: float = Field(ge=20, le=400, strict=True)


class WorkoutPreviewBatch(InputModel):
    items: list[WorkoutPreviewRequest] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({item.client_id for item in self.items}) != len(self.items):
            raise ValueError("Each workout needs a unique preview identifier")
        return self


class AgentRequest(InputModel):
    day: date
    message: str = Field(min_length=1, max_length=4000)


class MealTextRequest(InputModel):
    text: str = Field(min_length=1, max_length=2000)


class MealDraftItem(InputModel):
    name: str = Field(min_length=1, max_length=120)
    grams: float | None = Field(default=None, gt=0, le=10000, strict=True)
    amount_description: str = Field(default="", max_length=120)
    confidence: Literal["needs_confirmation", "unknown"] = "needs_confirmation"


class MealDraft(InputModel):
    input_type: Literal["text", "photo"]
    items: list[MealDraftItem] = Field(default_factory=list, max_length=30)
    questions: list[Annotated[str, Field(min_length=1, max_length=240)]] = Field(default_factory=list, max_length=10)
    status: Literal["awaiting_confirmation"] = "awaiting_confirmation"


class NutritionPreviewRequest(MealDraftItem):
    client_id: UUID


class NutritionPreviewBatch(InputModel):
    items: list[NutritionPreviewRequest] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def distinct_requests(self):
        if len({item.client_id for item in self.items}) != len(self.items):
            raise ValueError("Each food needs its own preview identifier")
        return self


class DraftCreate(MealTextRequest):
    client_id: UUID
    day: date
    meal_type: Literal["breakfast", "lunch", "dinner", "snack"]


class DraftVersion(InputModel):
    version: int = Field(ge=1, strict=True)


class DraftUpdate(DraftVersion):
    text: str | None = Field(default=None, min_length=1, max_length=2000)
    day: date
    meal_type: Literal["breakfast", "lunch", "dinner", "snack"]
    items: list[MealDraftItem] = Field(max_length=30)
    notes: str = Field(default="", max_length=500)


class DraftConfirm(DraftVersion):
    confirmation_id: UUID


class NutritionRange(InputModel):
    lower: float = Field(ge=0, le=100000, strict=True)
    upper: float = Field(ge=0, le=100000, strict=True)

    @model_validator(mode="after")
    def ordered(self):
        if self.lower > self.upper:
            raise ValueError("Invalid nutrition range")
        return self


class NutritionEstimateItem(InputModel):
    index: int = Field(ge=0, le=4, strict=True)
    status: Literal["estimated", "unknown"]
    kcal: NutritionRange | None = None
    protein: NutritionRange | None = None
    carbs: NutritionRange | None = None
    fat: NutritionRange | None = None
    assumptions: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(default_factory=list, max_length=3)
    question: str = Field(default="", max_length=240)

    @model_validator(mode="after")
    def consistent(self):
        values = (self.kcal, self.protein, self.carbs, self.fat)
        if self.status == "estimated":
            if any(value is None for value in values) or not self.assumptions or self.question:
                raise ValueError("Estimates require all ranges and assumptions, without a question")
            if any(value.upper > 10000 for value in (self.protein, self.carbs, self.fat)):
                raise ValueError("Invalid macronutrient range")
        elif any(value is not None for value in values) or not self.question:
            raise ValueError("Unknown nutrition needs a question, never invented numbers")
        return self


class NutritionEstimateResponse(InputModel):
    items: list[NutritionEstimateItem] = Field(min_length=1, max_length=5)


class WorkoutCalorieItem(InputModel):
    index: int = Field(ge=0, le=4, strict=True)
    status: Literal["estimated", "unknown"]
    kcal: NutritionRange | None = None
    assumptions: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(default_factory=list, max_length=3)
    question: str = Field(default="", max_length=240)

    @model_validator(mode="before")
    @classmethod
    def empty_optional_fields(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            if value.get("question") is None:
                value["question"] = ""
            if value.get("assumptions") is None:
                value["assumptions"] = []
        return value

    @model_validator(mode="after")
    def consistent(self):
        if self.status == "estimated" and (self.kcal is None or not self.assumptions or self.question):
            raise ValueError("An estimate requires a range and assumptions")
        if self.status == "unknown" and (self.kcal is not None or not self.question):
            raise ValueError("Unknown calories require a question and no numbers")
        return self


class WorkoutCalorieResponse(InputModel):
    items: list[WorkoutCalorieItem] = Field(min_length=1, max_length=5)


class KnowledgeHit(InputModel):
    source_id: str
    title: str
    locator: str
    excerpt: str
    chunk_id: str = ""
    original_title: str = ""
    publisher: str = ""
    url: str = ""
    origin: Literal["public_web", "curated_text"] = "public_web"
    topic: Literal["nutrition", "training"] = "training"
    source_version: str = ""
    reviewed_on: str = ""
    review_due: str = ""
    scope: str = ""
    license: str = ""
    license_url: str = ""
    notice: str = ""


class TrainingPlanRequest(CoachBoundRequest):
    client_id: UUID
    day: date
    daily_minutes: int = Field(ge=15, le=120, strict=True)
    time_basis: Literal["daily", "session"] = "daily"
    activity: Literal["walk", "cycle", "either"] = "walk"
    bicycle_available: bool = Field(default=False, strict=True)
    general_adult: Literal[True]
    constraints_reviewed: Literal[True]

    @field_validator("general_adult", "constraints_reviewed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation required")
        return value


class TrainingPlanSelection(InputModel):
    activity_id: Literal["walk", "cycle"]
    main_minutes: int = Field(ge=5, le=30, strict=True)
    chunk_ids: list[str] = Field(min_length=4, max_length=4)


class KnowledgeQuery(InputModel):
    query: str = Field(default="", max_length=300)
    topic: Literal["all", "nutrition", "training"] = "all"
    limit: int = Field(default=12, ge=1, le=20, strict=True)


class KnowledgeQuestion(InputModel):
    question: str = Field(min_length=1, max_length=1000)


class KnowledgeSelection(InputModel):
    status: Literal["answered", "insufficient", "personal_scope"]
    chunk_ids: list[Annotated[str, Field(min_length=1, max_length=161)]] = Field(max_length=3)

    @model_validator(mode="after")
    def valid_selection(self):
        if (self.status == "answered") != bool(self.chunk_ids) or len(set(self.chunk_ids)) != len(self.chunk_ids):
            raise ValueError("Selection status and evidence disagree")
        return self
