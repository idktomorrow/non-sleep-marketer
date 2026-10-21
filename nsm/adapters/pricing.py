"""API 비용 계산용 가격표 (달러, 2026-10 공식 가격표 기준). 비용 상한·대시보드 표시에 쓴다.

가격이 바뀌면 여기만 고친다. 모르는 모델은 가장 비싼 값으로 보수적으로 잡는다.
"""

from __future__ import annotations

import re
from typing import Any

# 모델 → (입력 $/1M 토큰, 출력 $/1M 토큰)
CLAUDE_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.00, 50.00),
}
_FALLBACK = (10.00, 50.00)

# OpenAI 텍스트 모델(S13, developers.openai.com/api/docs/pricing 2026-10-21 확인). 스냅샷 이름(-2026-..)은 앞부분으로 맞춘다
OPENAI_TEXT_PRICES: dict[str, tuple[float, float]] = {
    "gpt-6-astra": (10.00, 50.00),
    "gpt-6.1-sol": (2.00, 10.00),
    "gpt-6-sol": (2.00, 10.00),
    "gpt-6-luna": (0.10, 0.50),
    "gpt-5.6-sol": (4.00, 20.00),
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.5": (5.00, 30.00),
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.2": (1.75, 14.00),
    "gpt-5.1": (1.25, 10.00),
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
}

# GPT Image 2·2.5 토큰 단가($/1M): 이미지 입력·텍스트 입력·이미지 출력이 다르다 → 종류별로 곱한다(S13 피드백 5)
IMAGE_TOKEN_RATES: dict[str, dict[str, float]] = {
    "gpt-image-2.5-flare": {"image_in": 8.00, "text_in": 5.00, "out": 30.00},
    "gpt-image-2.5-sunburst": {"image_in": 8.00, "text_in": 5.00, "out": 30.00},
    "gpt-image-2": {"image_in": 8.00, "text_in": 5.00, "out": 30.00},
    "gpt-image-1.5": {"image_in": 8.00, "text_in": 5.00, "out": 32.00},
    "gpt-image-1-mini": {"image_in": 2.50, "text_in": 2.00, "out": 8.00},
}
_IMAGE_RATE_FALLBACK = {"image_in": 8.00, "text_in": 5.00, "out": 32.00}

# 품질별 1장 추정가(세로형, 출력 토큰 low≈408·medium≈1584·high≈6240 기준 + 입력 약간). usage가 없을 때만 쓴다
IMAGE_PRICE_ESTIMATE: dict[str, float] = {"low": 0.015, "medium": 0.05, "high": 0.20}
# 이미지 1장 추정가(옛 인터페이스 호환: 중간 품질)
IMAGE_PRICE_PER_IMAGE: dict[str, float] = {
    "gpt-image-2.5-flare": 0.05,
    "gpt-image-2.5-sunburst": 0.10,
}


def claude_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = CLAUDE_PRICES.get(model, _FALLBACK)
    return (input_tokens * pin + output_tokens * pout) / 1_000_000


_SNAPSHOT = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def _base_model(model: str) -> str:
    """스냅샷 이름(gpt-6.1-sol-2026-09-08)의 날짜 꼬리만 떼어 가격표 키로 맞춘다.
    앞글자만 같은 모르는 모델(gpt-5.7, gpt-image-2-pro)은 그대로 두어 가장 비싼 값으로 잡힌다."""
    return _SNAPSHOT.sub("", model)


def openai_text_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = OPENAI_TEXT_PRICES.get(_base_model(model), _FALLBACK)
    return (input_tokens * pin + output_tokens * pout) / 1_000_000


def image_cost(model: str) -> float:
    return IMAGE_PRICE_PER_IMAGE.get(_base_model(model), 0.20)


def image_cost_from_usage(
    model: str, usage: dict[str, Any] | None, quality: str
) -> tuple[float, str]:
    """이미지 1장 비용과 상태. `usage.input_tokens_details`가 있으면 종류별 단가로 확정(confirmed),
    없으면 품질별 추정(estimated)."""
    rates = IMAGE_TOKEN_RATES.get(_base_model(model), _IMAGE_RATE_FALLBACK)
    details = (usage or {}).get("input_tokens_details") or {}
    out_tokens = (usage or {}).get("output_tokens")
    if usage and out_tokens is not None and ("image_tokens" in details or "text_tokens" in details):
        img_in = int(details.get("image_tokens", 0))
        txt_in = int(details.get("text_tokens", 0))
        usd = (
            img_in * rates["image_in"] + txt_in * rates["text_in"] + int(out_tokens) * rates["out"]
        ) / 1_000_000
        return usd, "confirmed"
    return IMAGE_PRICE_ESTIMATE.get(quality, IMAGE_PRICE_ESTIMATE["medium"]), "estimated"
