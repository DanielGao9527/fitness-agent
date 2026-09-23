"""Validated local actions; evidence eligibility is checked separately on every request."""
import hashlib
from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_validator

from config import ROOT
from schemas import InputModel

DATA_PATH = ROOT / "data/training/exercises.json"
SYSTEMS_PATH = ROOT / "data/training/systems.json"
Focus = Literal["chest", "back", "legs", "core", "shoulders", "arms"]
Resource = Literal["floor", "dumbbell", "band", "seat", "chest-machine", "lat-machine",
                   "row-machine", "leg-press-machine", "knee-extension-machine",
                   "hamstring-machine", "cable-triceps", "barbell", "bench", "incline-bench",
                   "rack", "safeties", "cable", "dual-pulley", "adjustable-pulley", "handles",
                   "cable-bar", "rope", "backrest", "pec-deck", "shoulder-machine",
                   "curl-machine", "cable-row-machine", "pullup-bar", "band-anchor",
                   "seated-curl-machine", "kettlebell", "pullup-band", "calf-platform",
                   "single-handle", "ankle-cuff", "stable-support", "loop-band",
                   "smith-machine", "smith-stops", "adductor-machine", "abductor-machine",
                   "rear-delt-machine", "triceps-machine", "abdominal-machine"]


class Exercise(InputModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,49}$")
    name: str = Field(min_length=1, max_length=100)
    focus: Focus
    needs: set[Resource] = Field(min_length=1, max_length=8)
    loads: set[Focus] = Field(min_length=1, max_length=6)
    source: str = Field(pattern=r"^[a-z0-9-]+:[a-z0-9-]+$")
    cue: str = Field(min_length=1, max_length=300)
    unit: str = Field(min_length=1, max_length=80)
    pattern: str = Field(pattern=r"^[a-z][a-z0-9-]{0,49}$")
    level: Literal["beginner", "experienced"] = "beginner"
    target: str = Field(default="", max_length=100)


class ActionCatalog(InputModel):
    version: str = Field(min_length=1, max_length=80)
    notice: str = Field(min_length=1, max_length=500)
    exercises: list[Exercise] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique(self):
        if len({item.id for item in self.exercises}) != len(self.exercises):
            raise ValueError("Duplicate exercise id")
        if any(item.focus not in item.loads for item in self.exercises):
            raise ValueError("Missing direct load")
        return self


class TrainingSystem(InputModel):
    split: Literal["full_body", "upper_lower", "ppl", "four", "five"]
    title: str = Field(min_length=1, max_length=100)
    reference: str = Field(min_length=1, max_length=200)
    source_ids: list[Annotated[str, Field(pattern=r"^[a-z0-9-]+:[a-z0-9-]+$")]] = Field(min_length=1, max_length=5)
    original_structure: str = Field(min_length=1, max_length=500)
    adaptation: str = Field(min_length=1, max_length=500)
    working_sets: dict[Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,49}$")],
                       Annotated[int, Field(ge=3, le=4, strict=True)]] = Field(default_factory=dict, max_length=40)


class SystemCatalog(InputModel):
    version: str = Field(min_length=1, max_length=80)
    systems: list[TrainingSystem] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def complete(self):
        if len({item.split for item in self.systems}) != 5:
            raise ValueError("Missing or duplicate training structure")
        for item in self.systems:
            if "acsm-2026:programming" not in item.source_ids or len(set(item.source_ids)) != len(item.source_ids):
                raise ValueError("Missing principles or duplicate references")
        return self


def read_systems():
    try:
        raw = SYSTEMS_PATH.read_bytes()
        if len(raw) > 30_000:
            raise ValueError("Training structures too large")
        data = SystemCatalog.model_validate_json(raw)
        return {item.split: item.model_dump() for item in data.systems}, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, ValidationError):
        return {}, "unavailable"


def read_catalog():
    try:
        raw = DATA_PATH.read_bytes()
        if len(raw) > 200_000:
            raise ValueError("Action catalog too large")
        data = ActionCatalog.model_validate_json(raw)
        return [item.model_dump() for item in data.exercises], hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, ValidationError):
        return [], "unavailable"
