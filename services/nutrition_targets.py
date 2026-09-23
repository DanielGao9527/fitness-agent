"""Confirmed standing rules and date-only overrides, using the existing target ledger."""
import json
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

from fastapi import HTTPException

from schemas import NutritionStandard
from services.energy_estimates import maintenance_reference
from services.profile_context import energy_inputs, ENERGY_FIELDS
from services.business_time import business_today

METHOD = "standing-nutrition-v1"
SOURCES = [
    {"title": "Health Canada / DRI 2023 EER", "url": "https://www.canada.ca/en/health-canada/services/food-nutrition/healthy-eating/dietary-reference-intakes/tables/equations-estimate-energy-requirement.html"},
    {"title": "Garthe 2011：运动员不同减重速度研究", "url": "https://pubmed.ncbi.nlm.nih.gov/21558571/"},
    {"title": "Helms 2023：训练者5%与15%能量盈余研究", "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC10620361/"},
]
NOTICE = "缺口10%、盈余5%是产品选择的温和初始参考，不是研究规定的个人最佳比例。实际需求和体重变化可能不同；数周后复核，不以凑数强迫进食或禁食。"


def calculate(measurements, standard):
    base = maintenance_reference(measurements, standard.inputs) if standard.inputs else None
    goal = measurements["goal"]
    if standard.mode == "fixed":
        kcal = standard.fixed_kcal
        adjustment = None
    else:
        ratio = {"fat_loss": "-0.10", "muscle_gain": "0.05", "maintain": "0"}[goal]
        default = int((Decimal(base["kcal"]) * Decimal(ratio) / 50).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * 50)
        custom = getattr(standard, goal + "_kcal", None)
        adjustment = default if custom is None else (-custom if goal == "fat_loss" else custom)
        adjustment += standard.offset_kcal
        if abs(adjustment) > min(500, base["kcal"] * .20):
            raise HTTPException(409, "净调整超过当前入口范围（500 kcal或维持参考的20%），请核对调整量；没有保存或截断数值")
        if (goal == "fat_loss" and adjustment > 0) or (goal == "muscle_gain" and adjustment < 0):
            raise HTTPException(409, "整体微调与当前减脂/增肌方向相反，请修改目标或调整量")
        kcal = base["kcal"] + adjustment
    if not 1200 < kcal <= 5000:
        raise HTTPException(409, "结果超出当前简化入口范围（大于1200且不超过5000 kcal），请核对资料或咨询专业人士")
    return {"method": METHOD, "kcal": kcal, "maintenance": base, "goal": goal,
            "mode": standard.mode, "adjustment_kcal": adjustment,
            "offset_kcal": standard.offset_kcal, "sources": SOURCES, "notice": NOTICE,
            "measurements": measurements, "exercise_added": False, "reviewed_on": "2026-09-19"}


def insert(connection, user_id, state, day, kcal, payload, source, client_id=None):
    from services.intake_targets import encode
    connection.execute("INSERT INTO intake_targets(id,user_id,client_id,version,effective_from,kcal,source,context_hash,input_payload) "
                       "VALUES (?,?,?,?,?,?,?,?,?)", (str(uuid4()), user_id, str(client_id or uuid4()), state["version"] + 1,
                       day.isoformat(), kcal, source, state["context_hash"], encode(payload)))


def resolved_standard(standard, current):
    if standard.use_profile:
        return standard.model_copy(update={"inputs": energy_inputs(current["profile_inputs"])})
    return standard


def profile_changed(connection, user_id, previous, profile):
    """New facts change today's onward rule snapshot, never rewrite earlier ledger rows."""
    from services.intake_targets import target_state
    fields = ("height_cm", "weight_kg", "goal", *ENERGY_FIELDS)
    if all(previous.get(key) == profile.get(key) for key in fields):
        return
    day = business_today()
    state = target_state(connection, user_id, day)
    standard_data = (state.get("baseline") or {}).get("standard")
    if not standard_data:
        return
    standard = NutritionStandard.model_validate(standard_data)
    try:
        if standard.mode == "calculated" and (all(profile.get(key) is not None for key in ENERGY_FIELDS)
                                              or any(previous.get(key) != profile.get(key) for key in ENERGY_FIELDS)):
            standard = standard.model_copy(update={"use_profile": True})
        standard = resolved_standard(standard, state)
        standard_data = standard.model_dump(mode="json")
        result = calculate(state["measurements"], standard)
        payload = {"scope": "baseline", "standard": standard_data, "calculation": result, "status": "active"}
        kcal = result["kcal"]
    except HTTPException as error:
        payload = {"scope": "baseline", "standard": standard_data, "status": "needs_input", "error": str(error.detail)}
        kcal = None
    insert(connection, user_id, state, day, kcal, payload, "长期营养标准 · 档案变更重算")


class NutritionTargetService:
    def __init__(self, database, user_id):
        self.database, self.user_id = database, user_id

    def preview(self, body):
        from services.intake_targets import target_state, IntakeTargetService
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            current = target_state(connection, self.user_id, body.day)
            if current["context_hash"] != body.context_hash:
                raise HTTPException(409, "档案已变化，请重新核对营养标准")
            IntakeTargetService._check_scope(current)
            standard = resolved_standard(body.standard, current)
            return {"target_state": current, "calculation": calculate(current["measurements"], standard)}

    def save(self, body, *, daily=False):
        from services.intake_targets import target_state, encode, IntakeTargetService
        request = body.model_dump(mode="json")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = target_state(connection, self.user_id, body.day)
            old = connection.execute("SELECT input_payload FROM intake_targets WHERE user_id=? AND client_id=?",
                                     (self.user_id, str(body.client_id))).fetchone()
            if old:
                if encode(json.loads(old[0]).get("request")) != encode(request):
                    raise HTTPException(409, "提交标识已用于不同内容，请重新核对")
                return current
            if current["version"] != body.version or current["context_hash"] != body.context_hash:
                raise HTTPException(409, "目标或档案已变化，输入保留，请重新打开核对")
            IntakeTargetService._check_scope(current)
            payload = {"request": request, "scope": "day" if daily else "baseline"}
            if not daily:
                # Standing changes start today; past and future date-only choices use the other endpoint.
                if body.day != business_today():
                    raise HTTPException(409, "长期标准从今天起生效；其他日期请使用单日调整")
                standard = resolved_standard(body.standard, current)
                result = calculate(current["measurements"], standard)
                if result["kcal"] != body.kcal:
                    raise HTTPException(409, "计算结果已变化，请重新预览后确认")
                payload.update(standard=standard.model_dump(mode="json"), calculation=result, status="active")
            insert(connection, self.user_id, current, body.day, body.kcal, payload,
                   "仅本日自定义目标" if daily else "长期营养标准 · 用户确认", body.client_id)
            return target_state(connection, self.user_id, body.day)
