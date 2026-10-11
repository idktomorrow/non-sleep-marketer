from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from nsm.domain.models import AccountMetrics, Content, ContentBrief, PostMetrics
from nsm.engine.context import JobContext
from nsm.engine.weekly_report import build_report, run_weekly_report
from nsm.store.repo import Repository
from tests.fakes import FakeNotifier, FakeTextGenerator, load_source_items


def _publish(
    ctx: JobContext,
    cid: str,
    topic: str,
    template: str,
    platform: str,
    ext: str,
    views: int,
    likes: int,
    saves: int = 0,
    is_ad: bool = False,
    persona_topic: str = "핸드드립",
) -> None:
    gen = (
        FakeTextGenerator()
        .generate_content(
            ContentBrief(topic=topic, template=template, sources=load_source_items()[:1]),
            ctx.config.persona,
            ctx.config.brand,
        )
        .model_copy(update={"topic": topic})
    )
    ctx.repo.insert_content(
        Content(
            id=cid,
            status="approved",
            generated=gen,
            relevance_confidence=0.9,
            created_at=ctx.clock.now(),
            is_ad=is_ad,
        ),
        persona_topic=persona_topic,
    )
    pid = ctx.repo.create_publications(cid, [platform])[0]
    ctx.repo.transition_publication(pid, "publishing")
    ctx.repo.transition_publication(pid, "published", external_id=ext, url=f"https://x/{ext}")
    ctx.repo.insert_post_metrics(
        PostMetrics(
            platform=platform,
            post_external_id=ext,
            captured_at=ctx.clock.now(),
            views=views,
            likes=likes,
            comments=1,
            saves=saves,
            shares=0,
        )
    )


@pytest.fixture
def ctx(db, clock, app_config, tmp_path: Path) -> JobContext:
    c = JobContext(
        config=app_config,
        repo=Repository(db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        reports_dir=tmp_path / "reports",
    )
    c.repo.insert_account_metrics(
        AccountMetrics(
            platform="threads", captured_at=clock.now() - timedelta(days=6), followers=100
        )
    )
    _publish(c, "c1", "물 온도 정리", "info", "threads", "t1", views=1000, likes=50, saves=30)
    _publish(
        c,
        "c2",
        "원두 보관법",
        "list",
        "instagram",
        "i1",
        views=1000,
        likes=10,
        persona_topic="원두",
    )
    _publish(c, "c3", "협찬 드립백", "compare", "threads", "t2", views=500, likes=20, is_ad=True)
    c.repo.insert_account_metrics(
        AccountMetrics(platform="threads", captured_at=clock.now(), followers=130)
    )
    c.repo.record_cost("llm", "anthropic", 1.25)
    c.clock.advance(days=1)  # 리포트는 "어제까지 7일"을 본다
    return c


def test_report_sections_and_ranking(ctx: JobContext) -> None:
    text, summary = build_report(ctx)
    assert summary["posts"] == 3
    assert summary["top"][0] == "물 온도 정리"  # 참여율 8.1%로 1위
    for h in [
        "## 요약",
        "## 팔로워",
        "## 가장 잘된 콘텐츠",
        "## 다음 주 추천 주제",
        "## 협찬(광고) 게시물",
    ]:
        assert h in text
    assert "threads: 100 → 130 (+30)" in text
    assert "저장이 많음" in text and "정보형" in text
    assert "협찬 드립백" in text.split("## 협찬(광고) 게시물")[1]
    assert summary["recommended"][0] == "핸드드립"
    assert "llm $1.25" in text


def test_empty_week(db, clock, app_config, tmp_path: Path) -> None:
    c = JobContext(
        config=app_config,
        repo=Repository(db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        reports_dir=tmp_path,
    )
    text, summary = build_report(c)
    assert summary["posts"] == 0 and "아직 비교할 지표가 없습니다" in text


def test_job_writes_file_and_notifies(ctx: JobContext) -> None:
    r = run_weekly_report(ctx)
    path = ctx.reports_dir / r.counts["file"]
    assert path.exists() and path.read_text("utf-8").startswith("# 주간 리포트")
    assert ctx.notifier.calls[-1][0] == "주간 리포트"


def test_window_is_previous_full_local_days(ctx: JobContext) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # 같은 날 아침·밤에 만들어도 기간이 같다(지터·수동 실행에 흔들리지 않음)
    ctx.clock.set(datetime(2026, 10, 12, 8, 9, tzinfo=ZoneInfo("Asia/Seoul")))
    a, _ = build_report(ctx)
    ctx.clock.set(datetime(2026, 10, 12, 23, 50, tzinfo=ZoneInfo("Asia/Seoul")))
    b, _ = build_report(ctx)
    assert a.splitlines()[0] == b.splitlines()[0] == "# 주간 리포트 2026-10-05 ~ 2026-10-11"
    # 오늘 게시한 글은 이번 리포트에 들어가지 않는다
    _publish(ctx, "c9", "오늘 글", "info", "threads", "t9", views=10, likes=1)
    c, s = build_report(ctx)
    assert s["posts"] == 3 and "오늘 글" not in c


def test_null_followers_and_zero_views(ctx: JobContext) -> None:
    ctx.repo.insert_account_metrics(
        AccountMetrics(
            platform="instagram", captured_at=ctx.clock.now() - timedelta(days=3), followers=None
        )
    )
    _publish(ctx, "c4", "협찬 0조회", "info", "instagram", "i9", views=0, likes=0, is_ad=True)
    ctx.repo.db.execute(
        "UPDATE publications SET published_at = published_at - INTERVAL 1 DAY WHERE external_id = 'i9'"
    )
    text, _ = build_report(ctx)
    assert "None" not in text
    assert "협찬 0조회 (instagram): 조회 0" in text


def test_untried_topic_does_not_beat_proven_one(ctx: JobContext) -> None:
    from nsm.engine.weekly_report import UNTRIED_TOPIC_FACTOR

    _, s = build_report(ctx)
    # 원두(성과 있음, 평균보다 낮음) vs 홈카페(기록 없음): 기록 없는 쪽은 평균 x 0.8로 본다
    assert UNTRIED_TOPIC_FACTOR < 1
    assert s["recommended"].index("핸드드립") == 0
