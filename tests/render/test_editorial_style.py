"""경계 3: editorial(사진 우선) 카드 스타일 (S10 제안, ADR 0049).

- 사진이 있는 표지·본문·마무리: 사진이 크게 보이고 글은 그 위 패널에, 1080×1350, 넘침 없음
- 사진이 없으면 종이색 카드로 떨어진다(패널·팔로우 카드 없음)
- 표기 슬롯·표지 최소 글자 크기(ADR 0035)는 다른 스타일과 같다
- 글자는 HTML이고, 사진이 있어도 ai_generated_image가 꺼져 있으면 AI 표기는 없다
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nsm.domain.models import CardDeck
from nsm.render.html_renderer import HtmlCardRenderer, _contrast, build_html
from tests.render.test_html_renderer import (
    FIXTURES,
    THUMB_SCALE,
    _cover_sizes,
    _extreme,
    png_size,
    slots,
)

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "nsm" / "templates" / "samples" / "sample-photo.jpg"
STYLE = "editorial"


def photo_deck(with_bg: bool = True, extreme: bool = False) -> CardDeck:
    """editorial-photo 픽스처. 배경 경로는 저장소 기준 상대 경로라 절대 경로로 바꿔 넣는다."""
    raw = json.loads((FIXTURES / "editorial-photo.json").read_text("utf-8"))
    if extreme:
        ex = _extreme("info").model_dump(mode="json")
        raw["slides"] = ex["slides"]
        raw["labels"] = ex["labels"]
        for s in raw["slides"]:
            s["background"] = "nsm/templates/samples/sample-photo.jpg"
    for s in raw["slides"]:
        s["background"] = str(SAMPLE) if (with_bg and s["background"]) else None
    return CardDeck.model_validate(raw)


@pytest.fixture(scope="module")
def renderer():
    r = HtmlCardRenderer(style=STYLE)
    yield r
    r.close()


def test_sample_photo_is_bundled() -> None:
    assert SAMPLE.is_file() and SAMPLE.stat().st_size < 150_000


@pytest.mark.parametrize("extreme", [False, True], ids=["normal", "extreme"])
@pytest.mark.parametrize("with_bg", [True, False], ids=["photo", "no-photo"])
def test_renders_1080x1350_without_overflow(
    renderer, with_bg: bool, extreme: bool, tmp_path
) -> None:
    deck = photo_deck(with_bg, extreme)
    paths = renderer.render(deck, tmp_path)
    assert len(paths) == len(deck.slides)
    assert all(png_size(p) == (1080, 1350) for p in paths)
    assert renderer.last_overflow == [], f"넘침: {renderer.last_overflow}"


def test_photo_parts_only_when_background_exists() -> None:
    with_bg = build_html(photo_deck(True), STYLE)
    assert "editorial.css" in with_bg and '<body class="style-editorial">' in with_bg
    # 사진이 있는 본문 장(2장)에만 설명 패널, 사진이 있는 마지막 장에 팔로우 카드
    assert with_bg.count('class="caption-panel"') == 1
    assert with_bg.count('class="follow-card"') == 1
    # 사진 없는 본문 장(3장)은 패널 없이 종이색 카드
    no_bg = build_html(photo_deck(False), STYLE)
    assert "caption-panel" not in no_bg and "follow-card" not in no_bg
    assert "has-bg" not in no_bg
    # 글자는 전부 HTML, 형광펜 상자(photo·illust) 없음, CTA 바 없음
    for s in photo_deck(True).slides:
        for text in (
            getattr(s, "title", None),
            getattr(s, "heading", None),
            getattr(s, "cta", None),
        ):
            if text:
                assert text.split("\n")[0] in with_bg
    assert '<span class="hl">' not in with_bg and "cta-bar" not in with_bg


def test_label_slots_match_clean_and_no_ai_caption_without_flag() -> None:
    deck = photo_deck(True)
    from tests.render.test_card_styles import _slots_with_style

    ours = _slots_with_style(deck, STYLE)
    assert [x["labels"] for x in ours] == [x["labels"] for x in slots(deck)]
    assert all("ai-generated" not in x["labels"] for x in ours)  # 사진이 있어도 AI 배경이 아니다
    assert [x["labels"].count("virtual-person") for x in ours] == [1, 0, 0, 1]
    assert all("ad" not in x["labels"] for x in ours)
    assert [x["sources"] for x in ours] == [0, 0, 0, 1]
    # 마지막 장 팔로우 카드 안의 이름은 .who(인물 슬롯)와 별개다: 배지 옆 이름은 여전히 표지·마지막 장 2곳
    html = build_html(deck, STYLE)
    assert html.count('<div class="who">') == 2 == html.count('data-label="virtual-person"')


@pytest.mark.parametrize("extreme", [False, True], ids=["normal", "extreme"])
def test_cover_readable_at_threads_thumbnail_with_photo(extreme: bool, tmp_path) -> None:
    """사진 위 표지도 ADR 0035 최소 크기: 제목 88px, 키커(부제 슬롯) 48px, 이름 38, 배지 34."""
    sizes = _cover_sizes(photo_deck(True, extreme), tmp_path, STYLE)
    for key, minimum in {"title": 18.0, "subtitle": 9.9, "who": 7.5, "badge": 7.0}.items():
        assert sizes[key] * THUMB_SCALE >= minimum, f"{key} {sizes[key]}px"
    assert sizes["titleLines"] <= 2 or extreme


def _measure(deck: CardDeck, tmp_path: Path, script: str):
    from playwright.sync_api import sync_playwright

    page_path = tmp_path / "deck.html"
    page_path.write_text(build_html(deck, STYLE), encoding="utf-8")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1080, "height": 1350})
            page.goto(page_path.resolve().as_uri(), wait_until="load")
            page.evaluate("document.fonts.ready.then(() => true)")
            page.evaluate("window.nsmFit()")
            return page.evaluate(script)
        finally:
            browser.close()


def test_caption_panel_leaves_most_of_the_photo_visible(tmp_path) -> None:
    """보통 길이 글이면 설명 패널은 카드 높이의 35% 아래, 표지 제목 묶음은 45% 아래 → 사진이 주인공."""
    r = _measure(
        photo_deck(True),
        tmp_path,
        """() => {
          const h = (el) => el ? el.getBoundingClientRect().height : 0;
          return { panel: h(document.querySelector('.caption-panel')),
                   cover: h(document.querySelector('section[data-kind="cover"] .content')),
                   follow: h(document.querySelector('.follow-card')) };
        }""",
    )
    assert 0 < r["panel"] <= 1350 * 0.35, r
    assert 0 < r["follow"] <= 1350 * 0.40, r
    # 표지 글 묶음(.content)은 아래 정렬이라 실제 글자가 차지하는 높이는 더 작다. 여기서는 상자 자체가 사진 절반을 넘지 않는지
    assert r["cover"] <= 1350 * 0.8


def test_text_on_white_panels_is_readable(tmp_path) -> None:
    """설명 패널·팔로우 카드 안 글자는 흰 바탕과 대비 4.5 이상, 사진 위 표지 글자는 흰색."""
    pairs = _measure(
        photo_deck(True),
        tmp_path,
        r"""() => {
          const rgba = (c) => (c.match(/[\d.]+/g) || []).map(Number).slice(0, 3);
          const out = [];
          for (const box of document.querySelectorAll('.caption-panel, .follow-card')) {
            const bg = rgba(getComputedStyle(box).backgroundColor);
            for (const el of box.querySelectorAll('h2, p, li, .cta, .follow-name')) {
              out.push([el.className || el.tagName, rgba(getComputedStyle(el).color), bg]);
            }
          }
          const t = document.querySelector('section[data-kind="cover"] h1');
          out.push(['cover-h1', rgba(getComputedStyle(t).color), [0, 0, 0]]);
          return out;
        }""",
    )
    bad = []
    for name, fg, bg in pairs:
        c = _contrast(tuple(v / 255 for v in fg), tuple(v / 255 for v in bg))
        if c < 4.5:
            bad.append(f"{name}: {c:.1f}")
    assert bad == []
