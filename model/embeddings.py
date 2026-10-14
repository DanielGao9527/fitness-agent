"""DashScope document/query embeddings. No automatic retries or query logging."""
import hashlib
import json
import math
import threading
from urllib.parse import urlsplit

import httpx

from model.factory import ModelError
from model.deadlines import bounded_timeout, check_deadline


class EmbeddingError(ModelError):
    pass


class QwenEmbeddingProvider:
    def __init__(self, settings, *, transport=None, max_calls=None, max_input_chars=None):
        self.settings = settings
        self.dimensions = settings.embedding_dimensions
        self.base_url = settings.embedding_base_url or settings.qwen_base_url.removesuffix(
            "/compatible-mode/v1") + "/api/v1"
        self.base_url = self.base_url.rstrip("/")
        self.transport = transport
        self.max_calls, self.max_input_chars = max_calls, max_input_chars
        self.calls = self.input_tokens = self.input_chars = 0
        self._lock = threading.Lock()
        self.identity = hashlib.sha256(json.dumps({
            "provider": "dashscope-native-v1", "endpoint": self.base_url,
            "model": settings.embedding_model, "dimensions": self.dimensions,
            "roles": "query/document", "output": "dense", "instruction": None,
        }, sort_keys=True).encode()).hexdigest()

    def _validate(self):
        try:
            url = urlsplit(self.base_url)
            allowed = url.hostname in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com",
                                       "dashscope-us.aliyuncs.com"} or (url.hostname or "").endswith(".maas.aliyuncs.com")
            valid = (url.scheme == "https" and allowed and url.netloc == url.hostname
                     and not url.query and not url.fragment and url.path == "/api/v1")
        except ValueError:
            valid = False
        settings = self.settings
        if (not valid or not settings.embedding_model or self.dimensions not in
                {64, 128, 256, 512, 768, 1024, 1536, 2048, 2560}
                or not 1 <= settings.embedding_batch_size <= 10
                or not math.isfinite(settings.embedding_timeout_seconds)
                or not 1 <= settings.embedding_timeout_seconds <= 60):
            raise EmbeddingError("EMBEDDING_CONFIGURATION_ERROR", "向量接口配置无效，请核对官方地址、型号和参数。", 503)
        if not settings.qwen_api_key:
            raise EmbeddingError("EMBEDDING_NOT_CONFIGURED", "向量模型未配置服务端密钥。", 503)

    def embed(self, texts, *, text_type, before_call=None):
        self._validate()
        if (text_type not in {"document", "query"} or not isinstance(texts, list)
                or any(not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 24000
                       for text in texts)):
            raise EmbeddingError("EMBEDDING_INVALID_INPUT", "向量输入为空、过长或类型不正确；未截断文本。", 422)
        if not texts:
            return []
        result = []
        size = self.settings.embedding_batch_size
        for start in range(0, len(texts), size):
            batch = texts[start:start + size]
            chars = sum(map(len, batch))
            with self._lock:
                check_deadline()
                if ((self.max_calls is not None and self.calls >= self.max_calls)
                        or (self.max_input_chars is not None and self.input_chars + chars > self.max_input_chars)):
                    raise EmbeddingError("EMBEDDING_BUDGET_EXCEEDED", "向量调用已达到本次运行预算，未继续请求。", 429)
                if before_call is not None:
                    before_call()  # Authenticated request quota; do not swallow its exception.
                self.calls += 1
                self.input_chars += chars
            payload = {"model": self.settings.embedding_model, "input": {"texts": batch},
                       "parameters": {"text_type": text_type, "dimension": self.dimensions,
                                      "output_type": "dense"}}
            try:
                timeout = bounded_timeout(self.settings.embedding_timeout_seconds)
                with httpx.Client(timeout=httpx.Timeout(timeout, connect=min(5, timeout)),
                                  transport=self.transport, follow_redirects=False) as client:
                    with client.stream("POST", self.base_url + "/services/embeddings/text-embedding/text-embedding",
                                       headers={"Authorization": "Bearer " + self.settings.qwen_api_key},
                                       json=payload) as response:
                        if response.status_code != 200:
                            category = ("AUTH" if response.status_code in (401, 403) else
                                        "RATE_LIMIT" if response.status_code == 429 else "UPSTREAM")
                            raise EmbeddingError("EMBEDDING_" + category,
                                                 "向量服务未返回有效结果，请核对权限、地域、额度或稍后重试。", 503)
                        raw = bytearray()
                        for chunk in response.iter_bytes():
                            check_deadline()
                            if len(raw) + len(chunk) > 2 * 1024 * 1024:
                                raise EmbeddingError("EMBEDDING_INVALID_OUTPUT", "向量响应超出大小限制。")
                            raw.extend(chunk)
                check_deadline()
                body = json.loads(raw)
                rows = body["output"]["embeddings"]
                if not isinstance(rows, list) or len(rows) != len(batch):
                    raise ValueError
                ordered = {}
                for row in rows:
                    index, vector = row["text_index"], row["embedding"]
                    if (type(index) is not int or not 0 <= index < len(batch) or index in ordered
                            or not isinstance(vector, list) or len(vector) != self.dimensions
                            or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector)
                            or not any(vector)):
                        raise ValueError
                    ordered[index] = [float(value) for value in vector]
                usage = body.get("usage", {})
                if not isinstance(usage, dict):
                    raise ValueError
                tokens = usage.get("total_tokens")
                if type(tokens) is int and tokens >= 0:
                    with self._lock:
                        self.input_tokens += tokens
                result.extend(ordered[index] for index in range(len(batch)))
            except httpx.TimeoutException:
                raise EmbeddingError("EMBEDDING_TIMEOUT", "向量请求超时，未自动重试。", 504) from None
            except httpx.RequestError:
                raise EmbeddingError("EMBEDDING_UNAVAILABLE", "向量服务连接失败，未自动重试。", 503) from None
            except (ValueError, KeyError, TypeError, OverflowError):
                raise EmbeddingError("EMBEDDING_INVALID_OUTPUT", "向量响应的顺序、维度或数值未通过校验。") from None
        return result
