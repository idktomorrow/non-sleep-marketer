from __future__ import annotations

import os
import random
from pathlib import Path

import pytest

from nsm.adapters.drivers.base import Link
from nsm.adapters.sources.common import Pacer, normalize_url, url_hash
from nsm.adapters.sources.google_trends import GoogleTrendsSource, parse_trends_rss
from nsm.adapters.sources.upload import UploadSource
from nsm.adapters.sources.web import CommunitySource, NaverSearchSource
from tests.fakes import FakeDriver


def _pacer() -> tuple[Pacer, list[float]]:
    slept: list[float] = []
    return Pacer(2.0, 4.0, sleep=slept.append, rng=random.Random(0)), slept


def test_normalize_url_drops_tracking_and_unifies_mobile_blog() -> None:
    a = "https://M.Blog.naver.com/user/123/?utm_source=x&b=2&a=1"
    b = "https://blog.naver.com/user/123?a=1&b=2"
    assert normalize_url(a) == normalize_url(b)
    assert url_hash(a) == url_hash(b)
    assert url_hash("https://x.com/a") != url_hash("https://x.com/b")


def test_pacer_waits_between_requests_only() -> None:
    p, slept = _pacer()
    p.wait()
    assert slept == []
    p.wait()
    p.wait()
    assert len(slept) == 2 and all(2.0 <= s <= 4.0 for s in slept)


def test_upload_source_reads_supported_files(tmp_path: Path, clock) -> None:
    (tmp_path / "제품.txt").write_text("싱글오리진 에티오피아, 2주 이내 로스팅", encoding="utf-8")
    (tmp_path / "press.md").write_text("# 보도자료\n가을 한정 원두 출시", encoding="utf-8")
    (tmp_path / "가격.csv").write_text("품목,가격\n드립백,1500\n", encoding="utf-8")
    (tmp_path / "empty.txt").write_text("   ", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"\x89PNG")
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["원두", "산미"])
    wb.active.append(["케냐", "높음"])
    wb.save(tmp_path / "표.xlsx")

    items = UploadSource(tmp_path, clock).collect([], 10)
    titles = {i.title for i in items}
    assert titles == {"제품", "press", "가격", "표"}
    assert all(i.source == "upload" and i.url.startswith("file:") for i in items)
    xlsx = next(i for i in items if i.title == "표")
    assert "케냐, 높음" in xlsx.text


def test_upload_source_rehashes_edited_file(tmp_path: Path, clock) -> None:
    f = tmp_path / "a.txt"
    f.write_text("첫 버전", encoding="utf-8")
    h1 = UploadSource(tmp_path, clock).collect([], 10)[0].url_hash
    assert UploadSource(tmp_path, clock).collect([], 10)[0].url_hash == h1
    f.write_text("두 번째 버전, 더 길다", encoding="utf-8")
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 10))
    assert UploadSource(tmp_path, clock).collect([], 10)[0].url_hash != h1


def test_upload_source_missing_folder_is_empty(tmp_path: Path, clock) -> None:
    assert UploadSource(tmp_path / "nope", clock).collect([], 10) == []


NAVER_LINKS = [
    Link(
        "핸드드립 물 온도 완벽 정리 새 창 열림",
        "https://blog.naver.com/coffee/2231",
        "핸드드립 물 온도 완벽 정리 92도가 기본...",
    ),
    Link("핸드드립 물 온도 완벽 정리", "https://m.blog.naver.com/coffee/2231", "중복(모바일 주소)"),
    Link("블로그", "https://blog.naver.com/coffee", "짧은 제목·글 주소 아님"),
    Link("광고 배너 링크입니다 클릭", "https://ad.naver.com/x", "광고"),
    Link(
        "커피 수입량 역대 최대 기록",
        "https://n.news.naver.com/mnews/article/001/0000001",
        "커피 수입량이 ...",
    ),
]


def test_naver_source_extracts_blog_and_news_with_dedupe(clock) -> None:
    driver = FakeDriver({"https://search.naver.com": NAVER_LINKS})
    pacer, slept = _pacer()
    items = NaverSearchSource(driver, clock, pacer).collect(["핸드드립"], 10)
    urls = [i.url for i in items]
    assert urls == [
        "https://blog.naver.com/coffee/2231",
        "https://n.news.naver.com/mnews/article/001/0000001",
    ]
    assert all(i.source == "naver" for i in items)
    assert items[0].title == "핸드드립 물 온도 완벽 정리"
    assert len(driver.visited) == 2  # 블로그 탭 + 뉴스 탭
    assert len(slept) == 1  # 두 요청 사이 한 번 쉼


def test_naver_source_respects_limit_and_survives_failure(clock) -> None:
    driver = FakeDriver({"https://search.naver.com": NAVER_LINKS}, fail_on="where=blog")
    items = NaverSearchSource(driver, clock, _pacer()[0]).collect(["핸드드립", "원두"], 1)
    assert len(items) == 1
    assert items[0].url.startswith("https://n.news.naver.com")


def test_community_source_keeps_same_host_links(clock) -> None:
    links = [
        Link("홈카페 드리퍼 추천 부탁드려요", "https://cafe.example.com/board/10", "질문글"),
        Link("다른 사이트로 가는 링크입니다", "https://other.com/x/1", "외부"),
    ]
    driver = FakeDriver({"https://cafe.example.com/search": links})
    src = CommunitySource("https://cafe.example.com/search?q={query}", driver, clock, _pacer()[0])
    assert src.name == "community:cafe.example.com"
    items = src.collect(["드리퍼"], 5)
    assert [i.url for i in items] == ["https://cafe.example.com/board/10"]
    with pytest.raises(ValueError):
        CommunitySource("https://cafe.example.com/search", driver, clock, _pacer()[0])


def test_google_trends_parses_rss(fixtures_dir: Path, clock) -> None:
    xml = (fixtures_dir / "sources" / "trends_kr.xml").read_text("utf-8")
    items = parse_trends_rss(xml, clock, 10)
    assert len(items) == 3
    assert items[0].title == "윤택"
    assert items[0].text.startswith("급상승 검색어: 윤택")
    assert "관련 뉴스" in items[0].text
    assert items[0].url.startswith("http")
    # 같은 날 다시 읽으면 같은 해시(중복 수집 방지)
    assert parse_trends_rss(xml, clock, 1)[0].url_hash == items[0].url_hash


def test_google_trends_source_failure_returns_empty(clock) -> None:
    def boom(url: str) -> str:
        raise OSError("offline")

    assert GoogleTrendsSource(clock, fetch=boom).collect([], 5) == []
