"""C37: 댓글 조회가 일시적 오류로 연속 실패하면 30분 쉬기, 새 설정 기본값, 소요 시간 경고(ADR 0062, incidents/0011)."""

from __future__ import annotations

from datetime import timedelta

import pytest
import yaml
from pydantic import ValidationError

from nsm.adapters.platforms.aside_common import PlatformError
from nsm.config.schema import AppConfig, Settings
from nsm.engine.context import JobContext
from nsm.engine.publish import PAUSED_KEY
from nsm.engine.reply import FETCH_FAIL_KEY, run_reply
from nsm.engine.runner import run_job
from tests.conftest import FIXTURES
from tests.engine.test_publish_job import ctx as publish_ctx  # noqa: F401

PAGE = "threads: 게시물 본문이 20초 안에 열리지 않음"


def _failing(ctx: JobContext, calls: list) -> None:
    def boom(ids, since):
        calls.append(1)
        raise PlatformError(PAGE, "transient", "page")

    ctx.adapters["threads"].fetch_new_comments = boom


def _rest_notes(ctx: JobContext) -> list:
    return [c for c in ctx.notifier.calls if c[0] == "스레드 댓글 확인 쉬기"]


def test_two_transient_failures_rest_30_minutes_and_notify_once(ctx: JobContext) -> None:  # (d)  # noqa: F811
    calls: list = []
    _failing(ctx, calls)
    run_reply(ctx)
    assert (
        ctx.repo.get_setting(FETCH_FAIL_KEY)["threads"]["failures"] == 1 and _rest_notes(ctx) == []
    )
    ctx.clock.advance(minutes=15)
    r = run_reply(ctx)  # 연속 2번째: 30분 쉬기
    assert any("게시물이 열리지 않아 댓글 확인을 쉬는 중" in w for w in r.counts["rate_limited"])
    title, body, level = _rest_notes(ctx)[0]
    assert (
        body.startswith("스레드 게시물이 열리지 않아 댓글 확인을 30분 쉽니다") and level == "warn"
    )
    n = len(calls)
    ctx.clock.advance(minutes=15)
    run_job("reply", ctx, manual=True)  # 쉬는 동안은 사람이 눌러도 열지 않는다
    assert len(calls) == n
    assert ctx.repo.get_setting(PAUSED_KEY, []) == []  # 게시는 멈추지 않는다
    ctx.clock.advance(minutes=16)
    run_reply(ctx)  # 쉬고 나서 또 실패: 다시 쉬지만 알림은 그 연속 구간에 한 번
    assert len(calls) == n + 1 and len(_rest_notes(ctx)) == 1


def test_success_resets_the_count(ctx: JobContext) -> None:  # noqa: F811
    calls: list = []
    real = ctx.adapters["threads"].fetch_new_comments
    _failing(ctx, calls)
    run_reply(ctx)
    ctx.adapters["threads"].fetch_new_comments = real
    ctx.clock.advance(minutes=15)
    run_reply(ctx)
    assert "threads" not in (ctx.repo.get_setting(FETCH_FAIL_KEY, {}) or {})
    _failing(ctx, calls)
    ctx.clock.advance(minutes=15)
    run_reply(ctx)  # 다시 1회째: 쉬지 않는다
    assert (
        ctx.repo.get_setting(FETCH_FAIL_KEY)["threads"]["failures"] == 1 and _rest_notes(ctx) == []
    )


def test_non_transient_failure_is_not_counted(ctx: JobContext) -> None:  # noqa: F811
    def ui(ids, since):
        raise PlatformError("threads: 댓글 칸 없음", "ui_changed")

    ctx.adapters["threads"].fetch_new_comments = ui
    for _ in range(3):
        run_reply(ctx)
        ctx.clock.advance(minutes=15)
    assert "threads" not in (ctx.repo.get_setting(FETCH_FAIL_KEY, {}) or {})


def test_new_setting_defaults_and_ranges() -> None:  # (e)
    s = Settings()
    assert s.threads_comment_interval_minutes == 15 and s.comment_lookback_days == 7
    for bad in (
        {"threads_comment_interval_minutes": 4},
        {"threads_comment_interval_minutes": 181},
        {"comment_lookback_days": 0},
        {"comment_lookback_days": 31},
    ):
        with pytest.raises(ValidationError):
            Settings(**bad)
    Settings(threads_comment_interval_minutes=5, comment_lookback_days=30)


def test_old_config_without_the_keys_gets_new_defaults() -> None:
    raw = yaml.safe_load((FIXTURES / "personas" / "sample.yaml").read_text("utf-8"))
    raw.setdefault("settings", {})
    for k in ("threads_comment_interval_minutes", "comment_lookback_days"):
        raw["settings"].pop(k, None)
    cfg = AppConfig.model_validate(raw)
    assert (
        cfg.settings.threads_comment_interval_minutes == 15
        and cfg.settings.comment_lookback_days == 7
    )


def test_slow_check_is_recorded_without_notification(ctx: JobContext) -> None:  # (f)  # noqa: F811
    real = ctx.adapters["threads"].fetch_new_comments

    def slow(ids, since):
        ctx.clock.advance(minutes=14)  # 2026-11-04처럼 한 번에 14분
        return real(ids, since)

    ctx.adapters["threads"].fetch_new_comments = slow
    before = list(ctx.notifier.calls)
    r = run_reply(ctx)
    assert r.counts["slow"] == ["댓글 확인이 14분 걸림(간격 5분) · 스레드 댓글 조회 14분"]
    assert "댓글 확인이 14분 걸림(간격 5분)" in r.summary()
    rows = [f for f in ctx.repo.recent_failures() if f["stage"] == "duration"]
    assert rows and rows[0]["platform"] == "threads" and rows[0]["action"] == "기록만(알림 없음)"
    assert not [c for c in ctx.notifier.calls[len(before) :] if "걸림" in c[0] + c[1]]


def test_normal_duration_is_not_recorded(ctx: JobContext) -> None:  # noqa: F811
    r = run_reply(ctx)
    assert "slow" not in r.counts
    assert not [f for f in ctx.repo.recent_failures() if f["stage"] == "duration"]


def test_lookback_is_seven_days(ctx: JobContext) -> None:  # noqa: F811
    seen: list = []
    real = ctx.adapters["threads"].fetch_new_comments
    ctx.adapters["threads"].fetch_new_comments = lambda ids, since: (
        seen.append(list(ids)),
        real(ids, since),
    )[1]
    run_reply(ctx)
    since = ctx.clock.now() - timedelta(days=7)
    expected = [
        p.external_id for p in ctx.repo.published_posts_since("threads", since) if p.external_id
    ]
    assert seen == [expected]


def test_other_transient_failures_do_not_count_and_break_the_streak(ctx: JobContext) -> None:  # noqa: F811
    """리뷰: Aside 꺼짐 같은 일시적 실패는 "게시물이 열리지 않아"가 아니다. 세지 않고 연속을 끊는다."""
    calls: list = []
    _failing(ctx, calls)
    run_reply(ctx)  # page 1회
    ctx.adapters["threads"].fetch_new_comments = lambda ids, since: (_ for _ in ()).throw(
        PlatformError("threads: Aside 결과 줄이 없음", "transient", "cli")
    )
    ctx.clock.advance(minutes=15)
    run_reply(ctx)
    assert "threads" not in (ctx.repo.get_setting(FETCH_FAIL_KEY, {}) or {})
    _failing(ctx, calls)
    ctx.clock.advance(minutes=15)
    run_reply(ctx)  # 다시 page 1회째: 쉬지 않는다
    assert _rest_notes(ctx) == []


def test_rest_is_written_as_the_action(ctx: JobContext) -> None:  # noqa: F811
    calls: list = []
    _failing(ctx, calls)
    run_reply(ctx)
    ctx.clock.advance(minutes=15)
    run_reply(ctx)
    actions = [f["action"] for f in ctx.repo.recent_failures() if f["stage"] == "fetch_comments"]
    assert actions[0] == "댓글 확인 30분 쉬기(연속 실패)" and actions[1] == "다음 댓글 확인에 다시"
