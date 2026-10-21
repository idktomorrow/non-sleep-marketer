"""OpenAI 분류기(S16 제안, ADR 0060): OpenAI 키만 있어도 관련성·주제를 모델이 판정한다. 가짜 HTTP로만 돈다."""

from __future__ import annotations

import json

import httpx
import pytest

from nsm.adapters.classifier import make_classifiers
from nsm.adapters.classifier.openai import BATCH, OpenAIClassifier
from nsm.adapters.openai_common import OpenAIClient
from nsm.config.secrets import MemorySecretStore
from nsm.ports import AdapterError
from tests.fakes import FakeCostSink

KEY = "sk-test-secret-key-000000000000"


def _resp(items) -> httpx.Response:
    text = json.dumps({"items": items})
    return httpx.Response(
        200,
        json={
            "status": "completed",
            "model": "gpt-6.1-sol",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": 500, "output_tokens": 50},
        },
    )


def _clf(handler, sink=None) -> OpenAIClassifier:
    client = OpenAIClient(KEY, client=httpx.Client(transport=httpx.MockTransport(handler)))
    return OpenAIClassifier(client, "gpt-6.1-sol", sink)


def test_batches_and_maps_labels_with_strict_schema_and_cost() -> None:
    seen: list[dict] = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        seen.append(body)
        assert KEY not in json.dumps(body)
        n = body["input"][0]["content"][0]["text"].count("\n[")
        return _resp([{"i": i, "label": "relevant", "confidence": 0.9} for i in range(n)])

    sink = FakeCostSink()
    out = _clf(handler, sink).classify_many(
        [f"아이돌 컴백 {i}" for i in range(BATCH + 2)], ["relevant", "irrelevant"], "지시"
    )
    assert len(seen) == 2 and len(out) == BATCH + 2 and out[0].label == "relevant"
    fmt = seen[0]["text"]["format"]
    assert fmt["strict"] is True and seen[0]["store"] is False
    assert fmt["schema"]["properties"]["items"]["items"]["properties"]["label"]["enum"] == [
        "relevant",
        "irrelevant",
    ]
    assert sink.records  # 비용 기록


def test_missing_item_or_unknown_label_raises() -> None:
    with pytest.raises(AdapterError):
        _clf(lambda r: _resp([])).classify("글", ["a", "b"], "지시")
    with pytest.raises(AdapterError):
        _clf(lambda r: _resp([{"i": 0, "label": "zzz", "confidence": 1}])).classify(
            "글", ["a", "b"], "지시"
        )


def test_openai_key_alone_uses_model_classifier_before_keyword(app_config) -> None:
    """OpenAI 키만 있을 때 낱말 규칙이 아니라 OpenAI 분류기(넓은 주제 '한국 아이돌'은 낱말로 못 찾음)."""
    cs = make_classifiers(app_config, MemorySecretStore({"openai_api_key": "k"}), FakeCostSink())
    assert cs.name == "openai"
    assert [n for n, _ in cs.relevance.chain] == ["openai", "keyword"]
    allk = MemorySecretStore(
        {"gemini_api_key": "g", "anthropic_api_key": "a", "openai_api_key": "o"}
    )
    assert [n for n, _ in make_classifiers(app_config, allk, FakeCostSink()).relevance.chain] == [
        "gemini",
        "claude",
        "openai",
        "keyword",
    ]
    forced = app_config.model_copy(
        update={"settings": app_config.settings.model_copy(update={"classifier": "openai"})}
    )
    with pytest.raises(RuntimeError):
        make_classifiers(forced, MemorySecretStore(), FakeCostSink())
