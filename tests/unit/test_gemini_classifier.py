"""Gemini 분류기(C27, ADR 0048): 묶음 호출, 구조화 출력, 429 대기, 응답 검사. 가짜 HTTP로만 돈다."""

from __future__ import annotations

import json

import httpx
import pytest

from nsm.adapters.classifier.gemini import BATCH, GeminiClassifier
from nsm.ports import AdapterError


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok(items):
    text = json.dumps({"items": items})
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})


def test_batches_texts_and_maps_labels_back() -> None:
    seen: list[dict] = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        seen.append(body)
        assert req.headers["x-goog-api-key"] == "KEY" and "KEY" not in str(req.url)
        n = body["contents"][0]["parts"][0]["text"].count("\n[")
        return _ok(
            [
                {"i": i, "label": "relevant" if i % 2 == 0 else "irrelevant", "confidence": 0.9}
                for i in range(n)
            ]
        )

    c = GeminiClassifier("KEY", "gemini-3.5-flash-lite", client=_client(handler))
    texts = [f"글 {i}" for i in range(BATCH + 3)]
    out = c.classify_many(texts, ["relevant", "irrelevant"], "지시")
    assert len(seen) == 2 and len(out) == BATCH + 3  # 10건 + 3건, 두 요청
    assert [o.label for o in out[:3]] == ["relevant", "irrelevant", "relevant"]
    gen = seen[0]["generationConfig"]
    assert gen["responseMimeType"] == "application/json"
    assert gen["responseJsonSchema"]["properties"]["items"]["items"]["properties"]["label"][
        "enum"
    ] == ["relevant", "irrelevant"]
    assert "지시" in seen[0]["contents"][0]["parts"][0]["text"]


def test_rate_limit_waits_then_succeeds() -> None:
    calls = {"n": 0}
    waits: list[int] = []

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="quota")
        return _ok([{"i": 0, "label": "irrelevant", "confidence": 0.8}])

    c = GeminiClassifier("K", "m", client=_client(handler), sleep=waits.append)
    assert c.classify("x", ["relevant", "irrelevant"], "q").label == "irrelevant"
    assert calls["n"] == 2 and waits == [20]


def test_persistent_rate_limit_and_bad_responses_raise() -> None:
    c = GeminiClassifier(
        "K", "m", client=_client(lambda r: httpx.Response(429, text="quota")), sleep=lambda s: None
    )
    with pytest.raises(AdapterError, match="429"):
        c.classify("x", ["a", "b"], "q")
    c = GeminiClassifier(
        "K", "m", client=_client(lambda r: httpx.Response(200, json={"candidates": []}))
    )
    with pytest.raises(AdapterError):
        c.classify("x", ["a", "b"], "q")
    # 빠진 번호 → 오류, 모르는 라벨 → 마지막 라벨
    c = GeminiClassifier(
        "K", "m", client=_client(lambda r: _ok([{"i": 5, "label": "a", "confidence": 1}]))
    )
    with pytest.raises(AdapterError, match="0번"):
        c.classify("x", ["a", "b"], "q")
    c = GeminiClassifier(
        "K", "m", client=_client(lambda r: _ok([{"i": 0, "label": "zzz", "confidence": 2}]))
    )
    r = c.classify("x", ["a", "b"], "q")
    assert r.label == "b" and r.confidence == 1.0
