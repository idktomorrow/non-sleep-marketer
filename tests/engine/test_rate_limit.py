"""경계 1: 플랫폼 요청 제한(HTTP 429)과 인스타그램 댓글 확인 간격 (ADR 0038).

2026-10-14~15: 인스타그램이 글 페이지를 429로 막았는데 "로그인 필요"로 보였고,
5분마다 최근 14일 글을 모두 열어 하루 넘게 풀리지 않았다.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from nsm.adapters.platforms.aside_common import RateLimitedError
from nsm.engine.context import JobContext
from nsm.engine.health_metrics import run_health, run_metrics
from nsm.engine.publish import PAUSED_KEY, run_publish
from nsm.engine.reply import run_reply
from nsm.engine.runner import run_job
from tests.engine.test_publish_job import ctx as publish_ctx  # noqa: F401


def _limit(adapter, clock, hours: float = 2) -> None:
    """어댑터가 요청 제한을 만난 상태: 다음 호출부터 쉬는 시각까지 브라우저를 열지 않는다."""
    until = clock.now() + timedelta(hours=hours)

    def blocked(*_a, **_k):
        adapter.blocked_until = until
        raise RateLimitedError("instagram 요청 제한: 요청이 너무 많음(HTTP 429)", until)

    return blocked


def _ig_fetches(c: JobContext) -> list:
    calls: list = []
    real = c.adapters["instagram"].fetch_new_comments

    def spy(ids, since):
        calls.append(list(ids))
        return real(ids, since)

    c.adapters["instagram"].fetch_new_comments = spy
    return calls


def test_rate_limit_is_a_rest_not_a_failure_or_logout(ctx: JobContext) -> None:  # noqa: F811
    ig = ctx.adapters["instagram"]
    ig.fetch_new_comments = _limit(ig, ctx.clock)
    r = run_reply(ctx)
    assert r.ok and not r.errors  # 대시보드에 "실패"로 쌓이지 않는다
    assert r.counts["rate_limited"] == ["인스타그램 요청 제한으로 쉬는 중(12:00 이후 다시)"]
    assert r.counts["auto_replied"] > 0  # 다른 플랫폼은 그대로
    limits = [(b, lv) for t, b, lv in ctx.notifier.calls if t == "인스타그램 요청 제한"]
    assert len(limits) == 1 and limits[0][1] == "warn"
    assert "로그인은 풀리지 않았습니다" in limits[0][0] and "12:00" in limits[0][0]
    assert ctx.repo.get_setting(PAUSED_KEY, []) == []
    run_reply(ctx)  # 쉬는 동안 다시 돌아도 알림은 한 번만
    assert [t for t, _, _ in ctx.notifier.calls].count("인스타그램 요청 제한") == 1


def test_resting_platform_is_not_opened(ctx: JobContext) -> None:  # noqa: F811
    ctx.adapters["instagram"].blocked_until = ctx.clock.now() + timedelta(hours=1)
    calls = _ig_fetches(ctx)
    r = run_job("reply", ctx, manual=True)  # 사람이 "지금 실행"을 눌러도 쉰다
    assert calls == [] and r.counts["rate_limited"]
    ctx.clock.advance(hours=1)
    run_reply(ctx)
    assert len(calls) == 1


def test_instagram_comments_are_checked_less_often(ctx: JobContext) -> None:  # noqa: F811
    """인스타그램은 30분(설정)마다, 스레드는 잡이 돌 때마다. 지터 때문에 2분 일찍 와도 확인한다."""
    calls = _ig_fetches(ctx)
    threads = []
    real = ctx.adapters["threads"].fetch_new_comments
    ctx.adapters["threads"].fetch_new_comments = lambda ids, since: (
        threads.append(1) or real(ids, since)
    )
    for _ in range(6):  # 5분마다 6번(25분)
        run_reply(ctx)
        ctx.clock.advance(minutes=5)
    assert len(calls) == 1 and len(threads) == 6
    ctx.clock.advance(minutes=-2)  # 처음부터 28분
    run_reply(ctx)
    assert len(calls) == 2
    run_job("reply", ctx, manual=True)  # "지금 실행"은 간격을 무시
    assert len(calls) == 3


def test_failed_check_also_waits_for_the_interval(ctx: JobContext) -> None:  # noqa: F811
    def boom(ids, since):
        raise RuntimeError("network")

    ctx.adapters["instagram"].fetch_new_comments = boom
    assert run_reply(ctx).errors
    ctx.clock.advance(minutes=5)
    assert not run_reply(ctx).errors  # 막힌 플랫폼을 5분마다 두드리지 않는다


def test_instagram_looks_only_at_recent_posts(ctx: JobContext) -> None:  # noqa: F811
    calls = _ig_fetches(ctx)
    ctx.clock.advance(days=4)  # 글이 4일 전 것이 된다(인스타그램 기본 3일, 스레드 14일)
    run_reply(ctx)
    assert calls == []
    s = ctx.config.settings.model_copy(update={"instagram_comment_lookback_days": 7})
    ctx.config = ctx.config.model_copy(update={"settings": s})
    run_job("reply", ctx, manual=True)
    assert calls == [["p2"]]


def test_health_does_not_pause_or_ask_login_on_rate_limit(ctx: JobContext) -> None:  # noqa: F811
    ig = ctx.adapters["instagram"]
    ig.health = _limit(ig, ctx.clock)
    r = run_health(ctx)
    assert ctx.repo.get_setting(PAUSED_KEY, []) == []
    assert r.counts["rate_limited"] and not any("로그인" in t for t, _, _ in ctx.notifier.calls)
    run_health(ctx)  # 쉬는 동안은 확인하지 않고 그대로 쉰다
    assert ctx.repo.get_setting(PAUSED_KEY, []) == []


def test_metrics_stop_at_first_rate_limit(ctx: JobContext) -> None:  # noqa: F811
    ig = ctx.adapters["instagram"]
    hits = []
    blocked = _limit(ig, ctx.clock)
    ig.read_post_metrics = lambda ext: hits.append(ext) or blocked()
    r = run_metrics(ctx)
    assert hits == ["p2"] and r.counts["rate_limited"]
    assert not any("instagram" in e for e in r.errors)


def test_publish_waits_while_resting_without_using_a_retry(publish_ctx: JobContext) -> None:  # noqa: F811
    c = publish_ctx
    c.adapters["instagram"].blocked_until = c.clock.now() + timedelta(hours=2)
    r = run_job("publish", c, manual=True)  # 사람이 누른 게시하기도 쉰다(계정 보호)
    assert not c.adapters["instagram"].published and c.adapters["threads"].published
    assert any("인스타그램 요청 제한으로 쉬는 중" in w for w in r.counts["waits"])
    assert all(p.attempts == 0 for p in c.repo.due_publications("instagram", c.clock.now()))


@pytest.mark.parametrize("platform", ["instagram"])
def test_publish_hit_by_rate_limit_notifies_once(publish_ctx: JobContext, platform) -> None:  # noqa: F811
    c = publish_ctx
    c.adapters[platform].publish = _limit(c.adapters[platform], c.clock)
    run_publish(c)
    assert [t for t, _, _ in c.notifier.calls].count("인스타그램 요청 제한") == 1
    assert c.adapters[platform].blocked_until


def test_rate_limit_while_replying_leaves_comments_for_later(ctx: JobContext) -> None:  # noqa: F811
    """답글을 달다 막혀도 댓글을 사람에게 넘기지 않는다: 'new'로 남겨 쉬고 나서 처리."""
    from tests.fakes import load_comment_cases

    praise = next(c for c, cat in load_comment_cases() if cat == "praise_general")
    ig = ctx.adapters["instagram"]
    for i in (1, 2):  # 자동 답글 대상 칭찬 댓글 2개
        ig.comments["p2"].append(
            praise.model_copy(
                update={
                    "platform": "instagram",
                    "external_id": f"igp{i}",
                    "post_external_id": "p2",
                    "created_at": ctx.clock.now() - timedelta(minutes=i),
                }
            )
        )
    ig.reply = _limit(ig, ctx.clock)
    r = run_reply(ctx)
    assert r.counts["rate_limited"] and not ig.replied
    assert ctx.repo.get_comment("instagram:igp1").status == "new"  # 사람에게 넘기지 않음
    assert ctx.repo.get_comment("instagram:igp2").status == "new"
    ig.reply = lambda c, text: "r1"
    ctx.clock.advance(hours=2)
    run_reply(ctx)  # 쉬고 나면 남은 댓글을 처리
    assert ctx.repo.get_comment("instagram:igp1").status == "auto_replied"


def test_rate_limited_publish_keeps_its_retries(publish_ctx: JobContext) -> None:  # noqa: F811
    """요청 제한은 원고 탓이 아니다: 여러 번 걸려도 포기·검토로 돌리지 않고, 쉬는 끝 시각에 다시."""
    c = publish_ctx
    ig = c.adapters["instagram"]
    for _ in range(len(c.config.settings.retry_delays_minutes) + 2):
        ig.blocked_until = None
        ig.publish = _limit(ig, c.clock)
        run_job("publish", c, manual=True)
        c.clock.set(ig.blocked_until)
    due = c.repo.due_publications("instagram", c.clock.now())
    assert due and due[0].attempts == 0 and due[0].status == "failed"
    assert not any(t == "게시 포기" for t, _, _ in c.notifier.calls)


def test_metrics_count_rows_inserted_before_the_limit(ctx: JobContext) -> None:  # noqa: F811
    ig = ctx.adapters["instagram"]
    blocked = _limit(ig, ctx.clock)
    ig.read_account_metrics = blocked  # 게시물 지표는 읽고 계정 지표에서 막힘
    r = run_metrics(ctx)
    assert r.counts["posts"] >= 2 and r.counts["rate_limited"]  # threads·instagram 게시물 지표


def test_notice_uses_the_right_particle(ctx: JobContext) -> None:  # noqa: F811
    from nsm.engine.rate_limit import resting

    resting(ctx, "threads", ctx.clock.now() + timedelta(hours=2))
    resting(ctx, "instagram", ctx.clock.now() + timedelta(hours=2))
    bodies = [b for t, b, _ in ctx.notifier.calls if t.endswith("요청 제한")]
    assert bodies[0].startswith("스레드가 ") and bodies[1].startswith("인스타그램이 ")
