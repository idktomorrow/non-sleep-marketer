"""S12 제안: 생성기가 소식형 덱(products·facts)을 조립하고, 상품 이름·가격을 자료와 대조한다."""

from __future__ import annotations

from nsm.adapters.generator.claude_text import build_deck
from nsm.domain.models import ContentBrief, GeneratedContent, SourceItem
from nsm.domain.policy import TEMPLATES, choose_template
from nsm.domain.quality import unverified_products
from tests.fakes import load_source_items
from tests.unit.test_generator import _slide, llm_output


def _brief(sources):
    return ContentBrief(topic="굿즈 소식", template="editorial", sources=sources)


def test_build_deck_editorial_mixes_body_products_facts(app_config) -> None:
    src = load_source_items()[:2]
    out = llm_output("info", [s.id for s in src])
    out["slides"] = [
        _slide(heading="개요", body="무엇이 새로 나왔나"),
        _slide(
            heading="라인업",
            products=[
                {"name": "머그컵", "price": "¥5,500", "note": ""},
                {"name": "키링", "price": "", "note": "8종"},
                {"name": "", "price": "¥1", "note": ""},
            ],
        ),
        _slide(
            heading="일정",
            facts=[
                {"label": "출시", "value": "10월 23일"},
                {"label": "구매처", "value": "온라인몰"},
                {"label": "", "value": "버림"},
            ],
        ),
        _slide(
            heading="상품 1개뿐", products=[{"name": "하나", "price": "", "note": ""}]
        ),  # 2개 미만 → 버린다
    ]
    deck = build_deck("editorial", out, app_config.brand, app_config.persona, _brief(src))
    assert deck.template == "editorial"
    assert [s.kind for s in deck.slides] == ["cover", "body", "products", "facts", "outro"]
    assert [it.name for it in deck.slides[2].items] == ["머그컵", "키링"]
    assert [r.label for r in deck.slides[3].rows] == ["출시", "구매처"]


def test_choose_template_rotates_editorial_first_when_asked() -> None:
    assert choose_template([], ("editorial", *TEMPLATES)) == "editorial"
    assert choose_template(["editorial"], ("editorial", *TEMPLATES)) == "info"
    assert choose_template([], TEMPLATES) == "info"  # 기본 후보는 그대로


def test_unverified_products_flags_names_and_prices_missing_from_sources(app_config) -> None:
    src = [
        SourceItem(
            id="s1",
            source="naver",
            url="https://x/1",
            url_hash="h1",
            title="굿즈 공개",
            text="도자기 머그컵 ¥5,500, 아크릴 키링 ¥6,160 출시",
            collected_at=load_source_items()[0].collected_at,
        )  # fmt: skip
    ]
    out = llm_output("info", ["s1"])
    out["slides"] = [
        _slide(
            heading="라인업",
            products=[
                {"name": "도자기 머그컵", "price": "¥5,500", "note": ""},
                {"name": "아크릴 키링", "price": "¥6,600", "note": ""},  # 가격이 자료와 다름
                {"name": "클리어 파일", "price": "", "note": ""},  # 자료에 없는 상품
            ],
        ),
    ]
    deck = build_deck("editorial", out, app_config.brand, app_config.persona, _brief(src))
    gen = GeneratedContent(
        topic="t",
        template="editorial",
        deck=deck,
        instagram_caption="c",
        threads_text="t",
        blog_title="",
        blog_markdown="",
        claims=[],
    )
    reasons = unverified_products(gen, src)
    assert reasons == [
        "'아크릴 키링' 가격 ¥6,600이(가) 자료에 없음",
        "상품 '클리어 파일'이(가) 자료에 없음",
    ]


def test_same_slide_with_products_and_facts_builds_both(app_config) -> None:
    src = load_source_items()[:1]
    out = llm_output("info", [src[0].id])
    out["slides"] = [
        _slide(
            heading="라인업과 일정",
            products=[
                {"name": "머그컵", "price": "", "note": ""},
                {"name": "키링", "price": "", "note": ""},
            ],
            facts=[
                {"label": "출시", "value": "10월 23일"},
                {"label": "구매처", "value": "온라인몰"},
            ],
        )
    ]
    deck = build_deck("editorial", out, app_config.brand, app_config.persona, _brief(src))
    assert [s.kind for s in deck.slides] == ["cover", "products", "facts", "outro"]


def test_price_must_match_a_whole_number_in_sources(app_config) -> None:
    src = [
        SourceItem(
            id="s1",
            source="naver",
            url="https://x/1",
            url_hash="h1",
            title="굿즈",
            text="2025년 10월 23일 아크릴 키링 ¥12,310 출시",
            collected_at=load_source_items()[0].collected_at,
        )  # fmt: skip
    ]
    out = llm_output("info", ["s1"])
    out["slides"] = [
        _slide(
            heading="라인업",
            products=[
                {"name": "아크릴 키링", "price": "¥2,310", "note": ""},
                {"name": "아크릴 키링", "price": "¥12,310", "note": ""},
            ],
        )
    ]
    deck = build_deck("editorial", out, app_config.brand, app_config.persona, _brief(src))
    gen = GeneratedContent(topic="t", template="editorial", deck=deck, instagram_caption="c", threads_text="t",
                           blog_title="", blog_markdown="", claims=[])  # fmt: skip
    assert unverified_products(gen, src) == ["'아크릴 키링' 가격 ¥2,310이(가) 자료에 없음"]


def test_rule_checks_see_product_and_fact_text(app_config) -> None:
    from nsm.domain.quality import rule_violations, sns_texts

    src = load_source_items()[:1]
    out = llm_output("info", [src[0].id])
    out["slides"] = [
        _slide(
            heading="라인업",
            products=[
                {"name": "머그컵", "price": "", "note": "예상 가격 미정이라는 소문"},
                {"name": "키링", "price": "", "note": ""},
            ],
        )
    ]
    deck = build_deck("editorial", out, app_config.brand, app_config.persona, _brief(src))
    gen = GeneratedContent(topic="t", template="editorial", deck=deck, instagram_caption="c", threads_text="t",
                           blog_title="", blog_markdown="", claims=[])  # fmt: skip
    assert "소문" in sns_texts(gen)["카드 2장"]
    assert any("확인되지 않은" in v for v in rule_violations(gen, app_config.persona))


def test_products_and_facts_fields_only_requested_for_editorial() -> None:
    from nsm.adapters.generator.claude_text import llm_schema

    for template in ("info", "list", "compare"):
        slide = llm_schema(False, template)["properties"]["slides"]["items"]
        assert "products" not in slide["properties"] and "facts" not in slide["required"]
    slide = llm_schema(False, "editorial")["properties"]["slides"]["items"]
    assert {"products", "facts"} <= set(slide["required"])
