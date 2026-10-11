"""OpenAI 이미지 API로 카드 배경을 만든다. 글자는 절대 넣지 않는다(한글은 템플릿이 그림, ADR 0007)."""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from nsm.adapters.pricing import image_cost
from nsm.domain.clock import new_id
from nsm.ports import AdapterError, CostSink

ENDPOINT = "https://api.openai.com/v1/images/generations"
PROMPT_PREFIX = (
    "Background artwork for a vertical social media card. Soft, clean composition with "
    "generous empty space for text overlay. Absolutely no text, letters, numbers, logos, "
    "watermarks, or human faces. Theme: "
)


def _snap16(n: int) -> int:
    return max(16, (n // 16) * 16)


class OpenAIImageGenerator:
    def __init__(
        self,
        api_key: str,
        model: str,
        out_dir: Path,
        cost_sink: CostSink | None = None,
        post: Callable[..., Any] | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.out_dir = out_dir
        self.cost_sink = cost_sink
        self._post = post or httpx.post

    def generate_background(self, prompt: str, width: int, height: int) -> Path | None:
        size = f"{_snap16(width)}x{_snap16(height)}"
        r = self._post(
            ENDPOINT,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "prompt": PROMPT_PREFIX + prompt, "size": size, "n": 1},
            timeout=120.0,
        )
        if r.status_code >= 400:
            raise AdapterError(f"이미지 생성 실패 HTTP {r.status_code}: {r.text[:200]}")
        try:
            b64 = r.json()["data"][0]["b64_json"]
        except (KeyError, IndexError, ValueError) as e:
            raise AdapterError(f"이미지 응답 형식이 예상과 다릅니다: {e}") from e
        if self.cost_sink:
            self.cost_sink.record_cost("image", "openai", image_cost(self.model), "background")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"bg-{new_id()}.png"
        path.write_bytes(base64.b64decode(b64))
        return path


class NullImageGenerator:
    """이미지 생성을 끈 경우. 템플릿이 브랜드 색 배경을 쓴다."""

    def generate_background(self, prompt: str, width: int, height: int) -> Path | None:
        return None
