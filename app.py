import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager, suppress
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from api.routes import router
from api.workouts import router as workout_router
from api.knowledge import router as knowledge_router
from api.meal_plans import router as meal_plan_router
from api.training_plans import router as training_plan_router
from api.coach import router as coach_router
from api.meal_photos import router as photo_router
from api.body_measurements import router as body_router
from model.vision import PHOTO_TYPES
from config import ROOT, Settings
from database import Database
from rag.rag_service import LocalKnowledgeRetriever
from services.access_guard import AccessGuard
from services.guests import cleanup as cleanup_guests
from starlette.concurrency import run_in_threadpool


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate_access()
    if settings.database_path.resolve().is_relative_to((ROOT / "static").resolve()):
        raise ValueError("Personal database must not be inside public static files")
    database = Database(settings.database_path)
    access_guard = AccessGuard(settings.database_path)

    @asynccontextmanager
    async def lifespan(application):
        database.initialize()
        access_guard.initialize()
        cleanup_guests(database)

        async def sweep_guests():
            while True:
                await asyncio.sleep(60)
                try:
                    await run_in_threadpool(cleanup_guests, database)
                except sqlite3.Error:
                    logging.getLogger(__name__).warning("Guest cleanup deferred; database temporarily unavailable")

        task = asyncio.create_task(sweep_guests())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    local_debug = settings.access_mode == "local"
    application = FastAPI(
        title="Fitness Agent", lifespan=lifespan,
        docs_url="/docs" if local_debug else None,
        redoc_url="/redoc" if local_debug else None,
        openapi_url="/openapi.json" if local_debug else None,
    )
    application.state.settings = settings
    application.state.database = database
    application.state.access_guard = access_guard
    knowledge_path = settings.knowledge_index_path or settings.database_path.with_name("knowledge.sqlite3")
    if knowledge_path.resolve() == settings.database_path.resolve():
        raise ValueError("Knowledge index must not use the personal database path")
    application.state.knowledge = LocalKnowledgeRetriever(settings.knowledge_source_path.resolve(), knowledge_path.resolve())

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        # Omit submitted values: validation responses must not echo passwords.
        fields = [".".join(map(str, item["loc"][1:])) for item in error.errors()]
        return JSONResponse(status_code=422, content={"detail": {"code": "INVALID_INPUT", "message": "请检查输入的格式、范围与必填项", "fields": fields}})

    @application.middleware("http")
    async def request_boundary(request: Request, call_next):
        def origin_key(value):
            try:
                parsed = urlsplit(value)
                if parsed.username or parsed.password or not parsed.hostname or parsed.scheme not in ("http", "https"):
                    return None
                return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
            except ValueError:
                return None
        if request.url.path.startswith("/api/") and request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            expected = settings.public_origin if settings.access_mode == "shared" else str(request.base_url)
            if request.headers.get("sec-fetch-site") == "cross-site" or (origin and (origin_key(origin) is None or origin_key(origin) != origin_key(expected))):
                return JSONResponse(status_code=403, content={"detail": "请求来源不匹配"})
            speech_upload = request.url.path == "/api/speech/transcribe" and request.method == "POST"
            photo_upload = request.url.path.startswith("/api/meal-drafts/") and request.url.path.endswith("/photo") and request.method == "POST"
            required_type = "audio/wav" if speech_upload else "application/json"
            actual_type = request.headers.get("content-type", "").split(";")[0]
            if photo_upload and actual_type not in PHOTO_TYPES:
                return JSONResponse(status_code=415, content={"detail": "照片接口仅接受 JPEG、PNG 或 WebP"})
            if not photo_upload and request.method in ("POST", "PUT", "PATCH") and actual_type != required_type:
                if speech_upload:
                    return JSONResponse(status_code=415, content={"detail": "语音接口仅接受 WAV 录音"})
                return JSONResponse(status_code=415, content={"detail": "此接口仅接受 JSON 数据"})
            if not speech_upload and not photo_upload:
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > 64 * 1024:
                        return JSONResponse(status_code=413, content={"detail": "请求内容过大"})
                    body.extend(chunk)
                # Starlette's cached middleware request replays this body to the
                # endpoint. Only the bounded, fully checked bytes are retained.
                request._body = bytes(body)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    application.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts), www_redirect=False)

    application.include_router(router)
    application.include_router(workout_router)
    application.include_router(knowledge_router)
    application.include_router(meal_plan_router)
    application.include_router(training_plan_router)
    application.include_router(coach_router)
    application.include_router(photo_router)
    application.include_router(body_router)
    application.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @application.get("/", include_in_schema=False)
    def index():
        return FileResponse(ROOT / "static" / "index.html")

    return application


app = create_app()
