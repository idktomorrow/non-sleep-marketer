"""OpenAI 범용 이미지 제작기(S13 제안): 생성/편집 분기, 참조 상한, 투명 배경, 크기 검사, 정책 차단,
usage 기반 비용(종류별 단가), 전송 뒤 응답 없음 → unknown(자동 재시도 없음)."""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest

from nsm.adapters.generator.image_api import (
    ImageTransient,
    ImageUnavailable,
    OpenAIImageStudio,
    check_size,
)
from nsm.adapters.openai_common import OpenAIClient
from nsm.ports import ImageRefused, ImageSpec, ImageUnknown
from nsm.store.costs import CostLedger
from nsm.store.db import Database
from tests.fakes import FakeCostSink

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8ffff3f0005fe02fea7d6a4b00000000049454e44ae426082"
)


def _ok(usage: dict | None = None) -> httpx.Response:
    body: dict = {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body, headers={"x-request-id": "req_1"})


def _studio(handler, tmp_path: Path, sink=None):
    seen: list[httpx.Request] = []

    def wrapped(r):
        seen.append(r)
        return handler(r)

    client = OpenAIClient(
        "sk-img-key-0000000000000000", client=httpx.Client(transport=httpx.MockTransport(wrapped))
    )
    return OpenAIImageStudio(client, "gpt-image-2.5-flare", tmp_path / "studio", sink), seen


def test_generation_without_references_uses_json_endpoint(tmp_path: Path) -> None:
    sink = FakeCostSink()
    studio, seen = _studio(lambda r: _ok(), tmp_path, sink)
    res = studio.make(
        ImageSpec(prompt="a fluffy mascot", transparent=True, quality="low", ref="sticker:s1")
    )
    assert res.path.read_bytes() == PNG and res.cost_status == "estimated"
    req = seen[0]
    assert req.url.path.endswith("/images/generations")
    import json

    body = json.loads(req.content)
    assert body["background"] == "transparent" and body["output_format"] == "png"
    assert body["size"] == "1024x1024" and body["quality"] == "low"
    assert "no text" in body["prompt"].lower()
    assert sink.records[0][:2] == ("image", "openai") and sink.records[0][2] == pytest.approx(0.015)


def test_edits_with_references_is_multipart_and_capped(tmp_path: Path) -> None:
    refs = []
    for i in range(2):
        p = tmp_path / f"ref{i}.png"
        p.write_bytes(PNG)
        refs.append(p)
    studio, seen = _studio(
        lambda r: _ok(
            {
                "input_tokens": 1500,
                "input_tokens_details": {"image_tokens": 1200, "text_tokens": 300},
                "output_tokens": 1000,
            }
        ),
        tmp_path,
        FakeCostSink(),
    )
    res = studio.make(
        ImageSpec(prompt="same character waving", width=1536, height=1024, references=tuple(refs))
    )
    req = seen[0]
    assert req.url.path.endswith("/images/edits") and b'name="image[]"' in req.content
    assert res.cost_status == "confirmed"
    # 이미지 입력 $8·텍스트 입력 $5·출력 $30 /1M 을 종류별로 곱한다
    assert res.usd == pytest.approx((1200 * 8 + 300 * 5 + 1000 * 30) / 1_000_000)
    with pytest.raises(ValueError):
        studio.make(ImageSpec(prompt="x", references=tuple(refs * 9)))


def test_missing_reference_does_not_leave_reservation(tmp_path: Path, clock) -> None:
    ledger = CostLedger(Database(":memory:"), clock, workspace="기본")
    studio, seen = _studio(lambda r: _ok(), tmp_path, ledger)
    with pytest.raises(FileNotFoundError):
        studio.make(ImageSpec(prompt="x", references=(tmp_path / "nope.png",)))
    assert seen == [] and ledger.count() == 0  # 예약 전에 파일을 읽는다


def test_quality_falls_back_to_studio_setting(tmp_path: Path) -> None:
    import json

    studio, seen = _studio(lambda r: _ok(), tmp_path, FakeCostSink())
    studio.default_quality = "low"
    studio.make(ImageSpec(prompt="x"))
    assert json.loads(seen[0].content)["quality"] == "low"


def test_size_rules() -> None:
    assert check_size(1080, 1350) == (1072, 1344)
    with pytest.raises(ValueError):
        check_size(4000, 1000)  # 3:1 초과
    with pytest.raises(ValueError):
        check_size(512, 512)  # 픽셀 수 부족


def test_moderation_block_is_refused_with_reason(tmp_path: Path) -> None:
    studio, _ = _studio(
        lambda r: httpx.Response(
            400,
            json={
                "error": {
                    "code": "moderation_blocked",
                    "message": "blocked",
                    "moderation_details": {"categories": ["harassment"], "stage": "input"},
                }
            },
        ),
        tmp_path,
    )
    with pytest.raises(ImageRefused) as ei:
        studio.make(ImageSpec(prompt="x"))
    assert ei.value.categories == ("harassment",) and ei.value.stage == "input"


def test_timeout_after_send_is_unknown_and_not_retried(tmp_path: Path, clock) -> None:
    ledger = CostLedger(Database(":memory:"), clock, workspace="기본")
    calls = {"n": 0}

    def timeout(r):
        calls["n"] += 1
        raise httpx.ReadTimeout("no answer")

    studio, _ = _studio(timeout, tmp_path, ledger)
    with pytest.raises(ImageUnknown):
        studio.make(ImageSpec(prompt="x", quality="medium"))
    assert calls["n"] == 1
    day = clock.now()
    spent = ledger.breakdown(day.replace(hour=0), day.replace(hour=23))
    assert spent == {"unknown": pytest.approx(0.05)}  # 예약액이 상한에 계속 잡힌다
    assert ledger.unknown_requests()[0]["ref"] == ""


def test_rate_limit_is_transient_and_releases_reservation(tmp_path: Path, clock) -> None:
    ledger = CostLedger(Database(":memory:"), clock, workspace="기본")
    studio, _ = _studio(
        lambda r: httpx.Response(429, json={"error": {"code": "rate_limit_error"}}),
        tmp_path,
        ledger,
    )
    with pytest.raises(ImageTransient):
        studio.make(ImageSpec(prompt="x"))
    day = clock.now()
    assert ledger.spent_between(day.replace(hour=0), day.replace(hour=23)) == 0
    studio, _ = _studio(
        lambda r: httpx.Response(401, json={"error": {"code": "invalid_api_key"}}), tmp_path, ledger
    )
    with pytest.raises(ImageUnavailable):
        studio.make(ImageSpec(prompt="x"))
