"""경계 3: editorial(사진 우선) 카드 스타일 (S10 제안, ADR 0050).

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
from nsm.render.html_renderer import HtmlCardRenderer, _contrast, _luminance, build_html
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
    assert all("virtual-person" not in x["labels"] for x in ours)  # S18: 카드에 없음
    assert all("ad" not in x["labels"] for x in ours)
    assert [x["sources"] for x in ours] == [0, 0, 0, 1]
    # 이름·핸들·가상인물 배지는 카드에 없다(S18). 팔로우 카드는 브랜드 이름만
    html = build_html(deck, STYLE)
    assert html.count('<div class="who">') == 0 == html.count('data-label="virtual-person"')
    assert html.count(deck.persona.handle) == 0
    assert f'<div class="follow-name">{deck.brand.name}</div>' in html


@pytest.mark.parametrize("extreme", [False, True], ids=["normal", "extreme"])
def test_cover_readable_at_threads_thumbnail_with_photo(extreme: bool, tmp_path) -> None:
    """사진 위 표지도 ADR 0035 최소 크기: 제목 88px, 키커(부제 슬롯) 48px, 이름 38, 배지 34."""
    sizes = _cover_sizes(photo_deck(True, extreme), tmp_path, STYLE)
    for key, minimum in {"title": 18.0, "subtitle": 9.9}.items():
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
                   cover: h(document.querySelector('section[data-kind="cover"] .titles')),
                   follow: h(document.querySelector('.follow-card')) };
        }""",
    )
    assert 0 < r["panel"] <= 1350 * 0.35, r
    assert 0 < r["follow"] <= 1350 * 0.40, r
    # 표지 글 묶음(띠 포함)은 카드의 아래 절반 안에
    assert 0 < r["cover"] <= 1350 * 0.5, r


def test_text_on_white_panels_is_readable(tmp_path) -> None:
    """설명 패널·팔로우 카드 안 글자는 흰 바탕과 대비 4.5 이상(사진 위 글자는 아래 밝은 사진 테스트)."""
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
          return out;
        }""",
    )
    bad = []
    for name, fg, bg in pairs:
        c = _contrast(tuple(v / 255 for v in fg), tuple(v / 255 for v in bg))
        if c < 4.5:
            bad.append(f"{name}: {c:.1f}")
    assert bad == []


def _white_photo(tmp_path: Path, shade: int = 255) -> Path:
    from PIL import Image

    p = tmp_path / f"bright-{shade}.png"
    Image.new("RGB", (1080, 1350), (shade, shade, shade)).save(p)
    return p


@pytest.mark.parametrize("extreme", [False, True], ids=["normal", "extreme"])
def test_cover_text_readable_on_bright_photo(extreme: bool, tmp_path) -> None:
    """가장 밝은 사진(순백)에서도 표지 제목·키커·이름·페이지 번호 뒤 바탕이 흰 글자와 대비 3.0 이상(큰 글자 기준).

    글자를 투명하게 만든 뒤 글자 상자 자리의 실제 화면 밝기를 잰다(그라디언트·알약 바탕이 충분한지).
    """
    import io

    from PIL import Image, ImageStat
    from playwright.sync_api import sync_playwright

    raw = json.loads((FIXTURES / "editorial-photo.json").read_text("utf-8"))
    if extreme:
        ex = _extreme("info").model_dump(mode="json")
        raw["slides"], raw["labels"] = ex["slides"], ex["labels"]
    white = str(_white_photo(tmp_path))
    for s in raw["slides"]:
        s["background"] = white
    deck = CardDeck.model_validate(raw)
    page_path = tmp_path / "deck.html"
    page_path.write_text(build_html(deck, STYLE), encoding="utf-8")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1080, "height": 1350})
            page.goto(page_path.resolve().as_uri(), wait_until="load")
            page.evaluate("document.fonts.ready.then(() => true)")
            page.evaluate("window.nsmFit()")
            boxes = page.evaluate(
                """() => {
                  const s = document.querySelector('section.slide[data-kind="cover"]');
                  const rgba = (c) => { const n = (c.match(/[\\d.]+/g) || []).map(Number); return (c.startsWith('color(') ? n.map(v => v * 255) : n).slice(0, 3); };
                  const out = {};
                  for (const [k, sel] of [['title','h1'], ['kicker','.subtitle']]) {
                    const el = s.querySelector(sel); const r = el.getBoundingClientRect();
                    out[k] = [[r.left, r.top, r.right, r.bottom], rgba(getComputedStyle(el).color)];
                  }
                  return out;
                }"""
            )
            page.add_style_tag(
                content=".slide * { color: transparent !important; text-shadow: none !important; }"
            )
            shot = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
        finally:
            browser.close()
    bad = []
    for name, ((left, top, right, bottom), fg) in boxes.items():
        region = shot.crop((int(left), int(top), int(right), int(bottom)))
        mean = tuple(ImageStat.Stat(region).mean[:3])
        lf, lb = _luminance(tuple(v / 255 for v in fg)), _luminance(tuple(v / 255 for v in mean))
        contrast = (max(lf, lb) + 0.05) / (min(lf, lb) + 0.05)  # 글자색 대 글자 자리 바탕
        if contrast < 3.0:
            bad.append(f"{name}: {contrast:.2f} (바탕 평균 {tuple(round(m) for m in mean)})")
    assert bad == []


def test_long_caption_panel_shrinks_instead_of_clipping_upward(tmp_path) -> None:
    """아주 긴 본문도 패널이 위로 넘쳐 잘리지 않고 글자가 줄어들어 들어간다(nsmFit이 넘침을 볼 수 있어야 한다)."""
    raw = json.loads((FIXTURES / "editorial-photo.json").read_text("utf-8"))
    long = "아주 긴 문장이 들어와도 카드가 깨지지 않아야 합니다 그래서 일부러 길게 씁니다. "
    body = raw["slides"][1]
    body["heading"], body["body"], body["bullets"] = (
        "소제목도 길게 들어오는 경우",
        long * 6,
        [long] * 6,
    )
    for s in raw["slides"]:
        s["background"] = str(SAMPLE) if s["background"] else None
    deck = CardDeck.model_validate(raw)
    r = _measure(
        deck,
        tmp_path,
        """() => {
          const s = document.querySelector('section.slide[data-kind="body"].has-bg');
          const c = s.querySelector('.content').getBoundingClientRect();
          const p = s.querySelector('.caption-panel').getBoundingClientRect();
          const h2 = parseFloat(getComputedStyle(s.querySelector('h2')).fontSize);
          return { contentTop: c.top, panelTop: p.top, panelBottom: p.bottom, contentBottom: c.bottom, h2 };
        }""",
    )
    assert r["panelTop"] >= r["contentTop"] - 1, r  # 위로 잘리지 않음
    assert r["panelBottom"] <= r["contentBottom"] + 1, r
    assert r["h2"] < 56, r  # 글자가 실제로 줄었다


def test_repo_relative_background_resolves_from_any_cwd(tmp_path, monkeypatch) -> None:
    """픽스처의 저장소 기준 상대 경로는 다른 폴더에서 render-preview해도 사진을 찾는다(S10 리뷰)."""
    monkeypatch.chdir(tmp_path)
    deck = CardDeck.model_validate_json((FIXTURES / "editorial-photo.json").read_text("utf-8"))
    html = build_html(deck, STYLE)
    assert html.count("has-bg") == 3 and SAMPLE.resolve().as_uri() in html
    missing = deck.model_copy(
        update={
            "slides": [
                deck.slides[0].model_copy(update={"background": "nope/x.jpg"}),
                *deck.slides[1:],
            ]
        }
    )
    assert (
        "has-bg" in build_html(missing, STYLE) and build_html(missing, STYLE).count("has-bg") == 2
    )


def test_no_code_drawn_character_or_props(tmp_path) -> None:
    """S14: 사진 우선형은 코드 도형 캐릭터·키워드 소품을 그리지 않는다(그림은 사람이 고른 사진·스티커뿐)."""
    html = build_html(photo_deck(), STYLE)
    assert "kawaii" not in html and "prop-icon" not in html and "<svg" not in html
    nobg = build_html(photo_deck(with_bg=False), STYLE)
    assert "<svg" not in nobg and "<img" not in nobg.replace('class="logo"', "")
