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


# ---------------------------------------------------------------- C44 한 번 다시 읽기


def _published(
    ctx: JobContext, app_config, n: int, days_ago: int, platform: str = "instagram"
) -> str:
    from datetime import timedelta

    from nsm.domain.models import Content, ContentBrief
    from tests.fakes import FakeTextGenerator, load_source_items

    cid = f"c{platform[0]}{n}"
    gen = FakeTextGenerator().generate_content(
        ContentBrief(topic="t", template="info", sources=load_source_items()[:1]),
        app_config.persona,
        app_config.brand,
    )
    ctx.repo.insert_content(
        Content(
            id=cid,
            status="approved",
            generated=gen,
            relevance_confidence=0.9,
            created_at=ctx.clock.now(),
        )
    )
    pid = ctx.repo.create_publications(cid, [platform])[0]
    ctx.repo.transition_publication(pid, "publishing")
    ctx.repo.transition_publication(pid, "published", external_id=f"{platform[0]}{n}", url="u")
    at = ctx.clock.now() - timedelta(days=days_ago)
    ctx.repo.db.execute("UPDATE publications SET published_at = ? WHERE id = ?", [at, pid])
    return f"{platform[0]}{n}"


def _read_ids(ctx: JobContext) -> list[str]:
    return [
        r[0] for r in ctx.repo.db.execute("SELECT post_external_id FROM metrics_posts ORDER BY 1")
    ]


def test_reread_reads_90_days_once(ctx: JobContext, app_config) -> None:
    from nsm.engine.health_metrics import REREAD_KEY

    for n, days in enumerate((1, 20, 60, 120)):
        _published(ctx, app_config, n, days)
    _published(ctx, app_config, 9, 20, "threads")  # 스레드 옛 글은 다시 읽지 않는다
    r = run_metrics(ctx)
    assert _read_ids(ctx) == ["i0", "i1", "i2"]  # 120일 전 글은 빠짐
    assert r.counts["reread"] == "인스타그램 지표 다시 읽기 3/3"
    assert ctx.repo.get_setting(REREAD_KEY)["done"] is True
    r2 = run_metrics(ctx)  # 두 번째부터는 평소처럼 최근 7일만
    assert "reread" not in r2.counts
    assert _read_ids(ctx) == ["i0", "i0", "i1", "i2"]


def test_reread_resumes_after_rate_limit(ctx: JobContext, app_config) -> None:
    from datetime import timedelta

    from nsm.adapters.platforms.aside_common import RateLimitedError
    from nsm.engine.health_metrics import REREAD_KEY
    from nsm.engine.summary import describe

    ids = [_published(ctx, app_config, n, 10 + n) for n in range(3)]  # 최근 7일 글 없음
    ig = ctx.adapters["instagram"]
    orig = ig.read_post_metrics
    seen: list[str] = []

    def limited(ext: str):
        seen.append(ext)
        if len(seen) == 2:
            raise RateLimitedError("instagram 요청 제한", ctx.clock.now() + timedelta(hours=2))
        return orig(ext)

    ig.read_post_metrics = limited
    r = run_metrics(ctx)
    st = ctx.repo.get_setting(REREAD_KEY)
    assert st == {"read": [ids[0]], "done": False}
    assert r.counts["reread"].startswith("인스타그램 지표 다시 읽기 1/3")
    assert "다시 읽기 1/3" in describe("metrics", counts=r.counts)
    calls: list[str] = []
    ig.read_post_metrics = lambda ext: (calls.append(ext), orig(ext))[1]
    r2 = run_metrics(ctx)
    assert sorted(calls) == sorted(ids[1:])  # 이미 읽은 글은 건너뛴다
    assert r2.counts["reread"] == "인스타그램 지표 다시 읽기 3/3"
    assert ctx.repo.get_setting(REREAD_KEY)["done"] is True


def test_reread_failures_are_errors_not_zeros(ctx: JobContext, app_config) -> None:
    from nsm.adapters.platforms.aside_common import PlatformError

    _published(ctx, app_config, 0, 30)

    def broken(ext: str):
        raise PlatformError("인스타그램: 인사이트 글에서 조회수를 못 찾음: " + ext, "ui_changed")

    ctx.adapters["instagram"].read_post_metrics = broken
    r = run_metrics(ctx)
    assert _read_ids(ctx) == []  # 0으로 저장하지 않는다
    assert any("조회수를 못 찾음" in e for e in r.errors)
    assert r.counts["reread"] == "인스타그램 지표 다시 읽기 0/1"
