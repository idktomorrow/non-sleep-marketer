"""경계 3: 보조 캐릭터 스티커(S14 제안): 메모 상자 테두리에 걸친 그림. 사진 없이도 그리고, AI 표기는 자산별."""

from __future__ import annotations

from pathlib import Path

from nsm.domain.models import CardDeck
from nsm.render.html_renderer import HtmlCardRenderer, build_html
from tests.render.test_html_renderer import FIXTURES, png_size, slots


def deck() -> CardDeck:
    return CardDeck.model_validate_json((FIXTURES / "editorial-sticker.json").read_text("utf-8"))


def test_sticker_markup_and_ai_caption_per_slide() -> None:
    html = build_html(deck(), "editorial")
    assert html.count('class="sticker"') == 2 and "has-sticker" in html
    assert "<svg" not in html  # 코드 캐릭터 없음
    s = slots(deck())
    # AI 표기는 카드가 아니라 게시 글에(S18): ai_slides=[1, 2]가 있으면 글에 붙는다
    from nsm.domain.policy import has_ai_image

    assert not any("ai-generated" in x["labels"] for x in s) and has_ai_image(deck())


def test_missing_sticker_file_leaves_no_image() -> None:
    d = deck()
    slides = list(d.slides)
    slides[1] = slides[1].model_copy(
        update={"sticker": slides[1].sticker.model_copy(update={"path": "nope.png"})}
    )
    html = build_html(d.model_copy(update={"slides": slides}), "editorial")
    assert html.count('class="sticker"') == 1  # 없는 파일은 빈 자리, 대체 그림 없음


def test_sticker_overlaps_memo_border_and_text_is_clear(tmp_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    page = tmp_path / "deck.html"
    page.write_text(build_html(deck(), "editorial"), encoding="utf-8")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            pg = browser.new_page(viewport={"width": 1080, "height": 1350})
            pg.goto(page.resolve().as_uri(), wait_until="load")
            pg.evaluate("window.nsmFit()")
            m = pg.evaluate(
                """() => { const s = document.querySelectorAll('.slide')[1];
                  const r = e => { const b = e.getBoundingClientRect(); return [b.left, b.top, b.right, b.bottom]; };
                  return {content: r(s.querySelector('.content')), sticker: r(s.querySelector('.sticker')),
                          h2: r(s.querySelector('h2'))}; }"""
            )
        finally:
            browser.close()
    cl, ct, cr, cb = m["content"]
    sl, st, sr, sb = m["sticker"]
    hl, ht, hr, hb = m["h2"]
    assert st < ct < sb  # 테두리에 걸친다(위로 삐져나오고 아래는 상자 안)
    assert sb <= ht + 8 or sr <= hl  # 제목 글자를 가리지 않는다


def test_renders_1080x1350_without_overflow(tmp_path: Path) -> None:
    r = HtmlCardRenderer(style="editorial")
    try:
        paths = r.render(deck(), tmp_path)
        assert [png_size(p) for p in paths] == [(1080, 1350)] * 5 and r.last_overflow == []
    finally:
        r.close()
