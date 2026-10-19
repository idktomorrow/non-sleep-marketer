"""API 비용 계산용 가격표 (달러, 2026-10 기준). 비용 상한·대시보드 표시에 쓴다.

가격이 바뀌면 여기만 고친다. 모르는 모델은 가장 비싼 값으로 보수적으로 잡는다.
"""

from __future__ import annotations

# 모델 → (입력 $/1M 토큰, 출력 $/1M 토큰)
CLAUDE_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.00, 50.00),
}
_FALLBACK = (10.00, 50.00)

# 이미지 1장 추정가(중간 품질, 세로형)
IMAGE_PRICE_PER_IMAGE: dict[str, float] = {
    "gpt-image-2.5-flare": 0.05,
    "gpt-image-2.5-sunburst": 0.10,
}


def claude_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = CLAUDE_PRICES.get(model, _FALLBACK)
    return (input_tokens * pin + output_tokens * pout) / 1_000_000


def image_cost(model: str) -> float:
    return IMAGE_PRICE_PER_IMAGE.get(model, 0.20)
