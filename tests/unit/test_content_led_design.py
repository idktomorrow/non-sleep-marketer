"""내용이 형식을 정한다(S18 제안, ADR 0062): 정보 줄 배치 검증, 마무리 장 선택, 사진 짝짓기, 여러 장 밈."""

from __future__ import annotations

from nsm.adapters.generator.prompts import build_deck, fact_layout
from nsm.domain.models import (
    BodySlide,
    CardDeck,
    ContentBrief,
    CoverSlide,
    DeckBrand,
    DeckPersona,
    FactRow,
    MemeImageSlide,
)
from tests.fakes import load_source_items


def _rows(*values: str) -> list[FactRow]:
    return [FactRow(label=f"항목{i}", value=v) for i, v in enumerate(values)]


def test_fact_layout_is_validated_against_data() -> None:
    dated = _rows("10월 22일(수) 예약 시작", "10월 24일 출시", "11/3 배송")
    assert fact_layout(dated, "timeline") == "timeline"
    # 날짜가 아닌 줄이 섞이면 타임라인이 아니다(조건 묶음 등)
    assert fact_layout(_rows("10월 22일 시작", "1인 2개까지"), "timeline") == "rows"
    assert fact_layout(_rows("187만 명", "37개관"), "figures") == "figures"
    assert fact_layout(_rows("전국 CGV", "1인 2개"), "figures") == "rows"
    assert fact_layout(_rows("1인 2개까지", "현장 결제만"), "conditions") == "conditions"


def _out(cta: str) -> dict:
    blank = {
        "left_title": "",
        "left_points": [],
        "right_title": "",
        "right_points": [],
        "products": [],
    }
    return {
        "topic": "t",
        "cover_title": "제목",
        "cover_subtitle": "",
        "slides": [
            {
                **blank,
                "heading": "일정",
                "body": "",
                "bullets": [],
                "facts": [
                    {"label": "예약", "value": "10월 22일 오전 11시"},
                    {"label": "출시", "value": "10월 24일"},
                ],
                "facts_layout": "timeline",
                "photo_hint": "굿즈 사진",
            },
            {
                **blank,
                "heading": "핵심",
                "body": "",
                "bullets": ["하나", "둘", "셋"],
                "facts": [],
                "facts_layout": "rows",
                "photo_hint": "",
            },
        ],
        "cta": cta,
        "instagram_caption": "c",
        "threads_text": "t",
        "claims": [],
    }


def test_editorial_closing_is_optional_and_sources_kept_on_deck(app_config) -> None:
    items = load_source_items()[:1]
    brief = ContentBrief(topic="t", template="editorial", sources=items)
    deck = build_deck("editorial", _out(""), app_config.brand, app_config.persona, brief)
    assert [s.kind for s in deck.slides] == ["cover", "facts", "list"]  # 마무리 장 없음
    assert (
        deck.sources
        and deck.slides[1].layout == "timeline"
        and deck.slides[1].photo_hint == "굿즈 사진"
    )
    with_cta = build_deck(
        "editorial",
        _out("어떤 디자인이 제일 갖고 싶어요?"),
        app_config.brand,
        app_config.persona,
        brief,
    )
    assert with_cta.slides[-1].kind == "outro"  # 도움이 되는 질문은 허용


def test_text_photo_match_only_when_caption_fits() -> None:
    from nsm.engine.photo_match import _text_match

    deck = CardDeck(
        template="editorial",
        brand=DeckBrand(name="b", primary_color="#112233", accent_color="#445566"),
        persona=DeckPersona(name="p", handle="@p"),
        slides=[
            CoverSlide(title="치이카와 팝콘통"),
            BodySlide(
                heading="팝콘통 디자인",
                body="치이카와 팝콘통 굿즈 세 종",
                photo_hint="팝콘통 굿즈 사진",
            ),
            BodySlide(heading="4DX 상영", body="전국 37개관", photo_hint="4DX 포스터"),
        ],
    )
    cands = [
        {"path": "a", "kind": "og", "alt": ""},
        {"path": "b", "kind": "body", "alt": "치이카와 팝콘통 굿즈"},
        {"path": "c", "kind": "body", "alt": "극장 로비 전경"},
    ]
    p = _text_match(deck, cands)
    assert p.cover == 0 and p.slides == {1: 1}  # 맞는 설명이 없는 장(2번)은 비운다


def test_multi_page_meme_chains_second_page_to_first() -> None:
    from nsm.domain.production import Cast, CharacterRef, image_slots

    deck = CardDeck(
        template="meme",
        brand=DeckBrand(name="b", primary_color="#112233", accent_color="#445566"),
        persona=DeckPersona(name="p", handle="@p"),
        slides=[MemeImageSlide(scene="기대"), MemeImageSlide(scene="현실")],
    )
    slots = image_slots(deck, Cast(hero=CharacterRef(kind="account", name="h")))
    assert [(s.target, s.chain_from) for s in slots] == [
        ("s0.image", None),
        ("s1.image", "s0.image"),
    ]


def test_fan_and_original_candidates_are_added() -> None:
    from datetime import UTC, datetime

    from nsm.domain.meme_discovery import MemeCandidate, Sighting
    from nsm.engine.memes import MemeContext, with_fan_and_original

    now = datetime(2026, 10, 22, tzinfo=UTC)
    sg = Sighting(
        "https://blog.naver.com/u1/1",
        "치이카와 짤 모음",
        "울면서 버티기",
        "네이버 블로그",
        now,
        now,
        "blog.naver.com/u1",
    )
    mc = MemeContext(topic="치이카와", found=[MemeCandidate("치이카와 짤 모음", "C", [sg])])
    out = with_fan_and_original([], mc)
    assert [c.era for c in out] == ["fan", "original"]
    assert "반응 수치 미확인" in out[0].evidence and out[0].latest_evidence == "2026-10-22"
