"""Maintenance reference and explicit user adjustments, not personal prescriptions."""
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException


METHOD = "dri2023-adult-v1"
ADJUSTMENT_METHOD = "dri2023-user-adjustment-v1"
SOURCE_URL = "https://www.canada.ca/en/health-canada/services/food-nutrition/healthy-eating/dietary-reference-intakes/tables/equations-estimate-energy-requirement.html"
SOURCE_LABEL = "Health Canada / DRI 2023 EER"
REVIEWED_ON = "2026-09-19"
ACTIVITIES = {"inactive": "日常活动为主", "low_active": "较活跃", "active": "活跃", "very_active": "非常活跃"}

# Health Canada adult table, 2025-11-19: intercept, age (years), height (cm), weight (kg).
COEFFICIENTS = {
    "male": {
        "inactive": ("753.07", "-10.83", "6.50", "14.10"),
        "low_active": ("581.47", "-10.83", "8.30", "14.94"),
        "active": ("1004.82", "-10.83", "6.52", "15.91"),
        "very_active": ("-517.88", "-10.83", "15.61", "19.11"),
    },
    "female": {
        "inactive": ("584.90", "-7.01", "5.72", "11.71"),
        "low_active": ("575.77", "-7.01", "6.60", "12.14"),
        "active": ("710.25", "-7.01", "6.54", "12.34"),
        "very_active": ("511.83", "-7.01", "9.07", "12.56"),
    },
}


def maintenance_reference(measurements, inputs):
    height, weight = measurements["height_cm"], measurements["weight_kg"]
    if height is None or weight is None:
        raise HTTPException(409, "请先在个人档案中填写并核对身高、体重；原目标保持不变")
    height, weight = Decimal(str(height)), Decimal(str(weight))
    bmi = weight / (height / 100) ** 2
    if not (140 <= height <= 210 and 40 <= weight <= 200 and Decimal("18.5") <= bmi < 40):
        raise HTTPException(409, "当前体征超出这个简化测算入口的支持范围；请核对资料或向专业人士确认，原目标和记录保留")
    coefficients = COEFFICIENTS[inputs.equation_sex][inputs.activity]
    base, age_factor, height_factor, weight_factor = map(Decimal, coefficients)
    raw = base + age_factor * inputs.age + height_factor * height + weight_factor * weight
    if not 1000 <= raw <= 5000:
        raise HTTPException(409, "估算超出当前目标入口的支持范围，未截断或保存数值；请核对资料")
    kcal = int((raw / 50).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * 50)
    return {
        "method": METHOD, "kcal": kcal, "unrounded_kcal": float(raw), "rounding_kcal": 50,
        "basis": "estimated_maintenance", "exercise_added": False,
        "inputs": inputs.model_dump(), "measurements": measurements,
        "source": SOURCE_LABEL, "source_url": SOURCE_URL, "source_version": "2025-11-19",
        "reviewed_on": REVIEWED_ON, "coefficients": list(coefficients),
    }


def adjusted_reference(measurements, inputs, adjustment, effective_from):
    maintenance = maintenance_reference(measurements, inputs)
    if not 1 <= (adjustment.review_on - effective_from).days <= 28:
        raise HTTPException(409, "复核日期需在生效后1–28天内；请重新核对日期，原目标不变")
    # Product error guards, not a recommended dose or assurance of medical suitability.
    if Decimal(adjustment.amount_kcal) > Decimal(maintenance["kcal"]) * Decimal("0.20"):
        raise HTTPException(409, "调整量超出此简化入口支持范围（维持参考的20%）；未修改原目标，请核对已有方案")
    direction = -1 if adjustment.purpose == "fat_loss" else 1
    kcal = maintenance["kcal"] + direction * adjustment.amount_kcal
    if not 1200 < kcal <= 5000:
        raise HTTPException(409, "调整结果超出当前入口范围，未截断或保存；低能量饮食需专业支持，请核对已有方案")
    return {
        "method": ADJUSTMENT_METHOD, "kcal": kcal, "basis": "user_adjusted_reference", "exercise_added": False,
        "inputs": inputs.model_dump(), "measurements": measurements,
        "maintenance": maintenance, "adjustment": adjustment.model_dump(mode="json"),
        "source": SOURCE_LABEL + " + 用户已有调整", "reviewed_on": REVIEWED_ON,
    }
