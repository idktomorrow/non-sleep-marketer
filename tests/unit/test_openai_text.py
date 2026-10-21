"""OpenAI 텍스트 생성기(S13 제안): Responses API strict 스키마, 오류 분류, 비용 원장 연결.

- 성공: 공용 프롬프트 스키마 그대로 덱이 조립되고 usage × 단가로 비용이 확정된다
- refusal·incomplete·깨진 JSON·계약 불일치 → AdapterError(이 자료만 건너뜀)
- 401/429(크레딧) → ProviderUnavailable(영구), 429(한도)/503/연결 실패 → TransientError(재예약), 재시도 없음
- 전송 뒤 응답 없음(ReadTimeout) → 1회만 다시 보내고 첫 요청 비용은 unknown
- 요청에 store=false·strict 스키마가 들어가고, 키는 예외 문구에 없다
"""

from __future__ import annotations

import json

import httpx
import pytest

from nsm.adapters.generator.openai_text import (
    OpenAITextGenerator,
    ProviderUnavailable,
    TransientError,
)
from nsm.adapters.openai_common import OpenAIClient
from nsm.domain.models import Comment, ContentBrief
from nsm.ports import AdapterError
from nsm.store.costs import CostLedger
from nsm.store.db import Database
from tests.fakes import FakeCostSink, load_source_items

KEY = "sk-test-secret-key-000000000000"


def _deck_output() -> dict:
    return {
        "topic": "핸드드립 물 온도",
        "cover_title": "물 온도,\n왜 92도일까",
        "cover_subtitle": "추출이 달라지는 3가지 이유",
        "slides": [
            {
                "heading": "92도의 이유",
                "body": "너무 뜨거우면 쓴맛, 식으면 밍밍해요.",
                "bullets": [],
                "left_title": "",
                "left_points": [],
                "right_title": "",
                "right_points": [],
            }
        ],
        "cta": "여러분은 몇 도로 내리세요?",
        "instagram_caption": "물 온도 이야기 " * 20,
        "threads_text": "물 온도 92도",
        "claims": [{"text": "92도가 적정", "source_item_id": "src1"}],
    }


def _response(payload: dict, status: str = "completed", refusal: str | None = None) -> dict:
    content = (
        [{"type": "refusal", "refusal": refusal}]
        if refusal
        else [{"type": "output_text", "text": json.dumps(payload, ensure_ascii=False)}]
    )
    return {
        "id": "resp_1",
        "status": status,
        "model": "gpt-6.1-sol",
        "output": [{"type": "reasoning", "summary": []}, {"type": "message", "content": content}],
        "usage": {"input_tokens": 1000, "output_tokens": 500},
        **(
            {"incomplete_details": {"reason": "max_output_tokens"}}
            if status == "incomplete"
            else {}
        ),
    }


def _gen(handler, sink=None, effort="low") -> tuple[OpenAITextGenerator, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = OpenAIClient(KEY, client=httpx.Client(transport=httpx.MockTransport(wrapped)))
    return OpenAITextGenerator(client, "gpt-6.1-sol", sink, effort=effort, include_blog=False), seen


def _brief() -> ContentBrief:
    items = load_source_items()[:1]
    items[0] = items[0].model_copy(update={"id": "src1"})
    return ContentBrief(topic="핸드드립: 물 온도", template="info", sources=items)


def test_success_builds_deck_and_records_cost(app_config) -> None:
    sink = FakeCostSink()
    gen, seen = _gen(lambda r: httpx.Response(200, json=_response(_deck_output())), sink)
    out = gen.generate_content(_brief(), app_config.persona, app_config.brand)
    assert out.deck.slides[0].title.startswith("물 온도") and out.claims[0].source_item_id == "src1"
    body = json.loads(seen[0].content)
    assert body["store"] is False and body["text"]["format"]["strict"] is True
    assert body["text"]["format"]["type"] == "json_schema" and body["reasoning"] == {
        "effort": "low"
    }
    assert "blog_title" not in body["text"]["format"]["schema"]["properties"]
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    kind, provider, usd, ref = sink.records[0]
    assert (kind, provider, ref) == ("llm", "openai", "generate:info")
    assert usd == pytest.approx((1000 * 2 + 500 * 10) / 1_000_000)


@pytest.mark.parametrize(
    "make",
    [
        lambda: httpx.Response(200, json=_response({}, refusal="안 됩니다")),
        lambda: httpx.Response(200, json=_response(_deck_output(), status="incomplete")),
        lambda: httpx.Response(
            200,
            json={
                **_response({}),
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "{bad"}]}
                ],
            },
        ),
        lambda: httpx.Response(200, json=_response({"topic": "x"})),  # 계약 불일치
        lambda: httpx.Response(400, json={"error": {"message": "bad schema", "code": "invalid"}}),
    ],
    ids=["refusal", "incomplete", "bad-json", "contract", "400"],
)
def test_material_level_failures_are_adapter_errors(app_config, make) -> None:
    gen, seen = _gen(lambda r: make())
    with pytest.raises(AdapterError):
        gen.generate_content(_brief(), app_config.persona, app_config.brand)
    assert len(seen) == 1  # 재시도 없음


def test_auth_and_quota_are_permanent_without_retry(app_config) -> None:
    for status, err in (
        (401, {"message": "Incorrect API key", "code": "invalid_api_key"}),
        (
            429,
            {"message": "no credits", "code": "insufficient_quota", "type": "insufficient_quota"},
        ),
        (429, {"message": "limit", "code": "project_spend_limit_exceeded"}),
    ):
        gen, seen = _gen(lambda r, s=status, e=err: httpx.Response(s, json={"error": e}))
        with pytest.raises(ProviderUnavailable) as ei:
            gen.generate_content(_brief(), app_config.persona, app_config.brand)
        assert len(seen) == 1 and KEY not in str(ei.value)


def test_rate_limit_overload_and_connect_errors_are_transient(app_config) -> None:
    gen, seen = _gen(
        lambda r: httpx.Response(
            429, headers={"retry-after": "7"}, json={"error": {"code": "rate_limit_error"}}
        )
    )
    with pytest.raises(TransientError) as ei:
        gen.generate_content(_brief(), app_config.persona, app_config.brand)
    assert ei.value.retry_after == 7 and len(seen) == 1
    gen, _ = _gen(lambda r: httpx.Response(503, json={"error": {"code": "server_is_overloaded"}}))
    with pytest.raises(TransientError):
        gen.generate_content(_brief(), app_config.persona, app_config.brand)

    def connect_fail(r):
        raise httpx.ConnectError("refused")

    gen, _ = _gen(connect_fail)
    with pytest.raises(TransientError):
        gen.generate_content(_brief(), app_config.persona, app_config.brand)


def test_read_timeout_retries_once_and_marks_first_cost_unknown(app_config, clock) -> None:
    ledger = CostLedger(Database(":memory:"), clock, workspace="기본")
    calls = {"n": 0}

    def flaky(r):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json=_response(_deck_output()))

    gen, _ = _gen(flaky, ledger)
    gen.generate_content(_brief(), app_config.persona, app_config.brand)
    assert calls["n"] == 2
    statuses = ledger.breakdown(clock.now().replace(hour=0), clock.now().replace(hour=23))
    assert "unknown" in statuses and "confirmed" in statuses
    # 두 번째도 응답이 없으면 포기(세 번째 호출 없음)

    def always(r):
        calls["n"] += 1
        raise httpx.ReadTimeout("slow")

    calls["n"] = 0
    gen, _ = _gen(always, ledger)
    with pytest.raises(TransientError):
        gen.generate_content(_brief(), app_config.persona, app_config.brand)
    assert calls["n"] == 2


def test_ledger_reserve_then_confirm_is_single_row(app_config, clock) -> None:
    ledger = CostLedger(Database(":memory:"), clock, workspace="기본")
    gen, _ = _gen(lambda r: httpx.Response(200, json=_response(_deck_output())), ledger)
    gen.generate_content(_brief(), app_config.persona, app_config.brand)
    assert ledger.count() == 1
    day = clock.now()
    assert ledger.spent_between(day.replace(hour=0), day.replace(hour=23)) == pytest.approx(0.007)


def test_reply(app_config, clock) -> None:
    gen, seen = _gen(
        lambda r: httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": '{"reply": " 고마워요! "}'}],
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )
    )
    c = Comment(
        platform="threads",
        external_id="c1",
        post_external_id="p1",
        author_id="u1",
        author_name="a",
        text="좋아요",
        created_at=clock.now(),
    )
    assert gen.generate_reply(c, app_config.persona, "본문") == "고마워요!"
    assert json.loads(seen[0].content)["text"]["format"]["name"] == "reply"
