"""경계 3: 카드 디자인 스타일 clean·photo·illust (S8 #34, ADR 0036).

- 세 스타일 모두 1080×1350 PNG, 긴 글에서도 넘치지 않음
- 표기 슬롯(가상인물·광고·페이지·출처)은 스타일과 무관하게 같다
- 코드로 그린 배경은 "AI 생성 이미지" 표기 대상이 아니다(배경 사진이 있는 장만)
- 글자는 HTML로 올리고 이미지에 굽지 않는다, CTA 바는 그리지 않는다
표지 가독성(썸네일 최소 크기)은 test_html_renderer.py의 스타일별 테스트가 본다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nsm.render.html_renderer import HtmlCardRenderer, build_html, template_version
from tests.render.test_html_renderer import _extreme, load, png_size, slots

STYLES = ["clean", "photo", "illust", "editorial", "kitsch"]
TEMPLATES = ["info", "list", "compare"]


@pytest.fixture(scope="module")
def renderers():
    rs = {s: HtmlCardRenderer(style=s) for s in STYLES}
    yield rs
    for r in rs.values():
        r.close()


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("template", TEMPLATES)
def test_each_style_renders_1080x1350_without_overflow(
    renderers, style: str, template: str, tmp_path: Path
) -> None:
    for name, deck in (("normal", load(template)), ("extreme", _extreme(template))):
        paths = renderers[style].render(deck, tmp_path / name)
        assert len(paths) == len(deck.slides)
        assert all(png_size(p) == (1080, 1350) for p in paths)
        assert renderers[style].last_overflow == [], (
            f"{name} 넘침: {renderers[style].last_overflow}"
        )


@pytest.mark.parametrize("style", ["photo", "illust", "editorial"])
@pytest.mark.parametrize("template", TEMPLATES)
def test_label_slots_do_not_depend_on_style(style: str, template: str) -> None:
    deck = load(template)
    assert _slots_with_style(deck, style) == slots(deck)  # 같은 장, 같은 표기 슬롯
    html = build_html(deck, style)
    # 이름·가상인물 배지는 카드에 그리지 않는다(S18: 게시 글 첫 줄 표기)
    assert html.count('data-label="virtual-person"') == html.count('<div class="who">') == 0
    assert html.count('data-label="ad"') == (1 if deck.labels.ad else 0)


def _slots_with_style(deck, style: str) -> list[dict]:
    from tests.render.test_html_renderer import Slots

    p = Slots()
    p.feed(build_html(deck, style))
    return p.slides


@pytest.mark.parametrize("style", STYLES)
def test_code_drawn_background_has_no_ai_caption(style: str, tmp_path: Path) -> None:
    """배경 사진이 없는 장은 코드로 그린 배경이라 ai_generated_image가 켜져 있어도 표기하지 않는다."""
    deck = load("info")  # 배경 사진 없음
    deck = deck.model_copy(
        update={"labels": deck.labels.model_copy(update={"ai_generated_image": True})}
    )
    assert 'data-label="ai-generated"' not in build_html(deck, style)
    # 배경 사진이 있는 장에는 스타일과 무관하게 붙는다
    bg = tmp_path / "bg.png"
    bg.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8ffff3f0005fe02fea7d6a4b00000000049454e44ae426082"
        )
    )
    slides = list(deck.slides)
    slides[1] = slides[1].model_copy(update={"background": str(bg)})
    s = _slots_with_style(deck.model_copy(update={"slides": slides}), style)
    # AI 표기는 카드가 아니라 게시 글에(S18). 어느 장에 AI 그림이 있는지는 has_ai_image가 본다
    assert not any("ai-generated" in x["labels"] for x in s)


@pytest.mark.parametrize("style", ["photo", "illust"])
def test_slides_with_background_photo_skip_code_decoration(style: str, tmp_path: Path) -> None:
    """배경 사진이 있는 장은 장식(언덕·캐릭터 등)을 그리지 않는다: 아래 띠의 흰 페이지 번호가 밝은 언덕에 묻히던 문제."""
    import re

    bg = tmp_path / "bg.png"
    bg.write_bytes(b"png")  # 존재만 확인한다(그리지 않음)
    deck = load("info")
    slides = [s.model_copy(update={"background": str(bg)}) for s in deck.slides]
    html = build_html(deck.model_copy(update={"slides": slides}), style)
    assert 'class="deco"' not in html
    assert "mascot-stand" not in html and "mascot-wave" not in html
    assert len(re.findall(r'class="slide [^"]*has-bg', html)) == len(slides)


@pytest.mark.parametrize("style", STYLES)
def test_text_stays_html_and_no_cta_bar(style: str) -> None:
    deck = load("list")
    html = build_html(deck, style)
    for s in deck.slides:
        fields = ("title", "subtitle", "heading", "cta")
        for text in (*(getattr(s, f, None) for f in fields), *(getattr(s, "bullets", None) or [])):
            if text:
                assert text in html  # 글자는 HTML 글자로(이미지에 굽지 않음)
    assert "<img" not in html.replace('class="logo"', "")  # 로고 말고는 이미지 없음
    assert "더 알아보기" not in html and "cta-bar" not in html  # CTA 바는 플랫폼 기능


def test_clean_is_unchanged_default_and_has_no_decoration() -> None:
    deck = load("compare")
    html = build_html(deck)
    assert html == build_html(deck, "clean")
    assert 'class="deco"' not in html and "photo.css" not in html and "illust.css" not in html
    assert '<body class="style-clean">' in html


def test_style_specific_parts() -> None:
    deck = load("list")
    photo, illust = build_html(deck, "photo"), build_html(deck, "illust")
    assert (
        "photo.css" in photo and '<span class="hl">' in photo and '<svg class="mascot' not in photo
    )
    # illust: 표지 캐릭터 1, 마무리 손 흔드는 캐릭터 1. 캐릭터도 가상인물이라 배지가 있는 이 두 장에만(ADR 0043)
    assert illust.count("mascot-stand") == 1 and illust.count("mascot-wave") == 1
    assert "mascot-mini" not in illust
    # editorial(S10·S14): 사진이 없으면 패널·팔로우 카드 없이 종이색 카드, 형광펜 상자도 없다.
    # 코드로 그린 캐릭터·소품은 더 이상 얹지 않는다(S14: 그림은 사람이 고른 사진·스티커뿐)
    editorial = build_html(deck, "editorial")
    assert "editorial.css" in editorial and '<span class="hl">' not in editorial
    assert "kawaii" not in editorial and "prop-icon" not in editorial and "<svg" not in editorial
    assert "caption-panel" not in editorial and "follow-card" not in editorial


def test_unknown_style_is_rejected() -> None:
    with pytest.raises(ValueError, match="clean"):
        build_html(load("info"), "neon")
    with pytest.raises(ValueError):
        _ = HtmlCardRenderer(style="neon").version


def test_style_source_is_read_at_render_time() -> None:
    """함수로 넘기면 바뀐 설정(대시보드 저장)이 다음 렌더와 캐시 지문에 바로 반영된다."""
    current = {"style": "clean"}
    r = HtmlCardRenderer(style=lambda: current["style"])
    v = template_version()
    assert (r.style, r.version) == ("clean", f"{v}:clean")
    current["style"] = "illust"
    # 일러스트형은 캐릭터 그림까지 지문에 들어간다(C17): 캐릭터를 바꾸면 다시 그린다
    assert r.style == "illust" and r.version == f"{v}:illust:{r.character.fingerprint}"


@pytest.mark.parametrize("primary", ["#FFD84D", "#1E3A5F", "#F59E0B", "#22C55E", "#FFFFFF"])
def test_style_colors_keep_text_readable(primary: str) -> None:
    """밝은 브랜드 색이어도 photo 배경(흰 글자)·illust 배경(진한 글자) 대비가 유지된다."""
    from nsm.render.html_renderer import _contrast, _rgb, palette

    c = palette(primary, "#FF6B6B")
    white, ink = _rgb("#FFFFFF"), _rgb(c["ink"])
    assert _contrast(_rgb(c["photo_dark"]), white) >= 12
    assert _contrast(_rgb(c["photo_mid"]), white) >= 7
    assert _contrast(_rgb(c["photo_glow"]), white) >= 4.5
    assert _contrast(_rgb(c["illust_bg"]), ink) >= 12
    for bg, fg in (("photo_ad_bg", "photo_ad_fg"), ("illust_ad_bg", "illust_ad_fg")):
        assert _contrast(_rgb(c[bg]), _rgb(c[fg])) >= 4.5  # 광고 배지 글자


@pytest.mark.parametrize("with_bg", [False, True], ids=["code-bg", "photo-bg"])
@pytest.mark.parametrize("style", STYLES)
def test_text_on_filled_boxes_is_readable(style: str, with_bg: bool, tmp_path: Path) -> None:
    """번호 카드·비교 칸·말풍선처럼 바탕이 칠해진 상자 안 글자는 그 바탕과 대비 4.5 이상.

    illust에서 배경 사진이 있는 장의 번호 카드가 흰 바탕에 흰 글자로 그려지던 문제(S8 리뷰 중 발견).
    """
    from playwright.sync_api import sync_playwright

    from nsm.render.html_renderer import _contrast

    bg = tmp_path / "bg.png"
    bg.write_bytes(b"png")
    pages = []
    for template in TEMPLATES:
        deck = load(template)
        if with_bg:
            deck = deck.model_copy(
                update={
                    "slides": [s.model_copy(update={"background": str(bg)}) for s in deck.slides]
                }
            )
        page = tmp_path / f"{template}.html"
        page.write_text(build_html(deck, style), encoding="utf-8")
        pages.append(page)
    bad: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            pg = browser.new_page(viewport={"width": 1080, "height": 1350})
            for page in pages:
                pg.goto(page.resolve().as_uri(), wait_until="load")
                pairs = pg.evaluate(
                    r"""() => {
                      // color-mix()는 'color(srgb r g b)'(0~1)로 계산된다 → 0~255로 맞춘다
                      const rgba = (c) => { const n = (c.match(/[\d.]+/g) || []).map(Number);
                        return c.startsWith('color(') ? [n[0] * 255, n[1] * 255, n[2] * 255, n[3] === undefined ? 1 : n[3]] : n; };
                      const out = [];
                      for (const box of document.querySelectorAll('.content li, .content .col, .content .subtitle, .content .text, .content .cta')) {
                        const b = rgba(getComputedStyle(box).backgroundColor);
                        if (b.length < 3 || (b.length === 4 && b[3] < 0.95)) continue;  // 칠하지 않은 상자
                        for (const el of [box, ...box.querySelectorAll('h3, li')]) {
                          out.push([box.className + ' ' + el.tagName + ' ' + el.textContent.trim().slice(0, 12),
                                    rgba(getComputedStyle(el).color).slice(0, 3), b.slice(0, 3)]);
                        }
                      }
                      return out;
                    }"""
                )
                for name, fg, bgc in pairs:
                    c = _contrast(tuple(v / 255 for v in fg), tuple(v / 255 for v in bgc))
                    if c < 4.5:
                        bad.append(f"{page.stem} {name}: {c:.1f}")
        finally:
            browser.close()
    assert bad == []


def test_render_sample_uses_given_style_and_keeps_last_overflow(tmp_path: Path) -> None:
    """설정 화면 견본 렌더는 지정한 스타일로 그리고, 게시 렌더가 읽는 last_overflow를 바꾸지 않는다."""
    r = HtmlCardRenderer(style="clean")
    try:
        r.last_overflow = [7]
        paths = r.render_sample(load("list"), tmp_path, "illust")
        assert len(paths) == 4 and png_size(paths[0]) == (1080, 1350)
        assert r.last_overflow == [7] and r.style == "clean"
        with pytest.raises(ValueError):
            r.render_sample(load("list"), tmp_path, "neon")
    finally:
        r.close()
