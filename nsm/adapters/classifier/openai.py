"""OpenAI 분류기(S16 제안): OpenAI 키 하나만 있을 때도 관련성·주제를 모델이 판정한다.

- 낱말 규칙 분류기는 "한국 아이돌"처럼 넓은 주제를 글에서 글자 그대로 찾지 못해 실제 검증에서 관련 자료가 0건이었다.
- Responses API + strict JSON 스키마, 여러 건을 한 요청에 묶는다(`BATCH`). 호출·비용 원장·오류 분류는
  OpenAI 텍스트 생성기와 같은 코드(`_body`·`_call`·`_text`)를 쓴다.
- 응답이 깨졌거나(번호 누락·허용되지 않은 라벨) 호출이 실패하면 예외 → FallbackClassifier가 다음 분류기로 넘어간다.
"""

from __future__ import annotations

from typing import Any

from nsm.adapters.generator.openai_text import OpenAITextGenerator
from nsm.adapters.openai_common import OpenAIClient
from nsm.domain.models import Classification
from nsm.ports import AdapterError, CostSink

BATCH = 10
TEXT_LIMIT = 1500
MAX_TOKENS = 4000
ESTIMATE_USD = 0.01
SYSTEM = (
    "You are a precise text classifier for a Korean social media marketing tool. "
    "For each item pick exactly one label from the allowed list and give a calibrated "
    "confidence (0.0-1.0) that the label is correct."
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
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }


class OpenAIClassifier:
    name = "openai"

    def __init__(
        self,
        client: OpenAIClient,
        model: str,
        cost_sink: CostSink | None = None,
        effort: str = "low",
    ) -> None:
        self._gen = OpenAITextGenerator(client, model, cost_sink, effort=effort)
        self.model = model

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
        user = (
            f"{instruction}\n\nAllowed labels: {', '.join(labels)}\n\n"
            "아래 자료 각각을 판정하라. i는 자료 번호다.\n\n"
            f"{listing}"
        )
        g = self._gen
        body = g._body(SYSTEM, user, _schema(labels), "classification", MAX_TOKENS)
        data = g._parse(g._text(g._call("classify", body, ESTIMATE_USD)))
        try:
            items = {int(it["i"]): it for it in data["items"]}
        except (KeyError, TypeError, ValueError) as e:
            raise AdapterError(f"OpenAI 분류 응답을 읽을 수 없음: {type(e).__name__}") from e
        result = []
        for i in range(len(texts)):
            it = items.get(i)
            if it is None:
                raise AdapterError(f"OpenAI 분류 응답에 {i}번 자료가 없음")
            if it.get("label") not in labels:
                raise AdapterError(f"OpenAI가 허용되지 않은 라벨을 냄: {str(it.get('label'))[:40]}")
            conf = min(1.0, max(0.0, float(it.get("confidence", 0.0))))
            result.append(Classification(label=it["label"], confidence=conf))
        return result
