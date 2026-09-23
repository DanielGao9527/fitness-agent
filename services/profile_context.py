"""Read current profile fields with a read-only bridge for previously confirmed formula inputs."""
import json
from datetime import date
from services.business_time import business_today

from fastapi import HTTPException

from schemas import Profile, EnergyEstimateInputs

ENERGY_FIELDS = ("age", "equation_sex", "activity")


def read_profile(connection, user_id):
    row = connection.execute("SELECT payload FROM profiles WHERE user_id=?", (user_id,)).fetchone()
    saved = json.loads(row[0]) if row else {}
    profile = {**Profile().model_dump(), **{key: value for key, value in saved.items() if key in Profile.model_fields}}
    if profile["training_split"] not in ("ppl", "four", "five"):
        profile["training_split"] = "ppl"
    if any(key not in saved for key in ENERGY_FIELDS):
        rows = connection.execute("SELECT input_payload FROM intake_targets WHERE user_id=? AND effective_from<=? "
                                  "ORDER BY effective_from DESC,version DESC", (user_id, business_today().isoformat()))
        for row in rows:
            payload = json.loads(row[0])
            if payload.get("scope") == "day":
                continue
            inputs = (payload.get("standard") or {}).get("inputs") or (payload.get("estimate") or {}).get("inputs") or {}
            for key in ENERGY_FIELDS:
                if key not in saved and key in inputs:
                    profile[key] = inputs[key]
            break
    return profile


def energy_inputs(profile):
    missing = [label for key, label in (("age", "年龄"), ("equation_sex", "生理性别"), ("activity", "整体活动水平"))
               if profile.get(key) is None]
    if missing:
        raise HTTPException(409, "请先在个人档案保存" + "、".join(missing) + "，再预览长期标准")
    return EnergyEstimateInputs(**{key: profile[key] for key in ENERGY_FIELDS}, general_adult=True)
