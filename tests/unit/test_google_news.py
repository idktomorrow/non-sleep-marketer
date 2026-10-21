"""구글 뉴스 RSS 소스(C30, ADR 0050): 파싱·제목 중복·발행 시각·주소 풀기·본문 읽기. 네트워크 없이 가짜 HTTP로."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from nsm.adapters.sources.google_news import (
    GoogleNewsSource,
    decode_html,
    html_to_text,
    parse_news_rss,
    resolve_article_url,
)


def test_parse_rss_drops_source_suffix_dupes_and_bad_links(fixtures_dir: Path, clock) -> None:
    xml = (fixtures_dir / "sources" / "gnews_chiikawa.xml").read_text("utf-8")
    items = parse_news_rss(xml, clock, 10)
    titles = [i.title for i in items]
    # 같은 보도자료(제목 낱말 겹침 큼)는 하나만, 구글 id 없는 링크는 버린다, " - 매체" 꼬리는 뗀다
    assert titles == [
        "200만 돌파 '극장판 치이카와'…입소문 타고 4DX 상영 확정",
        "일본여행 가면 사온 '치이카와' 굿즈, 이제 편의점에서 산다",
        "4DX 특별 상영관",
    ]
    first = items[0]
    assert first.source == "google_news" and first.text.endswith("(일간스포츠)")
    assert first.published_at == datetime(2026, 10, 21, 8, 47, tzinfo=UTC)
    assert items[2].published_at is None  # 깨진 날짜는 비워 둔다
    assert first.url.startswith("https://news.google.com/rss/articles/")
    # 해시는 제목 낱말 집합 기준: 다시 읽어도, 다른 매체의 같은 보도자료여도 같다(실행을 건너서도 한 번만)
    assert parse_news_rss(xml, clock, 1)[0].url_hash == first.url_hash
    assert len(parse_news_rss(xml, clock, 2)) == 2
    copy = xml.replace("CBMiW0FVX3lxTFBneklEaWxwZ0VIdXFVRQAAA", "CBMiOTHER")
    assert parse_news_rss(copy, clock, 1)[0].url_hash == first.url_hash
    # 오래된 기사는 기간 밖이면 버린다(검색 연산자가 안 먹었을 때 대비)
    old = xml.replace("Wed, 21 Oct 2026 08:47:00 GMT", "Wed, 01 Jan 2025 08:47:00 GMT")
    assert "200만" not in [i.title[:4] for i in parse_news_rss(old, clock, 10, max_age_days=7)]
    # 오래된 원본이 빠지면 같은 보도자료의 다른 매체 사본이 대신 들어온다(날짜가 최근이므로)
    assert parse_news_rss(old, clock, 10, max_age_days=7)[0].title.startswith(
        "'극장판 치이카와' 200만"
    )


def test_similar_titles_need_four_words_and_source_tail_is_dropped(clock) -> None:
    from nsm.adapters.sources.google_news import _similar, _strip_source, _tokens

    # 짧은 제목은 낱말 하나 차이가 다른 소식일 수 있다
    assert not _similar(_tokens("치이카와 굿즈 출시"), _tokens("치이카와 굿즈"))
    assert _similar(_tokens("치이카와 굿즈 출시"), _tokens("치이카와 굿즈 출시"))
    assert _similar(
        _tokens("200만 돌파 극장판 치이카와 4DX 상영 확정"),
        _tokens("극장판 치이카와 200만 돌파 4DX 상영 확정 발표"),
    )
    assert _strip_source("제목 - 중앙일보", "중앙일보") == "제목"
    assert _strip_source("제목 - 중앙일보", "") == "제목"  # 매체 칸이 비어도 짧은 꼬리는 뗀다
    assert _strip_source(
        "A - B - 아주 긴 꼬리는 제목의 일부일 수 있어서 그대로 둔다 정말로", ""
    ) == ("A - B - 아주 긴 꼬리는 제목의 일부일 수 있어서 그대로 둔다 정말로")


def test_collect_splits_limit_over_topics_and_survives_topic_failure(
    fixtures_dir: Path, clock
) -> None:
    xml = (fixtures_dir / "sources" / "gnews_chiikawa.xml").read_text("utf-8")
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        if "%EC%8B%A4%ED%8C%A8" in url:  # "실패"
            raise httpx.ConnectError("down")
        return xml

    src = GoogleNewsSource(clock, fetch=fetch, sleep=lambda s: None, recent="1w")
    items = src.collect(["치이카와", "실패", "먼작귀"], 6)
    assert len(calls) == 3 and all("news.google.com/rss/search?q=" in c for c in calls)
    assert all("when%3A7d" in c for c in calls)  # 네이버의 검색 기간과 같은 뜻(ADR 0032)
    assert src._client is None  # fetch만 주입하면 HTTP 클라이언트를 열지 않는다
    src.close()
    # 주제당 2건씩. 세 번째 주제는 같은 RSS라 앞의 두 제목은 중복으로 빠지고 남은 한 건만 더해진다
    assert len(items) == 3 and [i.title for i in items][-1] == "4DX 특별 상영관"
    assert src.collect([], 5) == []


def _article_page(sig: str = "SIG", ts: str = "123") -> str:
    return f'<html><body><c-wiz data-n-a-sg="{sig}" data-n-a-ts="{ts}"></c-wiz></body></html>'


def _batch_response(url: str) -> str:
    inner = json.dumps(["garturlres", url, 1])
    return ")]}'\n\n" + json.dumps([["wrb.fr", "Fbv4je", inner, None, None, None, "generic"]])


def test_resolve_article_url_uses_signature_and_batchexecute() -> None:
    posted: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.startswith("/articles/"):
            return httpx.Response(200, text=_article_page())
        if req.url.path.endswith("/batchexecute"):
            posted.append(dict(httpx.QueryParams(req.content.decode())))
            return httpx.Response(200, text=_batch_response("https://www.joongang.co.kr/article/1"))
        return httpx.Response(404)

    c = httpx.Client(transport=httpx.MockTransport(handler))
    url = "https://news.google.com/rss/articles/CBMiABC?oc=5"
    assert resolve_article_url(url, c) == "https://www.joongang.co.kr/article/1"
    assert "CBMiABC" in posted[0]["f.req"] and "SIG" in posted[0]["f.req"]
    assert resolve_article_url("https://example.com/x", c) is None
    paced: list[int] = []
    assert resolve_article_url(url, c, pace=lambda: paced.append(1)) and len(paced) == 2


def test_resolve_returns_none_when_page_or_batch_is_odd() -> None:
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html/>")))
    assert resolve_article_url("https://news.google.com/rss/articles/CBMiX?oc=5", c) is None

    def bad_batch(req: httpx.Request) -> httpx.Response:
        if req.url.path.startswith("/articles/"):
            return httpx.Response(200, text=_article_page())
        return httpx.Response(200, text=")]}'\n\nnot json")

    c = httpx.Client(transport=httpx.MockTransport(bad_batch))
    assert resolve_article_url("https://news.google.com/rss/articles/CBMiX?oc=5", c) is None


def test_fetch_body_prefers_browser_reader_else_http(clock) -> None:
    long = "<html><script>x()</script><p>" + "치이카와 굿즈 소식 본문 " * 30 + "</p></html>"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.startswith("/articles/"):
            return httpx.Response(200, text=_article_page())
        if req.url.path.endswith("/batchexecute"):
            return httpx.Response(200, text=_batch_response("https://paper.example/a/1"))
        if req.url.host == "paper.example":
            return httpx.Response(200, text=long)
        return httpx.Response(404)

    c = httpx.Client(transport=httpx.MockTransport(handler))
    g = "https://news.google.com/rss/articles/CBMiABC?oc=5"
    via_http = GoogleNewsSource(clock, client=c, sleep=lambda s: None).fetch_body(g)
    assert via_http and via_http.startswith("치이카와 굿즈 소식 본문") and "x()" not in via_http
    seen: list[str] = []
    via_browser = GoogleNewsSource(
        clock,
        client=c,
        body_fetcher=lambda u: seen.append(u) or "브라우저 본문",
        sleep=lambda s: None,
    ).fetch_body(g)
    assert via_browser == "브라우저 본문" and seen == ["https://paper.example/a/1"]
    # 짧은 본문은 None(검색 요약문이 더 낫다)
    short = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: (
                httpx.Response(200, text=_article_page())
                if r.url.path.startswith("/articles/")
                else httpx.Response(200, text=_batch_response("https://paper.example/a/2"))
                if r.url.path.endswith("/batchexecute")
                else httpx.Response(200, text="<p>짧음</p>")
            )
        )
    )
    assert GoogleNewsSource(clock, client=short, sleep=lambda s: None).fetch_body(g) is None
    # 본문 추출: <article> 안의 <p>만, 메뉴·푸터 제외, HTML 엔티티 풀기
    page = (
        "<nav>로그인 회원가입</nav><article><p>첫 문단 &amp; 둘째</p><p>셋째</p></article>"
        "<footer>회사 소개</footer>"
    )
    assert html_to_text(page) == "첫 문단 & 둘째 셋째"
    assert html_to_text("<div>안녕</div><div>하세요</div>") == "안녕 하세요"
    # EUC-KR 사이트(헤더에 charset 없음)도 깨지지 않는다
    body = "<meta charset=euc-kr><p>치이카와 소식</p>".encode("cp949")
    assert "치이카와 소식" in decode_html(body, None)
    assert "치이카와" in decode_html("치이카와".encode(), None)
