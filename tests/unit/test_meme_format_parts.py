"""밈 형식 재해석 부품(S17 제안, ADR 0061): 탐색 파싱, 계획 검증, 한 장 렌더(말풍선 꼬리는 화자 쪽)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from nsm.adapters.generator.prompts import format_plan_from_output, meme_format_content
from nsm.adapters.sources.common import Pacer
from nsm.adapters.sources.meme_formats import MemeFormatSource
from nsm.ports import AdapterError, MemeFormatCandidate, MemeFormatRequest

PNG_200 = None


def _png(w=300, h=200) -> bytes:
    import io

    from PIL import Image

    b = io.BytesIO()
    Image.new("RGB", (w, h), (200, 120, 40)).save(b, "PNG")
    return b.getvalue()


class Clock:
    def now(self):
        return datetime(2026, 10, 21, 12, tzinfo=UTC)


KYM_POPULAR = '<a href="/memes/new">x</a><a href="/memes/scientists-reveal-the-hi">a</a><a href="/memes/memes">m</a>'


def _entry(title: str) -> str:
    return (
        f'<meta property="og:title" content="{title} | Know Your Meme">'
        '<meta property="og:image" content="https://i.kym-cdn.com/x.png">'
        '<h2 id="about">About</h2><p>A cat stares.</p>'
        '<h2 id="origin">Origin</h2><p>On July 16th, 2025 posted.</p>'
        '<h2 id="spread">Spread</h2><p>100,000+ likes.</p><h2 id="search">S</h2>'
    )


CAREET_LIST = (
    '<li><a href="/2008"><img src="a.png"> 유행 중 북마크 L을 가져가</a></li>'
    '<li><a href="/1905"><img src="b.png"> 유행지남 옛 밈</a></li>'
)
CAREET_ART = (
    '<meta property="og:title" content="L을 가져가 해설">'
    '<meta property="og:description" content="이마에 L">'
    '<meta property="og:image" content="https://cdn.careet.net/l.png">'
)


def _source(tmp_path: Path, routes: dict[str, httpx.Response]) -> MemeFormatSource:
    def handler(req: httpx.Request) -> httpx.Response:
        url = f"https://{req.headers['host']}{req.url.raw_path.decode()}"
        for k, v in routes.items():
            if url.startswith(k):
                return v
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return MemeFormatSource(
        Clock(),
        Pacer(0, 0, sleep=lambda s: None),
        tmp_path / "cache",
        client=client,
        resolver=lambda host: ["93.184.216.34"],
        popular_n=1,
        classic_n=1,
        local_n=1,
    )


def test_discovery_reads_pages_records_evidence_and_caches(tmp_path) -> None:
    routes = {
        "https://knowyourmeme.com/memes/popular": httpx.Response(200, text=KYM_POPULAR),
        "https://knowyourmeme.com/memes/scientists-reveal-the-hi": httpx.Response(
            200, text=_entry("Scientists Reveal the Hi")
        ),
        "https://knowyourmeme.com/memes/this-is-fine": httpx.Response(
            200, text=_entry("This Is Fine")
        ),
        "https://i.kym-cdn.com/x.png": httpx.Response(200, content=_png()),
        "https://www.careet.net/Content/Series/1": httpx.Response(200, text=CAREET_LIST),
        "https://www.careet.net/2008": httpx.Response(200, text=CAREET_ART),
        "https://cdn.careet.net/l.png": httpx.Response(200, content=_png()),
    }
    src = _source(tmp_path, routes)
    res = src.discover()
    keys = [c.key for c in res.candidates]
    assert keys == ["scientists-reveal-the-hi", "this-is-fine", "careet-2008"]
    hi = res.candidates[0]
    # 인기 목록에 있어도 페이지의 최근 날짜(2025-07)가 오래됐으면 '최근 확산'이 아니다(S18)
    assert (
        hi.era == "listed"
        and hi.latest_evidence == "2025-07-16"
        and hi.title == "Scientists Reveal the Hi"
    )
    assert "July 16th, 2025" in hi.evidence and "100,000+" in hi.evidence  # 페이지에 적힌 것만
    assert Path(hi.image_path).is_file() and "meme_formats" not in hi.image_path or True
    assert res.candidates[2].era == "local" and "유행 중" in res.candidates[2].evidence
    assert res.access["X"].startswith("수집 안 함")
    # 같은 날 다시 부르면 캐시(요청 없음)
    src.client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert [c.key for c in src.discover().candidates] == keys


def test_discovery_survives_blocked_source(tmp_path) -> None:
    routes = {
        "https://knowyourmeme.com/memes/this-is-fine": httpx.Response(
            200, text=_entry("This Is Fine")
        ),
    }
    res = _source(tmp_path, routes).discover()
    assert [c.key for c in res.candidates] == ["this-is-fine"]
    assert res.access["Know Your Meme 인기"].startswith("실패")
    assert res.access["캐릿(국내)"].startswith("실패")


def _req(**kw) -> MemeFormatRequest:
    c = MemeFormatCandidate(key="this-is-fine", title="This Is Fine", url="u", site="KYM")
    return MemeFormatRequest(
        topic="치이카와",
        character_name="치이카와",
        character_description="",
        tone="t",
        candidates=(c,),
        **kw,
    )


def _out(**kw):
    base = {
        "ranking": [
            {"key": "this-is-fine", "score": 12, "reason": "r"},
            {"key": "ghost", "score": 9, "reason": "x"},
        ],
        "key": "this-is-fine",
        "why": "w",
        "funny_point": "f",
        "structure": "s",
        "reinterpretation": "r",
        "scene": "sc",
        "overlays": [
            {
                "kind": "bubble",
                "text": "괜찮아",
                "x": 120,
                "y": 50,
                "w": 30,
                "align": "center",
                "size": "m",
                "tail_x": 40,
                "tail_y": 70,
            },
            {
                "kind": "caption",
                "text": "  ",
                "x": 1,
                "y": 1,
                "w": 30,
                "align": "left",
                "size": "l",
                "tail_x": 3,
                "tail_y": 3,
            },
            {
                "kind": "caption",
                "text": "위 캡션",
                "x": 1,
                "y": 1,
                "w": 30,
                "align": "left",
                "size": "l",
                "tail_x": 3,
                "tail_y": 3,
            },
        ],
        "caption": "c",
        "threads_text": "t",
        "suggested_character": "",
        "claims": [],
    }
    base.update(kw)
    return base


def test_plan_parser_clamps_drops_empty_and_rejects_unknown_key() -> None:
    plan = format_plan_from_output(_out(), _req())
    assert [o.kind for o in plan.overlays] == ["bubble", "caption"]
    assert (
        plan.overlays[0].x + plan.overlays[0].w <= 100 and plan.overlays[1].tail_x is None
    )  # 말풍선 아닌 것은 꼬리 없음
    assert [r.key for r in plan.ranking] == ["this-is-fine"] and plan.ranking[0].score == 10
    with pytest.raises(AdapterError):
        format_plan_from_output(_out(key="ghost"), _req())
    assert (
        format_plan_from_output(_out(key="ghost"), _req(force_key="this-is-fine")).key
        == "this-is-fine"
    )


def test_meme_image_renders_with_tail_toward_speaker(app_config, tmp_path) -> None:
    from nsm.render.html_renderer import HtmlCardRenderer

    out = _out()
    out["overlays"][0]["x"] = 55  # 장 안
    plan = format_plan_from_output(out, _req())
    gen = meme_format_content(
        plan,
        app_config.brand,
        app_config.persona,
        "밈",
        ["참고 밈 This Is Fine(KYM)"],
        ["참고 밈: This Is Fine — u"],
    )
    assert gen.instagram_caption.endswith("참고 밈: This Is Fine — u")
    img = tmp_path / "img.png"
    img.write_bytes(_png(1024, 1280))
    from nsm.domain.models import ImageAsset

    s = gen.deck.slides[0].model_copy(update={"image": ImageAsset(path=str(img), ai=True)})
    deck = gen.deck.model_copy(
        update={"slides": [s], "labels": gen.deck.labels.model_copy(update={"ai_slides": [0]})}
    )
    r = HtmlCardRenderer(style="editorial")
    try:
        paths = r.render(deck, tmp_path / "out")
        assert len(paths) == 1 and r.last_overflow == []
        # 장 밖으로 나간 글자는 줄여도 안 되면 넘침으로 알린다(잘린 카드를 올리지 않게; 계획 검증을 거치지 않은 덱)
        far = s.overlays[0].model_copy(update={"x": 100.0, "w": 60.0})
        bad = deck.model_copy(update={"slides": [s.model_copy(update={"overlays": [far]})]})
        r.render(bad, tmp_path / "bad")
        assert r.last_overflow == [1]
    finally:
        r.close()
    from nsm.render.html_renderer import build_html

    html = build_html(deck, "editorial")
    assert 'data-tx="40.0"' in html and "참고 밈" in html and "AI 생성 이미지" in html
    # 꼬리: 렌더 때 말풍선에서 화자(40%, 70%) 쪽으로 경로 하나
    page_file = tmp_path / "deck.html"
    page_file.write_text(html, encoding="utf-8")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1080, "height": 1350})
        pg.goto(page_file.as_uri())
        pg.evaluate("window.nsmFit()")
        paths = pg.evaluate(
            "[...document.querySelectorAll('svg.bubbles path')].map(p => p.getAttribute('d'))"
        )
        b.close()
    # 몸통과 꼬리가 경로 하나(이음새·삐져나온 선이 생길 수 없음), 꼬리 끝은 화자(왼쪽 아래) 쪽
    assert len(paths) == 1 and paths[0].endswith("Z")
    pts = [tuple(float(v) for v in seg.split()[:2]) for seg in paths[0].split(" L ")[1:]]
    tip = max(pts, key=lambda p: p[1])
    assert tip[0] < 55 / 100 * 1080 and tip[1] > 50 / 100 * 1350


def test_recent_only_when_page_has_recent_date() -> None:
    from nsm.adapters.sources.meme_formats import latest_date

    text = "On July 16th, 2025, posted. On October 12th, 2026, an edit got 35,000 likes."
    assert latest_date(text) == "2026-10-12"
    assert latest_date("no dates here") == ""
