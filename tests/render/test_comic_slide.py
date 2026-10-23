"""경계 3: 2컷 만화 장(S14 제안, ADR 0055). 계약 변경이 들어간 제안이다.

- 네 스타일 모두 1080×1350·넘침 없음(긴 라벨·대사 포함), 라벨·말풍선은 HTML 글자, 그림 없는 컷은 "그림 없음" 자리
- 첫 장이 만화면 이름·가상인물 배지가 거기 붙는다(표지 역할). AI 표기는 ai_slides 목록(자산별)으로만
- 라벨·말풍선이 컷 밖으로 나가지 않고 서로 겹치지 않는다(실제 레이아웃 측정)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from nsm.domain.models import CardDeck
from nsm.render.html_renderer import HtmlCardRenderer, build_html
from tests.render.test_card_styles import STYLES
from tests.render.test_html_renderer import FIXTURES, png_size, slots

ROOT = Path(__file__).resolve().parents[2]


def deck(long: bool = False, missing: bool = False, ai_slides=(0,)) -> CardDeck:
    raw = json.loads((FIXTURES / "meme.json").read_text("utf-8"))
    raw["labels"]["ai_slides"] = list(ai_slides) if ai_slides is not None else None
    comic = raw["slides"][0]
    if long:
        comic["panels"][0]["label"] = "극중 카페 알바 루나 월급 이백이십오만원"[:30]
        comic["panels"][0]["bubble"] = "진짜 이게 맞나 싶은데 그래도 일단 출근은 한다"[:40]
        comic["panels"][1]["label"] = "현실의 루나 연봉 이백오십억 원 톱스타"[:30]
        comic["panels"][1]["bubble"] = "시러는 무슨 소리야 지금 당장 한 잔 더"[:40]
    if missing:
        comic["panels"][1]["image"] = None
    return CardDeck.model_validate(raw)


def test_contract_limits() -> None:
    raw = json.loads((FIXTURES / "meme.json").read_text("utf-8"))
    assert CardDeck.model_validate(raw).slides[0].kind == "comic"
    one = {
        **raw,
        "slides": [
            {**raw["slides"][0], "panels": raw["slides"][0]["panels"][:1]},
            raw["slides"][1],
        ],
    }
    with pytest.raises(ValidationError):
        CardDeck.model_validate(one)  # 컷은 정확히 2개
    bad_label = json.loads(json.dumps(raw))
    bad_label["slides"][0]["panels"][0]["label"] = "가" * 31
    with pytest.raises(ValidationError):
        CardDeck.model_validate(bad_label)
    info_with_comic = json.loads((FIXTURES / "info.json").read_text("utf-8"))
    info_with_comic["slides"].insert(1, raw["slides"][0])
    with pytest.raises(ValidationError):
        CardDeck.model_validate(info_with_comic)  # 정보형에는 만화를 섞을 수 없다
    # 소식형에는 섞을 수 있다(혼합 표현)
    ed = json.loads((FIXTURES / "editorial-sticker.json").read_text("utf-8"))
    assert any(s["kind"] == "comic" for s in ed["slides"]) and CardDeck.model_validate(ed)


@pytest.fixture(scope="module")
def renderers():
    rs = {s: HtmlCardRenderer(style=s) for s in STYLES}
    yield rs
    for r in rs.values():
        r.close()


@pytest.mark.parametrize("style", STYLES)
def test_renders_without_overflow_in_every_style(renderers, style: str, tmp_path: Path) -> None:
    for name, d in (("normal", deck()), ("long", deck(long=True)), ("missing", deck(missing=True))):
        paths = renderers[style].render(d, tmp_path / name)
        assert [png_size(p) for p in paths] == [(1080, 1350)] * 2
        assert renderers[style].last_overflow == [], f"{style}/{name} 넘침"


def test_markup_text_labels_and_missing_panel() -> None:
    html = build_html(deck(missing=True), "editorial")
    assert html.count('<figure class="panel') == 2 and html.count("panel-img") == 1
    assert "그림 없음" in html and "<svg" not in html  # 도형으로 대체하지 않는다
    assert "카페 알바 루나" in html and "시러~" in html  # 라벨·말풍선은 HTML 글자
    # 카드에는 이름·가상인물 배지를 그리지 않는다(S18 사용자 결정: 표기는 게시 글 첫 줄)
    assert 'data-label="virtual-person"' not in html and 'class="who"' not in html


def test_ai_caption_follows_ai_slides_list() -> None:
    s = slots(deck(ai_slides=(0,)))
    assert ["ai-generated" in x["labels"] for x in s] == [True, False]
    s = slots(deck(ai_slides=()))  # 빈 목록 = AI 없음(레거시 플래그로 돌아가지 않는다)
    assert ["ai-generated" in x["labels"] for x in s] == [False, False]
    legacy = deck(ai_slides=None).model_copy(
        update={
            "labels": deck().labels.model_copy(
                update={"ai_slides": None, "ai_generated_image": True}
            )
        }
    )
    assert ["ai-generated" in x["labels"] for x in slots(legacy)] == [
        False,
        False,
    ]  # 레거시: background 있는 장만


def test_labels_and_bubbles_stay_inside_panels(tmp_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    page = tmp_path / "deck.html"
    page.write_text(build_html(deck(long=True), "editorial"), encoding="utf-8")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            pg = browser.new_page(viewport={"width": 1080, "height": 1350})
            pg.goto(page.resolve().as_uri(), wait_until="load")
            pg.evaluate("window.nsmFit()")
            boxes = pg.evaluate(
                """() => [...document.querySelectorAll('.panel')].map(p => {
                  const r = e => { const b = e.getBoundingClientRect(); return [b.left, b.top, b.right, b.bottom]; };
                  return {panel: r(p), label: r(p.querySelector('.label')), bubble: r(p.querySelector('.bubble')),
                          size: parseFloat(getComputedStyle(p.querySelector('.label')).fontSize)};
                })"""
            )
        finally:
            browser.close()
    for b in boxes:
        pl, pt, pr, pb = b["panel"]
        for key in ("label", "bubble"):
            left, top, right, bottom = b[key]
            assert pl - 1 <= left and right <= pr + 1 and pt - 1 <= top and bottom <= pb + 1, (
                key,
                b,
            )
        ll, lt, lr, lb = b["label"]
        bl, bt, br, bb = b["bubble"]
        overlap = not (lr <= bl or br <= ll or lb <= bt or bb <= lt)
        assert not overlap, b
        assert b["size"] >= 40  # 라벨 최소 크기(썸네일에서 제목 역할)
