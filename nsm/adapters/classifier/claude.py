"""Claude 분류기: 구조화 출력(JSON 스키마)으로 라벨 + 신뢰도를 받는다 (ADR 0003)."""

from __future__ import annotations

import json
from typing import Any

from nsm.adapters.pricing import claude_cost
from nsm.domain.models import Classification
from nsm.ports import AdapterError, CostSink

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
        if response.stop_reason == "refusal":
            raise AdapterError("분류 요청이 거절됐습니다(refusal)")
        raw = next((b.text for b in response.content if b.type == "text"), None)
        if raw is None:
            raise AdapterError(f"분류 응답에 텍스트가 없습니다(stop_reason={response.stop_reason})")
        data = json.loads(raw)
        label = data["label"] if data.get("label") in labels else labels[-1]
        conf = min(1.0, max(0.0, float(data.get("confidence", 0.0))))
        return Classification(label=label, confidence=conf)

    def classify_many(
        self, texts: list[str], labels: list[str], instruction: str
    ) -> list[Classification]:
        return [self.classify(t, labels, instruction) for t in texts]
