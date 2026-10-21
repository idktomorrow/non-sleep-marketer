"""관심 주제 순환·계정 충돌 확인·밈 탐색 분류(S16 제안, ADR 0060)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from nsm.domain.meme_discovery import Sighting, author_of, candidates, platform_of
from nsm.domain.topics import account_topic_conflict, interleave_by_topic, topic_order

T0 = datetime(2026, 10, 21, 12, tzinfo=UTC)


def test_topic_order_least_recently_used_first() -> None:
    topics = ["치이카와", "한국 아이돌", "뉴진스"]
    assert topic_order(topics, {}) == topics
    used = {"치이카와": T0, "한국 아이돌": T0 - timedelta(days=2)}
    assert topic_order(topics, used) == ["뉴진스", "한국 아이돌", "치이카와"]


@dataclass
class C:
    topic: str
    n: int


def test_interleave_keeps_one_topic_per_item_and_alternates() -> None:
    ranked = [C("치이카와", 1), C("치이카와", 2), C("한국 아이돌", 1)]
    out = interleave_by_topic(ranked, ["한국 아이돌", "치이카와"])
    assert [(c.topic, c.n) for c in out] == [("한국 아이돌", 1), ("치이카와", 1), ("치이카와", 2)]


def test_account_conflict_warns_but_does_not_decide() -> None:
    assert account_topic_conflict("커피 정보", "커피를 공부하는 바리스타", ["치이카와"])
    assert account_topic_conflict("캐릭터 굿즈 소식", "치이카와 덕후", ["치이카와"]) is None
    assert account_topic_conflict("x", "y", []) is None


def _s(url: str, title: str, days: float | None, snippet: str = "") -> Sighting:
    return Sighting(
        url=url,
        title=title,
        snippet=snippet,
        platform=platform_of(url),
        posted_at=None if days is None else T0 - timedelta(days=days),
        observed_at=T0,
        author=author_of(url),
    )


def test_categories_need_distinct_authors_and_recency() -> None:
    same = "치이카와 우는 짤 모음 하치와레 위로"
    recent = [_s(f"https://blog.naver.com/u{i}/1{i}", same, 1 + i) for i in range(3)]
    lone = [_s("https://blog.naver.com/solo/9", "치이카와 혼자 본 밈 낯선 반전", 1)]
    one_author_many = [
        _s(f"https://blog.naver.com/same/{i}", "아이돌 직캠 반응 짤 정리 모음", 1) for i in range(4)
    ]
    cands = {c.key: c for c in candidates(recent + lone + one_author_many, T0)}
    assert cands[same].category == "A" and cands[same].authors == 3
    assert cands["치이카와 혼자 본 밈 낯선 반전"].category == "C"
    assert (
        cands["아이돌 직캠 반응 짤 정리 모음"].category == "C"
    )  # 한 작성자의 글 여러 개는 확산 근거가 아니다
    old = [
        _s(f"https://blog.naver.com/o{i}/2{i}", "치이카와 시러 밈 계속 쓰임", d)
        for i, d in enumerate((2, 20, 60))
    ]
    assert candidates(old, T0)[0].category == "B"
    undated = [
        _s(f"https://cafe.naver.com/c{i}/3{i}", "아이돌 팬 공감 밈 반응", None) for i in range(3)
    ]
    assert (
        candidates(undated, T0)[0].category == "B"
    )  # 작성자는 많지만 최근 7일 근거가 없으면 A가 아니다


def test_metrics_stay_unknown_and_x_platform() -> None:
    s = _s("https://x.com/fan/status/123", "치이카와 트윗", 1)
    assert s.platform == "X" and s.metrics is None and s.to_dict()["metrics"] is None
    assert not s.body_checked


def test_author_ignores_mobile_host() -> None:
    """같은 블로거의 모바일·PC 주소는 한 사람(S16 리뷰: 작성자 수 부풀림)."""
    assert author_of("https://m.blog.naver.com/U1/2") == author_of("https://blog.naver.com/u1/1")
    assert author_of("https://twitter.com/abc/status/1") == author_of("https://x.com/abc/status/2")


def test_discovery_splits_quota_and_stops_at_limit() -> None:
    """첫 출처가 상한을 다 쓰지 않고, 상한에 닿으면 남은 요청을 하지 않는다(S16 리뷰)."""
    from nsm.adapters.drivers.base import Link
    from nsm.adapters.sources.common import Pacer
    from nsm.adapters.sources.meme_discovery import MemeDiscovery

    class Driver:
        def __init__(self) -> None:
            self.url = ""
            self.visits: list[str] = []

        def goto(self, url):
            self.url = url
            self.visits.append(url)

        def links(self):
            kind = "blog" if "where=blog" in self.url else "cafe"
            n = len(self.visits)
            return [
                Link(
                    f"치이카와 밈 모음 {kind} {n}-{i}",
                    f"https://{kind}.naver.com/u{n}{i}/{n}{i}0",
                    "",
                )
                for i in range(40)
            ]

    class Clock:
        def now(self):
            return datetime(2026, 10, 21, tzinfo=UTC)

    d = Driver()
    res = MemeDiscovery(d, Clock(), Pacer(0, 0, sleep=lambda s: None), ()).discover(
        "치이카와", limit=10
    )
    platforms = {s.platform for s in res.sightings}
    assert len(res.sightings) == 10 and len(platforms) == 2  # 블로그·카페가 나눠 가짐
    assert len(d.visits) == 2  # 출처마다 몫을 채우면 두 번째 검색어는 보내지 않음
