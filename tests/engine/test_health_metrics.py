from __future__ import annotations

import pytest

from nsm.engine.context import JobContext
from nsm.engine.health_metrics import run_health, run_metrics
from nsm.engine.publish import PAUSED_KEY
from nsm.store.repo import Repository
from tests.fakes import FakeNotifier, FakePlatformAdapter


@pytest.fixture
def ctx(db, clock, app_config) -> JobContext:
    return JobContext(
        config=app_config,
        repo=Repository(db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        adapters={p: FakePlatformAdapter(p, clock=clock) for p in ("threads", "instagram")},
    )


def test_logout_pauses_and_login_resumes(ctx: JobContext) -> None:
    ctx.adapters["instagram"].logged_in = False
    run_health(ctx)
    assert ctx.repo.get_setting(PAUSED_KEY) == ["instagram"]
    assert ctx.notifier.calls[-1][2] == "error"
    run_health(ctx)  # 같은 상태면 알림을 반복하지 않는다
    assert len(ctx.notifier.calls) == 1
    ctx.adapters["instagram"].logged_in = True
    run_health(ctx)
    assert ctx.repo.get_setting(PAUSED_KEY) == []
    assert "다시 연결" in ctx.notifier.calls[-1][0]


def test_health_exception_counts_as_logged_out(ctx: JobContext) -> None:
    def boom():
        raise RuntimeError("browser crashed")

    ctx.adapters["threads"].health = boom
    run_health(ctx)
    assert "threads" in ctx.repo.get_setting(PAUSED_KEY)


def test_metrics_snapshots_recent_posts(ctx: JobContext, app_config) -> None:
    from nsm.domain.models import Content, ContentBrief
    from tests.fakes import FakeTextGenerator, load_source_items

    gen = FakeTextGenerator().generate_content(
        ContentBrief(topic="t", template="info", sources=load_source_items()[:1]),
        app_config.persona,
        app_config.brand,
    )
    ctx.repo.insert_content(
        Content(
            id="c1",
            status="approved",
            generated=gen,
            relevance_confidence=0.9,
            created_at=ctx.clock.now(),
        )
    )
    pid = ctx.repo.create_publications("c1", ["threads"])[0]
    ctx.repo.transition_publication(pid, "publishing")
    ctx.repo.transition_publication(pid, "published", external_id="t1", url="u")
    r = run_metrics(ctx)
    assert r.counts == {"posts": 1, "accounts": 2}
    rows = ctx.repo.db.execute("SELECT platform, post_external_id FROM metrics_posts")
    assert rows == [("threads", "t1")]
