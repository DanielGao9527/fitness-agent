import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
DEFAULT_QWEN_MODEL = "qwen3.7-plus"


@dataclass(frozen=True)
class Settings:
    database_path: Path
    knowledge_source_path: Path = ROOT / "data/knowledge/sources.json"
    knowledge_index_path: Path | None = None
    cookie_secure: bool = False
    session_seconds: int = 7 * 24 * 60 * 60
    model_provider: str = "disabled"
    qwen_api_key: str = field(default="", repr=False)
    qwen_model: str = DEFAULT_QWEN_MODEL
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    speech_enabled: bool = False
    photo_enabled: bool = False
    qwen_vision_model: str = "qwen3.7-plus"
    nutrition_enabled: bool = False
    workout_enabled: bool = False
    knowledge_qa_enabled: bool = False
    meal_plan_enabled: bool = False
    training_plan_enabled: bool = False
    coach_enabled: bool = False
    qwen_asr_model: str = "qwen3-asr-flash"
    ai_user_daily_limit: int = 100
    ai_global_daily_limit: int = 100
    registration_enabled: bool = True
    access_mode: str = "local"
    public_origin: str = ""
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "[::1]", "testserver")

    def validate_access(self):
        if self.access_mode not in ("local", "shared"):
            raise ValueError("FITNESS_ACCESS_MODE must be local or shared")
        if not self.allowed_hosts or any(not host or any(char in host for char in "*/:@ ")
            for host in self.allowed_hosts if host != "[::1]"):
            raise ValueError("Explicit allowed hostnames are required (no wildcards or ports)")
        if self.access_mode == "shared":
            origin = urlsplit(self.public_origin)
            origin.port  # Validate malformed or out-of-range ports before starting.
            if (origin.scheme != "https" or not origin.hostname or origin.username or origin.password
                or origin.path not in ("", "/") or origin.query or origin.fragment
                or origin.hostname not in self.allowed_hosts or not self.cookie_secure):
                raise ValueError("Shared mode requires an explicit HTTPS origin, matching host and secure cookies")
            if self.registration_enabled:
                raise ValueError("Shared mode requires registration closed; prepare accounts locally first")

    @classmethod
    def from_env(cls):
        path = Path(os.getenv("FITNESS_DB_PATH", "data/runtime/fitness.sqlite3"))
        return cls(
            database_path=path if path.is_absolute() else ROOT / path,
            registration_enabled=os.getenv("FITNESS_REGISTRATION_ENABLED", "true").lower() == "true",
            access_mode=os.getenv("FITNESS_ACCESS_MODE", "local").strip().lower(),
            public_origin=os.getenv("FITNESS_PUBLIC_ORIGIN", "").strip().rstrip("/"),
            allowed_hosts=tuple(host.strip().lower() for host in os.getenv(
                "FITNESS_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver").split(",") if host.strip()),
            cookie_secure=os.getenv("FITNESS_COOKIE_SECURE", "false").lower() == "true",
            model_provider=os.getenv("FITNESS_MODEL_PROVIDER", "disabled").strip().lower(),
            qwen_api_key=os.getenv("DASHSCOPE_API_KEY", "").strip(),
            qwen_model=os.getenv("FITNESS_QWEN_MODEL", DEFAULT_QWEN_MODEL).strip(),
            speech_enabled=os.getenv("FITNESS_SPEECH_ENABLED", "false").lower() == "true",
            photo_enabled=os.getenv("FITNESS_PHOTO_ENABLED", "false").lower() == "true",
            qwen_vision_model=os.getenv("FITNESS_QWEN_VISION_MODEL", DEFAULT_QWEN_MODEL).strip(),
            nutrition_enabled=os.getenv("FITNESS_NUTRITION_ENABLED", "false").lower() == "true",
            workout_enabled=os.getenv("FITNESS_WORKOUT_ENABLED", "false").lower() == "true",
            knowledge_qa_enabled=os.getenv("FITNESS_KNOWLEDGE_QA_ENABLED", "false").lower() == "true",
            meal_plan_enabled=os.getenv("FITNESS_MEAL_PLAN_ENABLED", "false").lower() == "true",
            training_plan_enabled=os.getenv("FITNESS_TRAINING_PLAN_ENABLED", "false").lower() == "true",
            coach_enabled=os.getenv("FITNESS_COACH_ENABLED", "false").lower() == "true",
            qwen_asr_model=os.getenv("FITNESS_QWEN_ASR_MODEL", "qwen3-asr-flash").strip(),
            ai_user_daily_limit=max(0, int(os.getenv("FITNESS_AI_USER_DAILY_LIMIT", "100"))),
            ai_global_daily_limit=max(0, int(os.getenv("FITNESS_AI_GLOBAL_DAILY_LIMIT", "100"))),
            qwen_base_url=os.getenv(
                "FITNESS_QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
            ).strip().rstrip("/"),
        )
