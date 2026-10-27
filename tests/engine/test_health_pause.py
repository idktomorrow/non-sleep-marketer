"""E4: 끄는 중엔 멈추지 않기, 일시적 실패는 연속일 때만 멈추기, 멈춤 이유 저장(incidents/0008, ADR 0054 개정)."""

from __future__ import annotations

import pytest

from nsm.adapters.platforms.aside_common import LoginRequiredError, PlatformError
from nsm.domain.models import HealthStatus
from nsm.engine.context import JobContext
from nsm.engine.health_metrics import run_health
from nsm.engine.pause import HEALTH_STREAK_KEY, PAUSE_INFO_KEY
from nsm.engine.publish import PAUSED_KEY
from nsm.engine.runner import INTERRUPTED_PREFIX, run_job
from nsm.store.repo import Repository
from tests.fakes import FakeNotifier


class _Health:
    """health()가 돌려줄 것을 테스트가 정한다: 예외면 던지고, 아니면 로그인 여부."""

    blocked_until = None

    def __init__(self, platform: str) -> None:
        self.platform = platform
        self.outcome: BaseException | bool = True

    def capabilities(self) -> set[str]:
        return set()

    def health(self) -> HealthStatus:
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return HealthStatus(logged_in=self.outcome, detail="로그인돼 있지 않음")


class _Interrupted(Exception):
    code = "interrupted"


@pytest.fixture
def ctx(db, clock, app_config) -> JobContext:
    return JobContext(
        config=app_config,
        repo=Repository(db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        adapters={"threads": _Health("threads"), "instagram": _Health("instagram")},
    )


def _paused(ctx: JobContext) -> list[str]:
    return ctx.repo.get_setting(PAUSED_KEY, [])


def test_shutdown_failure_does_not_pause_notify_or_record(ctx: JobContext) -> None:
    """0008: 끄는 순간 끊긴 점검은 멈춤·알림·실패 기록 없이 실행 기록에 '중단'만."""
    for a in ctx.adapters.values():
        a.outcome = PlatformError("threads: Aside 결과 줄이 없음", "transient")
    ctx.shutting_down = lambda: True
    result = run_job("health", ctx)
    assert _paused(ctx) == [] and ctx.notifier.calls == []
    assert ctx.repo.recent_failures() == []
    assert result.interrupted
    assert ctx.repo.recent_runs(1)[0]["message"].startswith(INTERRUPTED_PREFIX)


def test_interrupted_code_alone_skips_pause(ctx: JobContext) -> None:
    """Ctrl+C가 종료 플래그보다 먼저 브라우저 CLI를 끊어도(interrupted 코드) 멈추지 않는다."""
    ctx.adapters["threads"].outcome = _Interrupted("Ctrl+C")
    ctx.adapters["instagram"].outcome = False  # 끄는 중이 아니면 로그인 풀림은 그대로 멈춘다
    run_health(ctx)
    assert _paused(ctx) == ["instagram"]
    assert [c for c in ctx.notifier.calls if c[0].startswith("threads")] == []


def test_soft_logout_while_shutting_down_is_ignored(ctx: JobContext) -> None:
    ctx.adapters["threads"].outcome = False
    ctx.shutting_down = lambda: True
    run_health(ctx)
    assert _paused(ctx) == [] and ctx.notifier.calls == []


def test_transient_pauses_only_on_second_consecutive_failure(ctx: JobContext) -> None:
    a = ctx.adapters["threads"]
    a.outcome = PlatformError("threads: 시간 초과", "transient")
    first = run_health(ctx)
    assert _paused(ctx) == [] and ctx.notifier.calls == []
    assert first.counts["health_retry"] == ["스레드 1/2회"]
    rows = ctx.repo.recent_failures()
    assert len(rows) == 1 and rows[0]["kind"] == "transient"
    assert rows[0]["action"] == "상태 확인 실패(일시적), 다음 점검에서 다시 확인(1/2회)"
    run_health(ctx)  # 연속 2회째: 멈추고 이유대로 알린다
    assert _paused(ctx) == ["threads"]
    title, body, level = ctx.notifier.calls[-1]
    assert title == "threads 상태 확인 실패" and level == "error"
    assert "상태 확인 2회 연속 실패(일시적)로 자동 게시·답글을 멈췄습니다" in body
    assert "로그인" not in title + body.split("(")[0]
    info = ctx.repo.get_setting(PAUSE_INFO_KEY)["threads"]
    assert info["reason"] == "transient" and info["failures"] == 2 and info["since"]


def test_success_between_transient_failures_resets_streak(ctx: JobContext) -> None:
    a = ctx.adapters["threads"]
    a.outcome = PlatformError("threads: 시간 초과", "transient")
    run_health(ctx)
    a.outcome = True
    run_health(ctx)
    assert ctx.repo.get_setting(HEALTH_STREAK_KEY, {}) == {}
    a.outcome = PlatformError("threads: 시간 초과", "transient")
    run_health(ctx)
    assert _paused(ctx) == []  # 연속이 아니므로 다시 1회째


def test_pause_after_setting_is_respected(ctx: JobContext, app_config) -> None:
    app_config.settings.health_pause_after_failures = 1
    ctx.adapters["threads"].outcome = PlatformError("threads: 시간 초과", "transient")
    run_health(ctx)
    assert _paused(ctx) == ["threads"]


@pytest.mark.parametrize(
    "outcome",
    [LoginRequiredError("threads 로그인 필요: 로그인돼 있지 않음"), False],
    ids=["auth-exception", "soft-logout"],
)
def test_auth_pauses_immediately_with_reason(ctx: JobContext, outcome) -> None:
    ctx.adapters["threads"].outcome = outcome
    run_health(ctx)
    assert _paused(ctx) == ["threads"]
    assert ctx.notifier.calls[-1][0] == "threads 로그인 필요"
    assert ctx.repo.get_setting(PAUSE_INFO_KEY)["threads"]["reason"] == "auth"


def test_resume_clears_reason_and_reason_shows_in_run_summary(ctx: JobContext) -> None:
    ctx.adapters["threads"].outcome = False
    r = run_health(ctx)
    assert r.counts["paused_why"] == {"threads": "로그인 필요"}
    assert "스레드(로그인 필요)" in r.summary()
    ctx.adapters["threads"].outcome = True
    run_health(ctx)
    assert _paused(ctx) == [] and ctx.repo.get_setting(PAUSE_INFO_KEY, {}) == {}
    assert "다시 연결" in ctx.notifier.calls[-1][0]


def test_old_pause_flag_without_reason_still_works(ctx: JobContext) -> None:
    """E4 전에 저장된 멈춤(이유 없음)도 그대로 읽히고, 다시 연결되면 풀린다."""
    ctx.repo.set_setting(PAUSED_KEY, ["instagram"])
    r = run_health(ctx)  # 두 플랫폼 모두 정상 → 풀림
    assert _paused(ctx) == [] and "다시 연결" in ctx.notifier.calls[-1][0]
    ctx.repo.set_setting(PAUSED_KEY, ["instagram"])
    ctx.adapters["instagram"].outcome = False
    r = run_health(ctx)  # 이미 멈춤: 다시 알리지 않고 이유도 지어내지 않는다
    assert r.counts["paused_why"] == {"instagram": "이유 미기록"}
