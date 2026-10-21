"""오늘의 추천 순수 규칙(S11): 사건 묶기, 최신성, 제외, 주제 다양성."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nsm.domain.models import SourceItem
from nsm.domain.picks_policy import (
    cluster_candidates,
    date_tokens,
    freshness,
    normalize_tokens,
    rank_clusters,
    same_event,
)

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
ALIASES = {"치이카와": ["ちいかわ", "Chiikawa"]}


def _item(
    n: int, title: str, published_at: datetime | None = NOW - timedelta(hours=5)
) -> SourceItem:
    return SourceItem(
        id=f"p-{n}",
        source="naver",
        url=f"https://example.com/{n}",
        url_hash=f"ph-{n}",
        title=title,
        text=title,
        published_at=published_at,
        collected_at=NOW,
    )


def test_alias_normalizes_to_canonical_topic() -> None:
    assert "치이카와" in normalize_tokens("ちいかわ×ANA コラボ 発表", ALIASES)
    assert "치이카와" in normalize_tokens("Chiikawa x ANA collab", ALIASES)


def test_same_event_across_korean_and_japanese_headlines() -> None:
    ko = "치이카와 × ANA 항공 콜라보 발표, 10월 23일 출시"
    ja = "ちいかわ × ANA コラボ 10月23日 発売"
    assert same_event(ko, ja, ALIASES)


def test_different_product_or_date_is_not_merged() -> None:
    a = "치이카와 굿즈 신상 '마스코트 인형' 10월 23일 출시"
    b = "치이카와 굿즈 신상 '아크릴 키링' 10월 23일 출시"
    assert not same_event(a, b, ALIASES)  # 따옴표 고유명이 다르다
    c = "치이카와 팝업 10월 23일 오픈"
    d = "치이카와 팝업 11월 1일 오픈"
    assert date_tokens(c) == {"10-23"} and not same_event(c, d, ALIASES)  # 날짜가 다르다


def test_cluster_merges_same_announcement_from_two_urls() -> None:
    cands = [
        (_item(1, "치이카와 × ANA 콜라보 굿즈 공개"), 0.9, "치이카와 굿즈"),
        (_item(2, "ちいかわ × ANA コラボ グッズ 公開"), 0.85, "치이카와 굿즈"),
        (_item(3, "치이카와 극장판 4DX 확정"), 0.8, "치이카와 행사"),
    ]
    clusters = cluster_candidates(cands, ALIASES)
    assert [len(c.items) for c in clusters] == [2, 1]
    assert clusters[0].best_relevance == 0.9 and clusters[0].sorted_items()[0][0].id == "p-1"


def test_freshness_uses_published_at_not_collected_at() -> None:
    assert freshness(NOW - timedelta(hours=10), NOW, 72) == "fresh"
    assert freshness(NOW - timedelta(days=5), NOW, 72) == "stale"
    assert freshness(None, NOW, 72) == "unknown"


def test_rank_prefers_fresh_then_diverse_topics_and_excludes_recent_or_dismissed() -> None:
    cands = [
        (_item(1, "치이카와 × ANA 콜라보 굿즈 공개"), 0.95, "치이카와 굿즈"),
        (
            _item(2, "치이카와 크리스마스 마켓 라인업", NOW - timedelta(days=9)),
            0.99,
            "치이카와 굿즈",
        ),
        (_item(3, "치이카와 극장판 4DX 확정"), 0.8, "치이카와 행사"),
        (_item(4, "치이카와 종이접기 문구 출시", None), 0.9, "치이카와 굿즈"),
    ]
    clusters = cluster_candidates(cands, ALIASES)
    ranked = rank_clusters(
        clusters, exclude=["치이카와 극장판 4DX 확정 소식"], now=NOW, fresh_hours=72
    )
    # 4DX는 최근에 다뤘으니 제외. 최근 소식(ANA) → 날짜 미상(종이접기) → 오래된 것(크리스마스)
    titles = [c.items[0][0].title for c in ranked]
    assert titles == [
        "치이카와 × ANA 콜라보 굿즈 공개",
        "치이카와 종이접기 문구 출시",
        "치이카와 크리스마스 마켓 라인업",
    ]
    assert [freshness(c.published_at, NOW, 72) for c in ranked] == ["fresh", "unknown", "stale"]


def test_rank_round_robins_topics() -> None:
    cands = [
        (_item(1, "롤 패치 16.21 챔피언 조정"), 0.95, "롤 패치"),
        (_item(2, "롤 패치 16.21 아이템 변경"), 0.94, "롤 패치"),
        (_item(3, "LCK 결승 일정 확정"), 0.9, "롤 e스포츠"),
    ]
    ranked = rank_clusters(cluster_candidates(cands), exclude=[], now=NOW, fresh_hours=72)
    assert [c.topic for c in ranked][:2] == ["롤 패치", "롤 e스포츠"]
