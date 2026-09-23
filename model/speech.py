import base64
import io
import wave
from dataclasses import replace

from fastapi import HTTPException

from model.factory import ModelError
from model.qwen import QwenTextModel

MAX_AUDIO_BYTES = 1_000_000
MAX_AUDIO_SECONDS = 30


def validate_audio(data):
    if not data or len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "录音为空或超过大小限制")
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            frames = audio.getnframes()
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, "NONE"):
                raise ValueError
            if not 0 < frames <= MAX_AUDIO_SECONDS * 16000:
                raise ValueError
            if len(audio.readframes(frames)) != frames * 2:
                raise ValueError
    except (wave.Error, EOFError, ValueError):
        raise HTTPException(422, "请重新录制30秒以内的有效语音") from None


class QwenSpeechModel:
    def __init__(self, settings, *, transport=None):
        if not settings.speech_enabled or not settings.qwen_api_key:
            raise ModelError("SPEECH_NOT_CONFIGURED", "语音转写尚未配置，仍可手动输入", 503)
        self.client = QwenTextModel(replace(settings, qwen_model=settings.qwen_asr_model), transport=transport)

    def transcribe(self, data):
        validate_audio(data)
        audio = "data:audio/wav;base64," + base64.b64encode(data).decode("ascii")
        return self.client.request({
            "model": self.client.settings.qwen_model,
            "messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": audio}}]}],
            "stream": False, "asr_options": {"enable_itn": False},
        }, output_limit=2000).strip()


def speech_status(settings):
    try:
        QwenSpeechModel(settings)
    except ModelError as error:
        return "not_configured" if error.code == "SPEECH_NOT_CONFIGURED" else "configuration_error"
    return "configured_unverified"
