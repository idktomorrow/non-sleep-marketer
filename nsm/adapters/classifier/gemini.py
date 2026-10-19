"""Gemini 분류기(배치 C27, ADR 0048): 무료 구간의 Flash-Lite 모델로 라벨 + 신뢰도를 받는다.

- REST(`generateContent`)를 httpx로 직접 부른다(SDK 의존 없음). 구조화 출력(JSON 스키마)으로 라벨을 받는다.
- 무료 구간은 분당 요청 수가 작아서(약 15회) 여러 건을 한 요청에 묶는다(`BATCH`). 운영 자료 294건으로
  jev와 비교한 결과 10건 묶음·1건씩 모두 jev와 거의 같았다(ADR 0048).
- 429(한도)·5xx(과부하)·연결 오류는 기다렸다 다시 보낸다. 그래도 안 되면 AdapterError → 다음 분류기로 넘어간다.
- 키는 헤더로만 보내고 오류 문구에 남기지 않는다.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from nsm.domain.models import Classification
from nsm.ports import AdapterError, CostSink

log = logging.getLogger(__name__)

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
BATCH = 10
TEXT_LIMIT = 1500  # 한 건에서 보내는 글자 수(제목 + 본문 앞부분이면 판정에 충분)
RETRY_WAITS = (20, 40, 60)  # 429·5xx·연결 오류 때 기다리는 초(무료 구간 분당 한도에 맞춤)
SYSTEM = (
    "You are a precise text classifier for a Korean social media marketing tool. "
    "For each item pick exactly one label from the allowed list and give a calibrated "
    "confidence (0.0-1.0) that the label is correct. Answer only with JSON."
)


def _schema(labels: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "i": {"type": "integer"},
                        "label": {"type": "string", "enum": list(labels)},
                        "confidence": {"type": "number"},
                    },
                    "required": ["i", "label", "confidence"],
                },
            }
        },
        "required": ["items"],
    }


class GeminiClassifier:
    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str,
        cost_sink: CostSink | None = None,
        client: httpx.Client | None = None,
        sleep=time.sleep,
        retry_waits: tuple[int, ...] = RETRY_WAITS,
    ) -> None:
        self._key = api_key
        self.model = model
        self.cost_sink = (
            cost_sink  # 무료 구간이라 비용은 적지 않는다(0원). 유료 전환 시 여기서 기록
        )
        self.client = client or httpx.Client(timeout=90.0)
        self._sleep = sleep
        self._retry_waits = retry_waits  # doctor 점검은 ()로 둬서 기다리지 않는다

    def classify(self, text: str, labels: list[str], instruction: str) -> Classification:
        return self.classify_many([text], labels, instruction)[0]

    def classify_many(
        self, texts: list[str], labels: list[str], instruction: str
    ) -> list[Classification]:
        out: list[Classification] = []
        for start in range(0, len(texts), BATCH):
            out.extend(self._batch(texts[start : start + BATCH], labels, instruction))
        return out

    def _batch(self, texts: list[str], labels: list[str], instruction: str) -> list[Classification]:
        listing = "\n\n".join(f"[{i}]\n{t[:TEXT_LIMIT]}" for i, t in enumerate(texts))
        prompt = (
            f"{instruction}\n\nAllowed labels: {', '.join(labels)}\n\n"
            "아래 자료 각각을 판정하라. i는 자료 번호다.\n\n"
            f"{listing}"
        )
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseJsonSchema": _schema(labels),
            },
        }
        data = self._post(body)
        try:
            raw = data["candidates"][0]["content"]["parts"][0]["text"]
            items = {int(it["i"]): it for it in json.loads(raw)["items"]}
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as e:
            raise AdapterError(f"Gemini 분류 응답을 읽을 수 없음: {type(e).__name__}") from e
        result = []
        for i in range(len(texts)):
            it = items.get(i)
            if it is None:
                raise AdapterError(f"Gemini 분류 응답에 {i}번 자료가 없음")
            if it.get("label") not in labels:
                # 스키마에 enum을 줬는데도 벗어나면 응답이 깨진 것. 엉뚱한 라벨에 신뢰도를 붙여 저장하지 않는다
                raise AdapterError(f"Gemini가 허용되지 않은 라벨을 냄: {str(it.get('label'))[:40]}")
            conf = min(1.0, max(0.0, float(it.get("confidence", 0.0))))
            result.append(Classification(label=it["label"], confidence=conf))
        return result

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{BASE_URL}/models/{self.model}:generateContent"
        headers = {"x-goog-api-key": self._key, "Content-Type": "application/json"}
        last = ""
        for attempt in range(len(self._retry_waits) + 1):
            try:
                r = self.client.post(url, headers=headers, json=body)
            except httpx.HTTPError as e:
                last = f"연결 오류 {type(e).__name__}"
                transient = True
            else:
                if r.status_code == 200:
                    return r.json()
                last = f"HTTP {r.status_code}: " + " ".join(r.text[:200].split())
                transient = r.status_code == 429 or r.status_code >= 500
            if not transient or attempt >= len(self._retry_waits):
                break
            wait = self._retry_waits[attempt]
            log.warning("Gemini 잠깐 실패(%s), %d초 뒤 다시", last, wait)
            self._sleep(wait)
        raise AdapterError(f"Gemini 호출 실패({last})")
