from typing import Protocol

from config import Settings


class ModelNotConfigured(RuntimeError):
    pass


class ModelError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 502):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class TextModel(Protocol):
    def generate(self, *, system_prompt: str, message: str) -> str: ...


class ChatModel(Protocol):
    def generate(self, *, system_prompt: str, message: str, context: dict) -> str: ...


class DisabledModel:
    def generate(self, *, system_prompt: str, message: str, context: dict) -> str:
        raise ModelNotConfigured("通用 AI 助手尚未开放；当前可使用档案、饮食和训练记录。")


def create_chat_model() -> ChatModel:
    return DisabledModel()


def create_meal_text_model(settings: Settings) -> TextModel:
    if settings.model_provider == "disabled":
        raise ModelError("MODEL_NOT_CONFIGURED", "文字解析尚未启用，手动记录仍可使用。", 503)
    if settings.model_provider != "qwen":
        raise ModelError("MODEL_CONFIGURATION_ERROR", "当前文字解析仅支持千问，请检查模型配置。", 503)
    if not settings.qwen_api_key:
        raise ModelError("MODEL_NOT_CONFIGURED", "请在本机配置千问 API Key 后再使用文字解析。", 503)
    from model.qwen import QwenTextModel

    return QwenTextModel(settings)


def meal_text_status(settings: Settings) -> str:
    try:
        create_meal_text_model(settings)
    except ModelError as error:
        return "not_configured" if error.code == "MODEL_NOT_CONFIGURED" else "configuration_error"
    return "configured_unverified"


def create_coach_model(settings: Settings) -> TextModel:
    if not settings.coach_enabled:
        raise ModelError("MODEL_NOT_CONFIGURED", "自由描述理解尚未启用，常用问题和手动记录仍可使用。", 503)
    return create_meal_text_model(settings)


def coach_status(settings: Settings) -> str:
    try:
        create_coach_model(settings)
    except ModelError as error:
        return "not_configured" if error.code == "MODEL_NOT_CONFIGURED" else "configuration_error"
    return "configured_unverified"


def create_nutrition_model(settings: Settings) -> TextModel:
    if not settings.nutrition_enabled:
        raise ModelError("MODEL_NOT_CONFIGURED", "营养估算尚未启用，仍可保存食物记录。", 503)
    return create_meal_text_model(settings)


def nutrition_status(settings: Settings) -> str:
    try:
        create_nutrition_model(settings)
    except ModelError as error:
        return "not_configured" if error.code == "MODEL_NOT_CONFIGURED" else "configuration_error"
    return "configured_unverified"


def create_workout_model(settings: Settings) -> TextModel:
    if not settings.workout_enabled:
        raise ModelError("MODEL_NOT_CONFIGURED", "训练解析与估算尚未启用，手动记录仍可使用。", 503)
    return create_meal_text_model(settings)


def workout_status(settings: Settings) -> str:
    try:
        create_workout_model(settings)
    except ModelError as error:
        return "not_configured" if error.code == "MODEL_NOT_CONFIGURED" else "configuration_error"
    return "configured_unverified"


def create_knowledge_model(settings: Settings) -> TextModel:
    if not settings.knowledge_qa_enabled:
        raise ModelError("MODEL_NOT_CONFIGURED", "资料问答尚未启用，仍可免费检索资料。", 503)
    return create_meal_text_model(settings)


def knowledge_qa_status(settings: Settings) -> str:
    try:
        create_knowledge_model(settings)
    except ModelError as error:
        return "not_configured" if error.code == "MODEL_NOT_CONFIGURED" else "configuration_error"
    return "configured_unverified"


def create_meal_plan_model(settings: Settings) -> TextModel:
    if not settings.meal_plan_enabled:
        raise ModelError("MODEL_NOT_CONFIGURED", "下一餐建议尚未启用，仍可使用记录和资料检索。", 503)
    return create_meal_text_model(settings)


def meal_plan_status(settings: Settings) -> str:
    try:
        create_meal_plan_model(settings)
    except ModelError as error:
        return "not_configured" if error.code == "MODEL_NOT_CONFIGURED" else "configuration_error"
    return "configured_unverified"


def create_training_plan_model(settings: Settings) -> TextModel:
    if not settings.training_plan_enabled:
        raise ModelError("MODEL_NOT_CONFIGURED", "训练建议尚未启用，仍可手动记录训练。", 503)
    return create_meal_text_model(settings)


def training_plan_status(settings: Settings) -> str:
    try:
        create_training_plan_model(settings)
    except ModelError as error:
        return "not_configured" if error.code == "MODEL_NOT_CONFIGURED" else "configuration_error"
    return "configured_unverified"
