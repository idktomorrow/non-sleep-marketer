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


# ---- S13 제안: 범용 이미지 제작기(스티커·만화 컷, 참조 이미지 기반) --------------------------------------------
# 표지 배경용 OpenAIImageGenerator와 달리 역할·참조·투명 배경을 받는 ImageStudio 포트 구현. 재시도는 하지 않는다:
# 전송 뒤 응답을 못 받으면 ImageUnknown(과금 불명), 정책 차단은 ImageRefused, 한도·과부하·연결 실패는
# ImageTransient(호출자가 next_attempt_at으로 재예약), 키·크레딧은 ImageUnavailable(영구).

from nsm.adapters.openai_common import OpenAIClient, OpenAIError  # noqa: E402
from nsm.adapters.pricing import IMAGE_PRICE_ESTIMATE, image_cost_from_usage  # noqa: E402
from nsm.ports import (  # noqa: E402
    CostLedger,
    ImageRefused,
    ImageResult,
    ImageSpec,
    ImageUnknown,
)

MAX_REFERENCES = 16  # /v1/images/edits image[] 상한(공식 문서)
MIN_PIXELS, MAX_PIXELS = 655_360, 8_294_400
NO_TEXT = (
    " Absolutely no text, letters, numbers, captions, speech bubbles, logos or watermarks "
    "in the image."
)
IMAGE_TIMEOUT_S = 150.0


class ImageTransient(Exception):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ImageUnavailable(Exception):
    """키·크레딧·권한 문제(영구)."""


def check_size(width: int, height: int) -> tuple[int, int]:
    """16의 배수, 비율 1:3~3:1, 픽셀 수 범위(공식 제약)를 맞춘다. 범위 밖이면 ValueError."""
    w, h = _snap16(width), _snap16(height)
    ratio = max(w, h) / min(w, h)
    if ratio > 3:
        raise ValueError(f"이미지 비율은 1:3~3:1 안이어야 합니다({w}x{h})")
    if not (MIN_PIXELS <= w * h <= MAX_PIXELS):
        raise ValueError(f"이미지 픽셀 수가 허용 범위 밖입니다({w}x{h})")
    return w, h


def _mime(path: Path) -> str:
    suf = path.suffix.lower()
    return {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
    }.get(suf.lstrip("."), "application/octet-stream")


class OpenAIImageStudio:
    provider = "openai"

    def __init__(
        self,
        client: OpenAIClient,
        model: str,
        out_dir: Path,
        cost_sink: CostSink | None = None,
        default_quality: str = "medium",
    ) -> None:
        self.client = client
        self.model = model
        self.out_dir = out_dir
        self.cost_sink = cost_sink
        self.default_quality = default_quality

    def make(self, spec: ImageSpec) -> ImageResult:
        if len(spec.references) > MAX_REFERENCES:
            raise ValueError(f"참조 이미지는 {MAX_REFERENCES}장까지입니다")
        w, h = check_size(spec.width, spec.height)
        quality = spec.quality or self.default_quality
        prompt = (
            spec.prompt.strip()
            + NO_TEXT
            + (f" Avoid: {spec.negative.strip()}" if spec.negative else "")
        )
        fields: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "size": f"{w}x{h}",
            "quality": quality,
            "n": 1,
            "output_format": "png",
        }
        if spec.transparent:
            fields["background"] = "transparent"
        ledger = self.cost_sink if isinstance(self.cost_sink, CostLedger) else None
        estimate = IMAGE_PRICE_ESTIMATE.get(quality, IMAGE_PRICE_ESTIMATE["medium"])
        rid = ledger.reserve("image", "openai", estimate, spec.ref, self.model) if ledger else ""
        try:
            if spec.references:
                files = [("image[]", (p.name, p.read_bytes(), _mime(p))) for p in spec.references]
                out = self.client.post_multipart(
                    "images/edits",
                    {k: str(v) for k, v in fields.items()},
                    files,
                    timeout=IMAGE_TIMEOUT_S,
                )
            else:
                out = self.client.post_json("images/generations", fields, timeout=IMAGE_TIMEOUT_S)
        except OpenAIError as e:
            raise self._fail(ledger, rid, e) from None
        try:
            b64 = out["data"][0]["b64_json"]
        except (KeyError, IndexError, TypeError) as e:
            if ledger:
                ledger.mark_unknown(rid, str(out.get("_request_id") or ""))
            raise AdapterError(f"이미지 응답 형식이 예상과 다릅니다: {e}") from e
        usage = out.get("usage") or {}
        usd, status = image_cost_from_usage(self.model, usage, quality)
        request_id = str(out.get("_request_id") or "")
        if ledger:
            ledger.confirm(
                rid,
                usd,
                status,
                detail={"model": self.model, "usage": usage},
                request_id=request_id,
            )  # type: ignore[arg-type]
        elif self.cost_sink:
            self.cost_sink.record_cost("image", "openai", usd, spec.ref)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"img-{new_id()}.png"
        path.write_bytes(base64.b64decode(b64))
        return ImageResult(
            path=path,
            model=self.model,
            usd=usd,
            cost_status=status,
            request_id=request_id,
            usage=usage,
        )  # type: ignore[arg-type]

    @staticmethod
    def _fail(ledger: CostLedger | None, rid: str, e: OpenAIError) -> Exception:
        """원장 정리 뒤 호출자가 올릴 예외를 돌려준다."""
        if e.kind == "timeout_unknown":
            if ledger and rid:
                ledger.mark_unknown(rid, e.request_id)
            return ImageUnknown(str(e))
        if ledger and rid:
            ledger.release(rid)
        if e.kind == "moderation":
            cats = tuple((e.details or {}).get("categories") or ())
            return ImageRefused(str(e), cats, str((e.details or {}).get("stage") or ""))
        if e.kind in ("auth", "quota"):
            return ImageUnavailable(str(e))
        if e.reschedulable:
            return ImageTransient(str(e), e.retry_after)
        return AdapterError(str(e))
