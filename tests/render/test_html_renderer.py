"""경계 3: 템플릿 렌더 테스트 (S3 #19, docs/contracts.md 5·10절).

실제 Playwright 크로미움으로 그린다(기본 실행에 포함, CI에서 크로미움 설치).
"""

from __future__ import annotations

import json
import re
import struct
from html.parser import HTMLParser
from pathlib import Path

import pytest

from nsm.domain.models import CardDeck
from nsm.render.html_renderer import HtmlCardRenderer, build_html, palette

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "decks"


def load(template: str) -> CardDeck:
    return CardDeck.model_validate_json((FIXTURES / f"{template}.json").read_text("utf-8"))


def png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", head[16:24])


class Slots(HTMLParser):
    """슬라이드(section)별로 표기 슬롯 속성을 모은다."""

    def __init__(self) -> None:
        super().__init__()
        self.slides: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "section" and "slide" in (a.get("class") or ""):
            self.slides.append(
                {
                    "kind": a.get("data-kind"),
                    "labels": [],
                    "page": 0,
                    "sources": 0,
                    "style": a.get("style") or "",
                }
            )
            return
        if not self.slides:
            return
        cur = self.slides[-1]
        if "data-label" in a:
            cur["labels"].append(a["data-label"])
        if "data-page" in a:
            cur["page"] += 1
        if "data-sources" in a:
            cur["sources"] += 1


def slots(deck: CardDeck) -> list[dict]:
    p = Slots()
    p.feed(build_html(deck))
    return p.slides


@pytest.fixture(scope="module")
def renderer() -> HtmlCardRenderer:
    return HtmlCardRenderer()


@pytest.mark.parametrize("template", ["info", "list", "compare"])
def test_fixture_decks_render_1080x1350(renderer, template: str, tmp_path: Path) -> None:
    deck = load(template)
    paths = renderer.render(deck, tmp_path)
    assert [p.name for p in paths] == [f"slide-{i:02d}.png" for i in range(1, len(deck.slides) + 1)]
    assert all(png_size(p) == (1080, 1350) for p in paths)
    assert renderer.last_overflow == []


@pytest.mark.parametrize("template", ["info", "list", "compare"])
def test_label_slots_present(template: str) -> None:
    deck = load(template)
    s = slots(deck)
    assert len(s) == len(deck.slides)
    assert all(x["labels"].count("virtual-person") == 1 for x in s)  # 모든 장에 가상인물 배지
    assert all(x["page"] == 1 for x in s)
    assert [x["sources"] for x in s] == [0] * (len(s) - 1) + [1]  # 출처는 마무리 장에만
    ad = [x for x in s if "ad" in x["labels"]]
    if deck.labels.ad:
        assert len(ad) == 1 and ad[0]["kind"] == "cover"  # 광고 배지는 표지에
    else:
        assert ad == []


def test_ad_false_has_no_ad_badge() -> None:
    deck = load("list")
    deck = deck.model_copy(update={"labels": deck.labels.model_copy(update={"ad": False})})
    assert all("ad" not in x["labels"] for x in slots(deck))
    assert 'data-label="ad"' not in build_html(deck)


def test_ai_caption_only_on_slides_with_background(tmp_path: Path) -> None:
    bg = tmp_path / "bg.png"
    bg.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8ffff3f0005fe02fea7d6a4b00000000049454e44ae426082"
        )
    )
    deck = load("info")
    slides = list(deck.slides)
    slides[0] = slides[0].model_copy(update={"background": str(bg)})
    deck = deck.model_copy(
        update={
            "slides": slides,
            "labels": deck.labels.model_copy(update={"ai_generated_image": True}),
        }
    )
    s = slots(deck)
    assert "ai-generated" in s[0]["labels"] and bg.resolve().as_uri() in s[0]["style"]
    assert all("ai-generated" not in x["labels"] for x in s[1:])


def test_missing_background_falls_back_to_brand_gradient() -> None:
    deck = load("info")
    slides = list(deck.slides)
    slides[0] = slides[0].model_copy(update={"background": "C:/nope/missing.png"})
    deck = deck.model_copy(
        update={
            "slides": slides,
            "labels": deck.labels.model_copy(update={"ai_generated_image": True}),
        }
    )
    s = slots(deck)
    assert s[0]["style"] == "" and "ai-generated" not in s[0]["labels"]


def _extreme(template: str) -> CardDeck:
    long = "아주 긴 문장이 들어와도 카드가 깨지지 않아야 합니다 그래서 일부러 길게 씁니다"
    raw = json.loads((FIXTURES / f"{template}.json").read_text("utf-8"))
    raw["slides"][0]["title"] = (
        "두 줄을 훌쩍 넘기는 아주아주 긴 표지 제목이 들어왔을 때도 글자가 줄어들어 들어가야 한다"
    )
    raw["slides"][0]["subtitle"] = long
    for s in raw["slides"][1:-1]:
        if s["kind"] == "body":
            s["heading"] = "소제목도 길게 들어오는 경우를 대비한 테스트 문구"
            s["body"] = long * 2
            s["bullets"] = [long] * 3
        elif s["kind"] == "list":
            s["heading"] = "소제목도 길게 들어오는 경우를 대비한 테스트"
            s["bullets"] = [long] * 5
        elif s["kind"] == "compare":
            s["left"]["points"] = [long] * 4
            s["right"]["points"] = [long] * 4
            s["left"]["title"] = s["right"]["title"] = "꽤 긴 비교 대상 이름"
    raw["slides"][-1]["cta"] = long
    raw["slides"][-1]["sources"] = [
        f"https://example.com/very/long/path/to/source/article-{i}?ref=abc" for i in range(6)
    ]
    raw["labels"]["ad"] = True
    return CardDeck.model_validate(raw)


@pytest.mark.parametrize("template", ["info", "list", "compare"])
def test_extreme_inputs_do_not_overflow(renderer, template: str, tmp_path: Path) -> None:
    deck = _extreme(template)
    paths = renderer.render(deck, tmp_path)
    assert len(paths) == len(deck.slides)
    assert renderer.last_overflow == [], f"넘친 슬라이드: {renderer.last_overflow}"


def test_render_works_while_playwright_runs_on_same_thread(renderer, tmp_path: Path) -> None:
    """수집 드라이버(Playwright)가 같은 작업 스레드에 떠 있어도 렌더가 된다(전용 스레드에서 렌더)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright():
        paths = renderer.render(load("compare"), tmp_path)
    assert len(paths) == 3


def test_long_sources_wrap_not_ellipsis() -> None:
    html = build_html(_extreme("info"))
    assert (
        "text-overflow"
        not in (Path(__file__).resolve().parents[2] / "nsm/templates/cards/base.css")
        .read_text("utf-8")
        .split(".outro .sources li")[1]
        .split("}")[0]
    )
    assert html.count('data-fit="16"') == 6  # 출처마다 글자 맞춤 대상


def test_no_html_left_in_output_and_overflow_reset(renderer, tmp_path: Path) -> None:
    renderer.last_overflow = [99]
    renderer.render(load("info"), tmp_path)
    assert renderer.last_overflow == []
    assert not list(tmp_path.glob("*.html"))  # 로컬 경로가 담긴 HTML을 남기지 않음


def test_ad_badge_stays_visible_when_accent_close_to_primary() -> None:
    c = palette("#1E40AF", "#1E3A8A")
    assert c["ad_bg"] != "#1E3A8A"
    ok = palette("#1E3A5F", "#F2A541")
    assert ok["ad_bg"] == "#F2A541"


def test_mid_tone_primary_text_readable_across_gradient() -> None:
    from nsm.render.html_renderer import _contrast, _rgb

    c = palette("#F59E0B", "#1E3A5F")
    fg = _rgb(c["on_primary"])
    assert min(_contrast(fg, _rgb(c["primary"])), _contrast(fg, _rgb(c["primary_deep"]))) >= 3.0


def test_palette_contrast() -> None:
    dark = palette("#1E3A5F", "#F2A541")
    assert dark["on_primary"] == "#FFFFFF" and dark["on_accent"] == "#121419"
    light = palette("#FFE680", "#FFFFFF")
    assert light["on_primary"] == "#121419"
    assert light["primary_ink"] != "#FFE680"  # 밝은 면 위에서는 어둡게 바꿔 읽히게


def test_get_renderer_prefers_html_and_falls_back(monkeypatch) -> None:
    import importlib.util

    from nsm import render
    from nsm.render.placeholder import PlaceholderRenderer

    assert isinstance(render.get_renderer(), HtmlCardRenderer)
    import nsm.render.html_renderer as hr

    monkeypatch.setattr(hr, "chromium_available", lambda: False)  # 크로미움 미설치
    assert isinstance(render.get_renderer(), PlaceholderRenderer)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)  # playwright 없음
    assert isinstance(render.get_renderer(), PlaceholderRenderer)


@pytest.mark.parametrize("template", ["info", "list", "compare"])
def test_virtual_person_badge_sits_next_to_persona_name(template: str) -> None:
    # 심사지침 Ⅴ.5: 사진에는 "가상인물과 근접한 위치"에 표시. 카드에서 인물은 페르소나 이름이다(ADR 0019)
    deck = load(template)
    html = build_html(deck)
    pattern = (
        r'<div class="ident">\s*<div class="who">[^<]*(?:<img[^>]*>)?[^<]*</div>'
        r'\s*<span class="badge" data-label="virtual-person">가상인물</span>'
    )
    assert len(re.findall(pattern, html)) == len(deck.slides)


def test_renderer_version_tracks_template_files() -> None:
    from nsm.render.html_renderer import template_version

    v = template_version()
    assert v == template_version() and len(v) == 16
    assert HtmlCardRenderer().version == f"{v}:clean"


# ---------------------------------------------------------------- 스레드 썸네일 가독성 (S-γ)

# 스레드 피드는 카드 첫 장을 224×280 CSS px로 보여 준다(2026-10-14 threads.com 실측, 원본 1080의 약 21%).
THUMB_SCALE = 224 / 1080
MIN_THUMB_PX = {"title": 18.0, "subtitle": 9.9, "who": 7.5, "badge": 7.0}


def _cover_sizes(deck: CardDeck, tmp_path: Path, style: str = "clean") -> dict[str, float]:
    """글자 맞춤(nsmFit)이 끝난 뒤 표지 글자 크기(px)."""
    from playwright.sync_api import sync_playwright

    page_path = tmp_path / "deck.html"
    page_path.write_text(build_html(deck, style), encoding="utf-8")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1080, "height": 1350})
            page.goto(page_path.resolve().as_uri(), wait_until="load")
            page.evaluate("document.fonts.ready.then(() => true)")
            page.evaluate("window.nsmFit()")
            return page.evaluate(
                """() => {
                  const s = document.querySelector('section.slide[data-kind="cover"]');
                  const px = (sel) => { const e = s.querySelector(sel); return e ? parseFloat(getComputedStyle(e).fontSize) : 0; };
                  return { title: px('h1'), subtitle: px('.subtitle'), who: px('.who'), badge: px('[data-label="virtual-person"]'),
                           titleLines: Math.round(s.querySelector('h1').getBoundingClientRect().height / parseFloat(getComputedStyle(s.querySelector('h1')).lineHeight)) };
                }"""
            )
        finally:
            browser.close()


@pytest.mark.parametrize("style", ["clean", "photo", "illust"])
@pytest.mark.parametrize("extreme", [False, True], ids=["normal", "extreme"])
@pytest.mark.parametrize("template", ["info", "list", "compare"])
def test_cover_readable_at_threads_thumbnail(
    template: str, extreme: bool, style: str, tmp_path: Path
) -> None:
    """표지 제목·부제·이름·가상인물 배지가 피드 썸네일 크기에서도 최소 크기 이상이다(긴 글로 줄어든 뒤에도).

    S8: 세 스타일 모두 같은 최소값을 지킨다(ADR 0035·0036).
    """
    deck = _extreme(template) if extreme else load(template)
    sizes = _cover_sizes(deck, tmp_path, style)
    for key, minimum in MIN_THUMB_PX.items():
        assert sizes[key] * THUMB_SCALE >= minimum, (
            f"{key} {sizes[key]}px → 썸네일 {sizes[key] * THUMB_SCALE:.1f}px"
        )


def test_cover_title_blank_lines_collapse(tmp_path: Path) -> None:
    """2026-10-13 실제 원고: 제목 "극장판 흥행에\n\n게임도 역주행"이 줄 사이가 크게 벌어져 그려졌다."""
    raw = json.loads((FIXTURES / "info.json").read_text("utf-8"))
    raw["slides"][0]["title"] = "극장판 흥행에\n\n게임도 역주행"
    deck = CardDeck.model_validate(raw)
    assert "극장판 흥행에\n게임도 역주행</h1>" in build_html(deck)
    assert _cover_sizes(deck, tmp_path)["titleLines"] == 2
