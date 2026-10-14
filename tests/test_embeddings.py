import json
from dataclasses import replace

import httpx
import pytest

from config import Settings
from model.embeddings import EmbeddingError, QwenEmbeddingProvider


def settings(tmp_path, **kwargs):
    return Settings(database_path=tmp_path / "private.sqlite3", qwen_api_key="test-only", embedding_dimensions=64, **kwargs)


def response(count, dimensions=64):
    return {"output": {"embeddings": [{"text_index": i, "embedding": [float(i + 1)] * dimensions}
                                      for i in reversed(range(count))]}, "usage": {"total_tokens": count * 3}}


def test_roles_order_batches_and_usage(tmp_path):
    payloads, reserved = [], []
    def handle(request):
        assert request.url.path.endswith("/embeddings/text-embedding/text-embedding")
        payload = json.loads(request.content)
        payloads.append(payload)
        return httpx.Response(200, json=response(len(payload["input"]["texts"])))
    provider = QwenEmbeddingProvider(settings(tmp_path, embedding_batch_size=2), transport=httpx.MockTransport(handle))
    vectors = provider.embed(["甲", "乙", "丙"], text_type="document", before_call=lambda: reserved.append(True))
    assert [v[0] for v in vectors] == [1, 2, 1]
    assert all(p["parameters"] == {"text_type": "document", "dimension": 64, "output_type": "dense"} for p in payloads)
    assert provider.calls == len(reserved) == 2 and provider.input_tokens == 9
    provider.embed(["问题"], text_type="query")
    assert payloads[-1]["parameters"]["text_type"] == "query"


@pytest.mark.parametrize("url", ["http://dashscope.aliyuncs.com/api/v1", "https://evil.example/api/v1",
    "https://dashscope.aliyuncs.com.evil.example/api/v1", "https://x@dashscope.aliyuncs.com/api/v1",
    "https://dashscope.aliyuncs.com/api/v1?secret=x", "https://dashscope.aliyuncs.com:443/api/v1"])
def test_invalid_endpoint_never_calls_or_reserves(tmp_path, url):
    provider = QwenEmbeddingProvider(settings(tmp_path, embedding_base_url=url))
    with pytest.raises(EmbeddingError, match="配置"):
        provider.embed(["test"], text_type="query", before_call=lambda: pytest.fail("no quota"))
    assert provider.calls == 0


@pytest.mark.parametrize("mutate", [
    lambda b: b["output"]["embeddings"].append(b["output"]["embeddings"][0]),
    lambda b: b["output"]["embeddings"][0].update(text_index=4),
    lambda b: b["output"]["embeddings"][0].update(embedding=[1]),
    lambda b: b["output"]["embeddings"][0].update(embedding=[float("nan")] * 64),
    lambda b: b["output"]["embeddings"][0].update(embedding=[False] * 64),
    lambda b: b["output"]["embeddings"][0].update(embedding=[0] * 64),
    lambda b: b.update(usage=None),
    lambda b: b.update(usage=[]),
])
def test_bad_response_rejected(tmp_path, mutate):
    body = response(1)
    mutate(body)
    provider = QwenEmbeddingProvider(settings(tmp_path), transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=json.dumps(body).encode())))
    with pytest.raises(EmbeddingError) as error:
        provider.embed(["test"], text_type="query")
    assert error.value.code == "EMBEDDING_INVALID_OUTPUT"


def test_budget_and_quota_are_before_network(tmp_path):
    provider = QwenEmbeddingProvider(settings(tmp_path), max_calls=0)
    with pytest.raises(EmbeddingError) as error:
        provider.embed(["test"], text_type="query")
    assert error.value.code == "EMBEDDING_BUDGET_EXCEEDED" and provider.calls == 0
    provider = QwenEmbeddingProvider(settings(tmp_path))
    def denied():
        raise RuntimeError("quota denied")
    with pytest.raises(RuntimeError, match="quota denied"):
        provider.embed(["test"], text_type="query", before_call=denied)
    assert provider.calls == 0


def test_no_retry_and_no_private_error_body(tmp_path, caplog):
    provider = QwenEmbeddingProvider(settings(tmp_path), transport=httpx.MockTransport(
        lambda _: httpx.Response(429, text="PRIVATE-INPUT test-only")))
    with pytest.raises(EmbeddingError) as error:
        provider.embed(["PRIVATE-INPUT"], text_type="query")
    assert provider.calls == 1
    assert "PRIVATE-INPUT" not in str(error.value) + caplog.text
    assert "test-only" not in str(error.value) + caplog.text


def test_configuration_identity_and_missing_key(tmp_path):
    a = settings(tmp_path)
    assert QwenEmbeddingProvider(a).identity != QwenEmbeddingProvider(replace(a, embedding_model="other")).identity
    assert QwenEmbeddingProvider(a).identity != QwenEmbeddingProvider(replace(a, embedding_dimensions=128)).identity
    assert QwenEmbeddingProvider(a).identity == QwenEmbeddingProvider(replace(a, qwen_api_key="another-secret")).identity
    with pytest.raises(EmbeddingError) as error:
        QwenEmbeddingProvider(replace(a, qwen_api_key="")).embed(["test"], text_type="query")
    assert error.value.code == "EMBEDDING_NOT_CONFIGURED"
