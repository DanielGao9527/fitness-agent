from urllib.parse import urlsplit
import logging
from uuid import uuid4

import httpx

from config import Settings
from model.factory import ModelError

logger = logging.getLogger(__name__)


def provider_error(response):
    # Only fixed categories and a locally generated incident ID enter logs/UI.
    known = {
        "InvalidParameter": "parameter", "invalid_parameter_error": "parameter",
        "InternalError.Algo.InvalidParameter": "parameter", "invalid_request_error": "parameter",
        "model_not_found": "model", "ModelNotFound": "model",
        "DataInspectionFailed": "content", "data_inspection_failed": "content",
        "Arrearage": "billing", "AllocationQuota.FreeTierOnly": "billing",
    }
    category = "unknown"
    try:
        body = response.json()
        error = body.get("error", body) if isinstance(body, dict) else {}
        code = error.get("code") if isinstance(error, dict) else None
        category = known.get(code, "unknown") if isinstance(code, str) else "unknown"
    except ValueError:
        pass
    status = response.status_code
    incident = uuid4().hex[:12]
    logger.warning("qwen_failure incident=%s http=%s category=%s", incident, status, category)
    if status in (401, 403):
        code, message, public_status = "MODEL_AUTH_ERROR", "千问鉴权失败，请检查密钥、地域和模型权限。", 503
    elif status == 429:
        code, message, public_status = "MODEL_RATE_LIMITED", "千问调用受限，请检查额度或稍后重试。", 429
    elif status >= 500 or status in (408, 504):
        code, message, public_status = "MODEL_UPSTREAM_ERROR", "千问服务暂时异常，请稍后重试；本次未获得估算结果，输入已保留。", 502
    elif category == "billing" or status == 402:
        code, message, public_status = "MODEL_BILLING_ERROR", "千问账户额度或计费状态限制了调用，请检查百炼账户。", 503
    elif category == "content":
        code, message, public_status = "MODEL_CONTENT_REJECTED", "千问未接受本次内容，请核对描述；输入已保留。", 502
    elif category == "model" or status == 404:
        code, message, public_status = "MODEL_CONFIGURATION_ERROR", "千问型号或接口不可用，请检查型号、地域和访问权限。", 503
    elif status in (400, 413, 422) or category == "parameter":
        code, message, public_status = "MODEL_UPSTREAM_ERROR", "千问未接受本次请求参数或内容格式；输入已保留。持续出现时请凭故障编号排查接口。", 502
    else:
        code, message, public_status = "MODEL_UPSTREAM_ERROR", "千问返回异常响应，输入已保留；请稍后重试，持续出现时凭故障编号排查。", 502
    return ModelError(code, f"{message}（故障编号 {incident}，HTTP {status}）", public_status)


class QwenTextModel:
    """One bounded, non-streaming JSON request; never retries automatically."""

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None):
        try:
            url = urlsplit(settings.qwen_base_url)
        except ValueError:
            raise ModelError("MODEL_CONFIGURATION_ERROR", "千问接口地址格式不正确。", 503) from None
        allowed_host = url.hostname in {
            "dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com"
        } or (url.hostname or "").endswith(".maas.aliyuncs.com")
        if (
            url.scheme != "https" or not allowed_host or url.username or url.password
            or url.netloc != url.hostname or url.query or url.fragment
            or url.path.rstrip("/") != "/compatible-mode/v1" or not settings.qwen_model
        ):
            raise ModelError("MODEL_CONFIGURATION_ERROR", "请检查千问型号与百炼官方 HTTPS 接口地址。", 503)
        self.settings = settings
        self.transport = transport

    def generate(self, *, system_prompt: str, message: str) -> str:
        return self.request({
            "model": self.settings.qwen_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message},
            ],
            "response_format": {"type": "json_object"},
            "enable_thinking": False, "stream": False, "max_tokens": 2048, "temperature": 0,
        })

    def request(self, payload, *, output_limit=24000):
        try:
            with httpx.Client(
                timeout=httpx.Timeout(20.0, connect=5.0), transport=self.transport, follow_redirects=False
            ) as client:
                response = client.post(
                    self.settings.qwen_base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {self.settings.qwen_api_key}"},
                    json=payload,
                )
        except httpx.TimeoutException:
            raise ModelError("MODEL_TIMEOUT", "千问响应超时，未保存记录；可稍后重试。", 504) from None
        except httpx.RequestError:
            raise ModelError("MODEL_UNAVAILABLE", "暂时无法连接千问，未保存记录。") from None

        # Provider bodies may contain private input or account details; never forward them.
        if response.status_code != 200:
            raise provider_error(response)
        try:
            choice = response.json()["choices"][0]
            content = choice["message"]["content"]
            if choice.get("finish_reason") != "stop" or not isinstance(content, str) or not content.strip():
                raise ValueError
            if len(content) > output_limit:
                raise ValueError
            return content
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ModelError("MODEL_INVALID_OUTPUT", "千问返回内容不完整或格式错误，未保存记录。") from None
