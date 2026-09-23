from uuid import UUID
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from model.factory import ModelError
from model.vision import MAX_PHOTO_BYTES, QwenVisionModel
from security import current_user
from services.meal_photos import MealPhotoService
from services.usage import reserve_call

router = APIRouter(prefix="/api/meal-drafts")


@router.post("/{draft_id}/photo")
async def parse_photo(draft_id: UUID, request: Request, user: Annotated[dict, Depends(current_user)],
                      client_id: UUID, version: Annotated[int, Query(ge=1)]):
    service = MealPhotoService(request.app.state.database, user["id"])
    await run_in_threadpool(service.get, draft_id)
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_PHOTO_BYTES:
            raise HTTPException(413, "照片超过10MB，请选择较小的图片")
        data.extend(chunk)
    settings = request.app.state.settings
    def factory():
        model = QwenVisionModel(settings)
        reserve_call(request.app.state.database, settings, user["id"])
        return model
    try:
        return await run_in_threadpool(service.parse_photo, draft_id, version, client_id, bytes(data),
                                      request.headers.get("content-type", "").split(";")[0], factory, settings.qwen_vision_model)
    except ModelError as error:
        raise HTTPException(error.status_code, {"code": error.code, "message": str(error)}) from None
