"""네이버 검색 결과의 날짜 표기 → published_at(S11). 못 읽으면 None이고 수집 시각으로 대신하지 않는다."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nsm.adapters.drivers.base import Link
from nsm.adapters.sources.web import _items_from_links, parse_naver_date
from tests.fakes import FixedClock

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def test_relative_and_absolute_dates() -> None:
    assert parse_naver_date("… 3시간 전 · 블로그", NOW) == NOW - timedelta(hours=3)
    assert parse_naver_date("2일 전 기사", NOW) == NOW - timedelta(days=2)
    assert parse_naver_date("어제 올린 글", NOW) == NOW - timedelta(days=1)
    assert parse_naver_date("2026.10.19. 네이버뉴스", NOW) == NOW.replace(day=19, hour=12, minute=0)
    assert parse_naver_date("날짜 없음", NOW) is None
    assert parse_naver_date("10월 20일 전후로 출시", NOW) is None  # 낱말 속 "전"은 날짜가 아니다
    assert parse_naver_date("7일 전체 일정 공개", NOW) is None
    assert parse_naver_date("2026.13.40.", NOW) is None


def test_items_from_links_fill_published_at_or_leave_none() -> None:
    import re

    clock = FixedClock(NOW)
    links = [
        Link("치이카와 ANA 콜라보 굿즈 23일 출시", "https://news.example.com/a/1", "요약 5시간 전"),
        Link("치이카와 극장판 4DX 확정 소식입니다", "https://news.example.com/a/2", "요약문만"),
    ]
    items = _items_from_links(links, re.compile(r"^https?://"), "naver", clock, 10, set())
    assert items[0].published_at == NOW - timedelta(hours=5)
    assert items[1].published_at is None and items[1].collected_at == NOW
