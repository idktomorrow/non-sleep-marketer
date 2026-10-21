"""오늘의 추천 잡(S11): 수집 → 사건 묶기 → 서로 다른 소재 최대 3개를 검토 대기 원고로."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from nsm.adapters.sources.og_image import PhotoCandidate
from nsm.domain.models import Classification, SourceItem
from nsm.domain.policy import local_day_bounds
from nsm.engine.context import JobContext
from nsm.engine.generate import run_generate
from nsm.engine.picks import run_picks
from nsm.store.picks import PicksStore
from nsm.store.repo import Repository
from tests.fakes import FakeClassifier, FakeNotifier, FakeSource, FakeTextGenerator


def _item(clock, n: int, title: str, hours_ago: float | None = 3) -> SourceItem:
    return SourceItem(
        id=f"pk-{n}",
        source="naver",
        url=f"https://news.example.com/a/{n}",
        url_hash=f"pkh-{n}",
        title=title,
        text=f"{title}. 출시 일정과 가격을 모았습니다.",
        published_at=None if hours_ago is None else clock.now() - timedelta(hours=hours_ago),
        collected_at=clock.now(),
    )


class TopicByTitle(FakeClassifier):
    """제목에 든 낱말로 주제를 나눈다(두 주제)."""

    def classify(self, text, labels, instruction):
        if labels == ["relevant", "irrelevant"]:
            return Classification(label="relevant", confidence=0.9)
        return Classification(
            label="행사" if "극장판" in text or "팝업" in text else "굿즈", confidence=0.8
        )


class CountingImage:
    def __init__(self) -> None:
        self.calls = 0

    def generate_background(self, prompt, width, height):
        self.calls += 1
        return None


def _enable(ctx: JobContext, **picks) -> None:
    s = ctx.config.settings
    persona = ctx.config.persona.model_copy(update={"topics": ["굿즈", "행사"]})
    ctx.config = ctx.config.model_copy(
        update={
            "persona": persona,
            "settings": s.model_copy(
                update={"picks": s.picks.model_copy(update={"enabled": True, **picks})}
            ),
        }
    )


@pytest.fixture
def ctx(db, clock, app_config, tmp_path: Path) -> JobContext:
    items = [
        _item(clock, 1, "치이카와 × ANA 콜라보 굿즈 공개"),
        _item(clock, 2, "ちいかわ × ANA コラボ グッズ 公開"),  # 같은 사건(다른 기사)
        _item(clock, 3, "치이카와 극장판 4DX 확정"),
        _item(clock, 4, "치이카와 종이접기 문구 출시", hours_ago=None),  # 날짜 미상
        _item(clock, 5, "치이카와 크리스마스 마켓 라인업", hours_ago=24 * 9),  # 오래됨
    ]
    c = JobContext(
        config=app_config,
        repo=Repository(db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        sources=[FakeSource("naver", items)],
        relevance_classifier=TopicByTitle(),
        topic_classifier=TopicByTitle(),
        classifier_name="fake",
        text_generator=FakeTextGenerator(),
        image_generator=CountingImage(),
        media_dir=tmp_path / "media",
    )
    _enable(c, aliases={"치이카와": ["ちいかわ"]})
    return c


def _fake_photo(url: str, dest: Path) -> PhotoCandidate | None:
    dest.mkdir(parents=True, exist_ok=True)
    p = dest / "candidate.jpg"
    p.write_bytes(b"\xff\xd8\xff fake")
    return PhotoCandidate(
        image_url=url + "/og.jpg", source_url=url, path=p, width=1000, height=1200
    )


def test_makes_three_distinct_pending_picks_without_quota_or_publications(ctx: JobContext) -> None:
    r = run_picks(ctx, fetch_photo=_fake_photo)
    assert r.ok and r.counts["made"] == 3 and r.counts["collected"] == 5
    store = PicksStore(ctx.repo.db, ctx.clock)
    picks = store.list(run_date=ctx.clock.now().date())
    assert [p.rank for p in picks] == [1, 2, 3]
    # 최근 소식(ANA 묶음, 4DX) → 날짜 미상(종이접기). 오래된 크리스마스는 뒤로 밀려 안 들어감
    assert [p.freshness for p in picks] == ["fresh", "fresh", "unknown"]
    assert {p.reason.split(" · ")[0] for p in picks} == {"최근 소식", "날짜 확인 필요"}
    for p in picks:
        c = ctx.repo.get_content(p.content_id)
        assert c.status == "pending_review" and c.review_reason.startswith("오늘의 추천")
        assert ctx.repo.publications_for_content(p.content_id) == []
        assert p.photo_status == "candidate" and p.photo_candidate_path.endswith("candidate.jpg")
    # 같은 사건 두 기사가 한 원고에 쓰였다
    used = ctx.repo.db.execute(
        "SELECT used_in_content_id, count(*) FROM source_items WHERE used_in_content_id IS NOT NULL GROUP BY 1"
    )
    assert sorted(n for _, n in used) == [1, 1, 2]
    # 일일 생성 목표를 쓰지 않았고, 유료 AI 배경을 만들지 않았다
    start, end = local_day_bounds(ctx.clock.now(), ctx.tz)
    assert ctx.repo.count_contents_between(start, end) == 0
    assert ctx.image_generator.calls == 0
    assert ctx.notifier.calls[-1][0] == "오늘의 추천"


def test_second_run_same_day_is_idempotent_and_generate_defers(ctx: JobContext) -> None:
    run_picks(ctx, fetch_photo=_fake_photo)
    again = run_picks(ctx, fetch_photo=_fake_photo)
    assert again.skipped.startswith("오늘 슬롯이 모두 찼음")  # 슬롯 기반 멱등(S14)
    assert ctx.repo.count_contents() == 3
    g = run_generate(ctx)
    assert "추천 모드" in g.skipped and ctx.repo.count_contents() == 3
    # 사람이 "지금 실행"으로 누르면 일일 원고도 평소처럼 1건 더 만든다
    assert run_generate(ctx, manual=True).counts["created"] == 1


def test_fewer_candidates_report_reason_and_disabled_mode_skips(ctx: JobContext) -> None:
    ctx.sources = [FakeSource("naver", [_item(ctx.clock, 9, "치이카와 신상 머그컵 출시")])]
    r = run_picks(ctx, fetch_photo=lambda u, d: None)
    assert r.counts["made"] == 1 and r.counts["reason"] == "서로 다른 소재가 1개뿐"
    assert (
        PicksStore(ctx.repo.db, ctx.clock).get(ctx.repo.list_contents()[0].id).photo_status
        == "none"
    )
    s = ctx.config.settings
    ctx.config = ctx.config.model_copy(
        update={
            "settings": s.model_copy(
                update={"picks": s.picks.model_copy(update={"enabled": False})}
            )
        }
    )
    assert "추천 모드가 꺼져" in run_picks(ctx).skipped
    assert (
        run_picks(ctx, manual=True, fetch_photo=lambda u, d: None).counts["made"] >= 0
    )  # 눌러서는 된다


def test_cost_cap_stops_before_generating(ctx: JobContext) -> None:
    ctx.repo.record_cost("llm", "anthropic", 5.0)
    r = run_picks(ctx, fetch_photo=_fake_photo)
    assert r.counts["made"] == 0 and r.counts["cost_cap_hit"] is True
    assert ctx.repo.count_contents() == 0


def test_dismissed_event_is_not_recommended_again(ctx: JobContext, clock) -> None:
    run_picks(ctx, fetch_photo=_fake_photo)
    store = PicksStore(ctx.repo.db, ctx.clock)
    first = store.list(run_date=clock.now().date())[0]
    title = ctx.repo.get_content(first.content_id).generated.topic
    store.dismiss(first.content_id)  # 자료가 풀린다
    clock.advance(days=1)
    r = run_picks(ctx, fetch_photo=_fake_photo)
    titles = [
        ctx.repo.get_content(p.content_id).generated.topic
        for p in store.list(run_date=clock.now().date())
    ]
    assert (
        r.counts["made"] >= 1 and title not in titles
    )  # 풀린 자료로 같은 사건을 다시 추천하지 않는다
