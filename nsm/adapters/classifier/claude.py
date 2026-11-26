"""Claude 분류기: 구조화 출력(JSON 스키마)으로 라벨 + 신뢰도를 받는다 (ADR 0003)."""

from __future__ import annotations

import json
import logging
from typing import Any

from nsm.adapters.pricing import claude_cost
from nsm.domain.models import Classification
from nsm.ports import AdapterError, CostSink

log = logging.getLogger(__name__)

BATCH = 10  # 한 요청에 묶는 건수(gemini.BATCH와 같은 값, ADR 0079)
TEXT_LIMIT = 1500  # 묶음 호출에서 한 건에 보내는 글자 수. 단건 classify는 6000자 그대로

SYSTEM = (
    "You are a precise text classifier for a Korean social media marketing tool. "
    "Pick exactly one label from the allowed list. "
    "confidence is your calibrated probability (0.0-1.0) that the chosen label is correct."
)


class ClaudeClassifier:
    name = "claude"

    def __init__(self, client: Any, model: str, cost_sink: CostSink | None = None) -> None:
        self.client = client
        self.model = model
        self.cost_sink = cost_sink

    def classify(self, text: str, labels: list[str], instruction: str) -> Classification:
        schema = {
            "type": "object",
            "properties": {
                "label": {"type": "string", "enum": labels},
                "confidence": {"type": "number"},
            },
            "required": ["label", "confidence"],
            "additionalProperties": False,
        }
        response = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=SYSTEM,
            thinking={"type": "disabled"},
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            messages=[
                {
                    "role": "user",
                    "content": f"{instruction}\n\nAllowed labels: {', '.join(labels)}\n\n"
                    f"<text>\n{text[:6000]}\n</text>",
                }
            ],
        )
        usage = getattr(response, "usage", None)
        if self.cost_sink and usage is not None:
            self.cost_sink.record_cost(
                "classifier",
                "anthropic",
                claude_cost(self.model, usage.input_tokens, usage.output_tokens),
                "classify",
            )
        if response.stop_reason in ("refusal", "max_tokens"):
            raise AdapterError(f"분류 응답을 쓸 수 없습니다(stop_reason={response.stop_reason})")
        raw = next((b.text for b in response.content if b.type == "text"), None)
        if raw is None:
            raise AdapterError(f"분류 응답에 텍스트가 없습니다(stop_reason={response.stop_reason})")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise AdapterError(f"분류 응답 JSON 파싱 실패: {e}") from e
        label = data["label"] if data.get("label") in labels else labels[-1]
        conf = min(1.0, max(0.0, float(data.get("confidence", 0.0))))
        return Classification(label=label, confidence=conf)

    def classify_many(
        self, texts: list[str], labels: list[str], instruction: str
    ) -> list[Classification]:
        if len(texts) <= 1:
            return [self.classify(t, labels, instruction) for t in texts]
        out: list[Classification] = []
        for start in range(0, len(texts), BATCH):
            out.extend(self._batch(texts[start : start + BATCH], labels, instruction))
        return out

    def _batch(self, texts: list[str], labels: list[str], instruction: str) -> list[Classification]:
        """10건까지 한 요청으로. 응답 모양이 깨지면 예외 대신 그 묶음만 단건으로 다시(ADR 0079).

        FallbackClassifier는 예외를 받으면 30분 동안 세 분류기를 다음 것(낱말 규칙)으로 넘기므로,
        묶음 형식이 한 번 깨진 것 때문에 그렇게 되지 않게 한다. API 장애·refusal은 그대로 올린다.
        """
        listing = "\n\n".join(f"[{i}]\n{t[:TEXT_LIMIT]}" for i, t in enumerate(texts))
        response = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=SYSTEM,
            thinking={"type": "disabled"},
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": _batch_schema(labels)},
            },
            messages=[
                {
                    "role": "user",
                    "content": f"{instruction}\n\nAllowed labels: {', '.join(labels)}\n\n"
                    "아래 자료 각각을 판정하라. i는 자료 번호다.\n\n"
                    f"<texts>\n{listing}\n</texts>",
                }
            ],
        )
        usage = getattr(response, "usage", None)
        if self.cost_sink and usage is not None:
            self.cost_sink.record_cost(
                "classifier",
                "anthropic",
                claude_cost(self.model, usage.input_tokens, usage.output_tokens),
                "classify_many",
            )
        if response.stop_reason in ("refusal", "max_tokens"):
            raise AdapterError(f"분류 응답을 쓸 수 없습니다(stop_reason={response.stop_reason})")
        try:
            return _parse_batch(response, len(texts), labels)
        except (KeyError, IndexError, TypeError, ValueError) as e:
            log.warning(
                "Claude 묶음 분류 응답이 깨짐(%s), %d건을 한 건씩 다시", _short(e), len(texts)
            )
            return [self.classify(t, labels, instruction) for t in texts]


def _short(e: Exception) -> str:
    return f"{type(e).__name__}: {str(e)[:80]}"


def _batch_schema(labels: list[str]) -> dict[str, Any]:
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


def _parse_batch(response: Any, n: int, labels: list[str]) -> list[Classification]:
    """번호 0..n-1이 전부 있고 라벨이 허용 목록 안일 때만 순서대로. 아니면 ValueError 계열."""
    raw = next((b.text for b in response.content if b.type == "text"), None)
    if raw is None:
        raise ValueError("텍스트 블록 없음")
    listed = json.loads(raw)["items"]  # JSONDecodeError는 ValueError
    idx = [it["i"] for it in listed]
    # 번호는 정수(bool 제외)이고 0..n-1이 한 번씩만. 겹치거나 남으면 어느 글의 판정인지 믿을 수 없다
    if any(type(i) is not int for i in idx) or sorted(idx) != list(range(n)):
        raise ValueError(f"번호가 0..{n - 1}과 맞지 않음")
    items = {it["i"]: it for it in listed}
    result = []
    for i in range(n):
        it = items.get(i)
        if it is None:
            raise ValueError(f"{i}번 자료 없음")
        if it.get("label") not in labels:
            raise ValueError(f"허용되지 않은 라벨 {str(it.get('label'))[:40]}")
        conf = min(1.0, max(0.0, float(it.get("confidence", 0.0))))
        result.append(Classification(label=it["label"], confidence=conf))
    return result
