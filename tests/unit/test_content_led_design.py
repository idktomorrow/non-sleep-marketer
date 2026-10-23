"""내용이 형식을 정한다(S18 제안, ADR 0062): 정보 줄 배치 검증, 마무리 장 선택, 사진 짝짓기, 여러 장 밈."""

from __future__ import annotations

from nsm.adapters.generator.prompts import (
    VIEWER_CHECK,
    build_deck,
    content_from_output,
    fact_layout,
    strip_viewer_checks,
)
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
from nsm.domain.quality import slide_texts
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


def test_article_images_skip_only_real_ad_segments_and_scope_captions() -> None:
    from nsm.adapters.sources.og_image import article_images

    html = (
        '<article><img src="/news/upload_2026/photo.jpg" alt="굿즈 사진">'
        '<img src="/img/logo.png"><img src="/thumbs/a.jpg" width="140" height="88">'
        '<img src="/lead-image.jpg"><figure><img src="/p2.jpg"><figcaption>팝콘통 굿즈</figcaption></figure></article>'
    )
    got = article_images(html, "https://news.example.com/a/1")
    urls = [u for u, _ in got]
    assert (
        "https://news.example.com/news/upload_2026/photo.jpg" in urls
    )  # 'upload_'는 광고가 아니다
    assert not any("logo" in u or "thumbs" in u for u in urls)  # 로고·작은 썸네일은 거른다
    desc = dict(got)
    assert desc["https://news.example.com/lead-image.jpg"] == ""  # 다음 그림의 캡션을 빌리지 않는다
    assert desc["https://news.example.com/p2.jpg"] == "팝콘통 굿즈"


def test_photo_placement_goes_through_port_and_is_validated() -> None:
    from types import SimpleNamespace

    from nsm.engine.photo_match import match_photos

    class Placer:
        def place_photos(self, cover_text, slides, photos, items=()):
            assert [i for i, _ in slides] == [1, 2]  # 표지는 본문 장 목록에 넣지 않는다
            assert items == []  # 상품 장이 없으면 상품 목록도 없다
            return {"cover": 1, "slides": [{"slide": 1, "photo": 0}, {"slide": 2, "photo": 1}, {"slide": 9, "photo": 0}],
                    "photos": [{"photo": 0, "shows": "팝콘통"}]}  # fmt: skip

    import tempfile
    from pathlib import Path

    d = Path(tempfile.mkdtemp())
    for n in ("a.jpg", "b.jpg"):
        (d / n).write_bytes(b"x")
    deck = CardDeck(
        template="editorial",
        brand=DeckBrand(name="b", primary_color="#112233", accent_color="#445566"),
        persona=DeckPersona(name="p", handle="@p"),
        slides=[
            CoverSlide(title="t"),
            BodySlide(heading="h", body="b"),
            BodySlide(heading="h2", body="b2"),
        ],
    )
    cands = [
        {"path": str(d / "a.jpg")},
        {"path": str(d / "missing.jpg")},
        {"path": str(d / "b.jpg")},
    ]
    p = match_photos(SimpleNamespace(text_generator=Placer()), deck, cands)
    # 없는 파일은 빼고 맞춘 뒤 원래 번호로: 표지=b(원래 2), 1장=a(0). 같은 사진 두 번·없는 장은 버린다
    assert p.method == "vision" and p.cover == 2 and p.slides == {1: 0} and p.notes == {0: "팝콘통"}


def test_blog_images_are_read_from_mobile_page() -> None:
    """네이버 블로그 PC 주소는 iframe 껍데기라 모바일 주소로 읽는다(S18 실호출: 블로그 자료 2건 모두 사진 0장)."""
    from pathlib import Path

    import httpx

    from nsm.adapters.sources.og_image import fetch_article_images

    seen = []

    def handler(req):
        seen.append(req.headers["host"])
        return httpx.Response(200, text="<html></html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetch_article_images(
        "https://blog.naver.com/u1/224451189345",
        Path("/tmp/x"),
        client=client,
        resolver=lambda h: ["93.184.216.34"],
    )
    assert seen == ["m.blog.naver.com"]


def test_each_product_gets_its_own_photo() -> None:
    """라인업 장의 상품 하나하나에 그 상품 사진(S18: 사진 위주 카드). 다른 장과 같은 사진은 두 번 쓰지 않는다."""
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace

    from nsm.domain.models import ProductItem, ProductsSlide
    from nsm.engine.photo_match import match_photos

    class Placer:
        def place_photos(self, cover_text, slides, photos, items=()):
            assert items == ["[1.0] A상 대형 인형 · 50cm", "[1.1] B상 갑옷 인형"]
            return {"cover": 2, "slides": [], "photos": [],
                    "items": [{"slide": 1, "item": 0, "photo": 0}, {"slide": 1, "item": 1, "photo": 1},
                              {"slide": 1, "item": 1, "photo": 2}, {"slide": 7, "item": 0, "photo": 1}]}  # fmt: skip

    d = Path(tempfile.mkdtemp())
    for n in ("a.jpg", "b.jpg", "c.jpg"):
        (d / n).write_bytes(b"x")
    deck = CardDeck(
        template="editorial",
        brand=DeckBrand(name="b", primary_color="#112233", accent_color="#445566"),
        persona=DeckPersona(name="p", handle="@p"),
        slides=[
            CoverSlide(title="t"),
            ProductsSlide(
                heading="경품",
                items=[
                    ProductItem(name="A상 대형 인형", note="50cm"),
                    ProductItem(name="B상 갑옷 인형"),
                ],
            ),
        ],
    )
    p = match_photos(
        SimpleNamespace(text_generator=Placer()),
        deck,
        [{"path": str(d / n)} for n in ("a.jpg", "b.jpg", "c.jpg")],
    )
    assert p.items == {(1, 0): 0, (1, 1): 1} and p.cover == 2  # 없는 장·두 번째 배정은 버린다


def test_viewer_checks_leave_cards_and_go_to_caption(app_config) -> None:
    """카드는 확인한 정보만(S18 사용자 결정 2026-10-23): '확인 필요'·'살펴봐요' 줄은 카드에서 빼고 캡션 '참고'로."""
    items = load_source_items()[:1]
    brief = ContentBrief(topic="t", template="editorial", sources=items)
    out = _out("배송비와 특전 조건도 살펴봐요")
    out["cover_subtitle"] = "상세 조건은 확인 필요"
    out["slides"][0]["facts"].append({"label": "가격", "value": "구체적인 정보 확인 필요"})
    content = content_from_output(
        {**out, "blog_title": "", "blog_markdown": ""}, brief, app_config.brand, app_config.persona
    )
    deck = content.deck
    texts = [t for s in deck.slides for t in slide_texts(s)]
    assert not any(VIEWER_CHECK.search(t) for t in texts)
    assert deck.slides[-1].kind != "outro" and deck.slides[0].subtitle == ""
    assert [r.label for r in deck.slides[1].rows] == ["예약", "출시"]  # 확인한 줄은 남는다
    assert "참고: " in content.instagram_caption and "가격: 구체적인 정보 확인 필요" in (
        content.instagram_caption
    )
    # 확인한 정보만 있는 덱은 그대로
    plain = build_deck("editorial", _out(""), app_config.brand, app_config.persona, brief)
    assert strip_viewer_checks(plain) == (plain, [])


def test_viewer_checks_also_cover_headings_compare_and_product_notes() -> None:
    from nsm.domain.models import CompareSide, CompareSlide, ProductItem, ProductsSlide

    deck = CardDeck(
        template="editorial",
        brand=DeckBrand(name="b", primary_color="#112233", accent_color="#445566"),
        persona=DeckPersona(name="p", handle="@p"),
        slides=[
            CoverSlide(title="t"),
            BodySlide(heading="가격은 확인 필요", body="본문"),
            CompareSlide(
                heading="한국과 일본",
                left=CompareSide(title="한국", points=["11월 출시"]),
                right=CompareSide(title="일본", points=["추후 공지"]),
            ),
            ProductsSlide(
                heading="인형 2종",
                items=[
                    ProductItem(name="A상 인형", price="미정", note="전 1종"),
                    ProductItem(name="B상 키링", price="1,100엔"),
                ],
            ),
        ],
    )
    out, notes = strip_viewer_checks(deck)
    assert [s.kind for s in out.slides] == ["cover", "products"]
    assert out.slides[1].items[0].price == "" and out.slides[1].items[0].note == "전 1종"
    assert "가격은 확인 필요" in notes and "추후 공지" in notes


def test_decimals_are_not_dates() -> None:
    assert fact_layout(_rows("2.5cm", "1.5배"), "timeline") == "rows"
    assert fact_layout(_rows("10.24 출시", "11/3 배송"), "timeline") == "timeline"


def test_malformed_vision_answer_falls_back_to_text_match() -> None:
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace

    from nsm.engine.photo_match import match_photos

    class Placer:
        def place_photos(self, cover_text, slides, photos, items=()):
            return {"cover": None, "slides": [{"slide": 1}]}

    d = Path(tempfile.mkdtemp())
    (d / "a.jpg").write_bytes(b"x")
    deck = CardDeck(
        template="editorial",
        brand=DeckBrand(name="b", primary_color="#112233", accent_color="#445566"),
        persona=DeckPersona(name="p", handle="@p"),
        slides=[CoverSlide(title="t"), BodySlide(heading="h", body="b")],
    )
    p = match_photos(SimpleNamespace(text_generator=Placer()), deck, [{"path": str(d / "a.jpg")}])
    assert p.method == "text"
