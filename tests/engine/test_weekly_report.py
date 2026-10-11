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
