"""Bounded local image decoding and one authenticated Qwen vision request."""
import base64
import io
import warnings
from dataclasses import replace

from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError

from model.factory import ModelError, create_meal_text_model

MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_PHOTO_PIXELS = 24_000_000
PHOTO_TYPES = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}


def normalize_photo(data, content_type):
    if content_type not in PHOTO_TYPES:
        raise HTTPException(415, "请选择 JPEG、PNG 或 WebP 照片；HEIC 请先转为 JPEG")
    if not data or len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(413, "照片为空或超过10MB，请换一张较小的照片")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as original:
                if original.format != PHOTO_TYPES[content_type] or getattr(original, "n_frames", 1) != 1:
                    raise ValueError
                width, height = original.size
                if min(width, height) < 16 or max(width, height) > 12000 or width * height > MAX_PHOTO_PIXELS:
                    raise ValueError
                original.verify()
            with Image.open(io.BytesIO(data)) as original:
                rotated = ImageOps.exif_transpose(original)
                rotated.thumbnail((1600, 1600))
                rgba = rotated.convert("RGBA")
                clean = Image.new("RGB", rgba.size, "white")
                clean.paste(rgba, mask=rgba.getchannel("A"))
                output = io.BytesIO()
                clean.save(output, format="JPEG", quality=85)
                return output.getvalue()
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise HTTPException(422, "照片无法解码、格式不符或像素过大；请换一张清晰的静态食物照片") from None


class QwenVisionModel:
    def __init__(self, settings, *, transport=None):
        if not settings.photo_enabled:
            raise ModelError("MODEL_NOT_CONFIGURED", "照片识别尚未启用，仍可手动或文字记录。", 503)
        self.model = create_meal_text_model(replace(settings, qwen_model=settings.qwen_vision_model))
        if transport is not None:
            self.model.transport = transport

    def generate(self, *, system_prompt, message, image):
        return self.model.request({
            "model": self.model.settings.qwen_model,
            "messages": [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": [
                             {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")}},
                             {"type": "text", "text": message}]}],
            "response_format": {"type": "json_object"}, "enable_thinking": False,
            "stream": False, "max_tokens": 2048, "temperature": 0,
        })


def photo_status(settings):
    try:
        QwenVisionModel(settings)
    except ModelError as error:
        return "not_configured" if error.code == "MODEL_NOT_CONFIGURED" else "configuration_error"
    return "configured_unverified"
