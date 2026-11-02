"""E5: 로그인 세부 상태에 따른 멈춤 정보·알림(24시간 억제, 상태 변화 시 재알림)과 지표 잡의 멈춘 플랫폼 건너뛰기."""

from __future__ import annotations

import pytest

from nsm.adapters.platforms.aside_common import PlatformError
from nsm.domain.models import HealthStatus
from nsm.engine.context import JobContext
from nsm.engine.health_metrics import run_health, run_metrics
from nsm.engine.pause import PAUSE_INFO_KEY, pause_notice, reason_text, short_reason, todo_text
from nsm.engine.publish import PAUSED_KEY
from nsm.store.repo import Repository
from tests.fakes import FakeNotifier, FakePlatformAdapter


class _Health(FakePlatformAdapter):
    """health가 로그인 안 됨이고 세부 상태(auth_state)를 들고 있는 어댑터."""

    def __init__(self, platform: str, clock) -> None:
        super().__init__(platform, clock=clock)
        self.auth_state: str | None = None
        self.error: BaseException | None = None
        self.metrics_calls = 0

    def health(self) -> HealthStatus:
        if self.error:
            raise self.error
        if self.auth_state is None:
            return HealthStatus(logged_in=True, detail="@me")
        return HealthStatus(logged_in=False, detail="인스타그램 로그인 필요(Aside 브라우저)")

    def read_account_metrics(self):
        self.metrics_calls += 1
        return super().read_account_metrics()


@pytest.fixture
def ctx(db, clock, app_config) -> JobContext:
    return JobContext(
        config=app_config,
        repo=Repository(db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        adapters={p: _Health(p, clock) for p in ("threads", "instagram")},
    )


def _info(ctx: JobContext) -> dict:
    return ctx.repo.get_setting(PAUSE_INFO_KEY)["instagram"]


def test_challenge_pauses_with_state_and_own_words(ctx: JobContext) -> None:
    ctx.adapters["instagram"].auth_state = "challenge"
    r = run_health(ctx)
    assert ctx.repo.get_setting(PAUSED_KEY) == ["instagram"]
    info = _info(ctx)
    assert info["reason"] == "auth" and info["auth_state"] == "challenge"
    assert info["notified_at"] == info["since"]
    title, body, level = ctx.notifier.calls[-1]
    assert title == "instagram 본인 확인 창" and level == "error"
    assert "인스타그램이 본인 확인 창을 띄웠습니다" in body and "자동으로 풀지 않습니다" in body
    assert r.counts["paused_why"] == {"instagram": "본인 확인 창"}


def test_same_state_is_not_repeated_within_24h_then_reminded(ctx: JobContext, clock) -> None:
    a = ctx.adapters["instagram"]
    a.auth_state = "verify"
    run_health(ctx)
    failures = len(ctx.repo.recent_failures())
    for _ in range(3):  # 30분마다 점검해도 같은 상태면 조용히
        clock.advance(minutes=30)
        run_health(ctx)
    assert len(ctx.notifier.calls) == 1
    assert len(ctx.repo.recent_failures()) == failures  # 실패 기록도 다시 쌓지 않는다
    assert _info(ctx)["failures"] == 1
    clock.advance(hours=23)  # 첫 알림부터 24시간 넘음
    run_health(ctx)
    title, body, level = ctx.notifier.calls[-1]
    assert len(ctx.notifier.calls) == 2 and title.endswith("(계속)") and level == "warn"
    assert "문자·이메일 인증을 요구합니다" in body
    assert _info(ctx)["notified_at"] == clock.now().isoformat()
    clock.advance(hours=1)
    run_health(ctx)
    assert len(ctx.notifier.calls) == 2  # 다시 24시간을 센다


def test_state_change_updates_info_and_notifies_at_once(ctx: JobContext, clock) -> None:
    a = ctx.adapters["instagram"]
    a.auth_state = "challenge"
    run_health(ctx)
    since = _info(ctx)["since"]
    clock.advance(minutes=30)
    a.auth_state = "verify"  # 본인 확인을 넘기자 문자 인증(2026-10-29~30 실제 순서)
    run_health(ctx)
    info = _info(ctx)
    assert info["auth_state"] == "verify" and info["since"] == since  # 멈춘 시각은 그대로
    assert info["notified_at"] == clock.now().isoformat()
    title, body, _ = ctx.notifier.calls[-1]
    assert title == "instagram 문자·이메일 인증 요구" and "다음 점검(30분 이내)에 자동으로" in body
    assert ctx.repo.recent_failures()[0]["action"] == "멈춤 유지(로그인 상태가 바뀜)"
    a.auth_state = "logged_out"
    clock.advance(minutes=30)
    run_health(ctx)
    assert _info(ctx)["auth_state"] == "logged_out" and len(ctx.notifier.calls) == 3


def test_transient_check_while_paused_keeps_the_reason(ctx: JobContext, clock) -> None:
    a = ctx.adapters["instagram"]
    a.auth_state = "restricted"
    run_health(ctx)
    a.error = PlatformError("instagram: 페이지가 덜 열림(빈 화면, 로그인 여부 모름)", "transient")
    clock.advance(minutes=30)
    run_health(ctx)
    assert _info(ctx)["reason"] == "auth" and _info(ctx)["auth_state"] == "restricted"
    assert len(ctx.notifier.calls) == 1


def test_login_again_resumes(ctx: JobContext, clock) -> None:
    a = ctx.adapters["instagram"]
    a.auth_state = "verify"
    run_health(ctx)
    a.auth_state = None
    clock.advance(minutes=30)
    run_health(ctx)
    assert ctx.repo.get_setting(PAUSED_KEY) == [] and "다시 연결" in ctx.notifier.calls[-1][0]
    assert ctx.repo.get_setting(PAUSE_INFO_KEY) == {}


def test_metrics_skips_paused_platform(ctx: JobContext) -> None:
    """10-30·10-31·11-01 03:25 증거는 모두 지표 잡이 멈춘 인스타그램을 열어 남긴 것이었다."""
    ctx.repo.set_setting(PAUSED_KEY, ["instagram"])
    r = run_metrics(ctx)
    assert ctx.adapters["instagram"].metrics_calls == 0
    assert ctx.adapters["threads"].metrics_calls == 1
    assert r.counts["paused_skipped"] == ["instagram"]
    assert ctx.repo.recent_failures() == []


@pytest.mark.parametrize(
    ("state", "short", "todo"),
    [
        ("logged_out", "로그인 풀림", "다시 로그인하면 풀림"),
        ("challenge", "본인 확인 창", "직접 본인 확인을 끝내면 풀림"),
        ("verify", "문자·이메일 인증 요구", "인증을 마치면 풀림"),
        ("two_factor", "두 단계 인증 요구", "인증 코드를 넣으면 풀림"),
        ("restricted", "계정 제한", "인스타그램 앱에서 계정 상태를 확인"),
        ("unknown", "로그인 필요(원인 모름)", "화면을 확인하고"),
    ],
)
def test_words_per_state(state: str, short: str, todo: str) -> None:
    info = {"reason": "auth", "auth_state": state, "since": "2026-10-29T09:00:00+09:00"}
    assert short_reason(info) == short
    assert todo in todo_text(info, "instagram")
    assert todo in reason_text(info, None, "instagram")
    title, body = pause_notice("instagram", info, "detail")
    assert title == f"instagram {short}" and body.endswith("(detail)")


def test_old_auth_pause_without_state_keeps_old_words() -> None:
    info = {"reason": "auth"}
    assert short_reason(info) == "로그인 필요" and todo_text(info) == ""
    assert pause_notice("instagram", info, "d")[0] == "instagram 로그인 필요"


def test_particles_follow_the_platform_name() -> None:
    info = {"reason": "auth", "auth_state": "challenge"}
    assert "인스타그램이 본인 확인 창을" in pause_notice("instagram", info, "d")[1]
    assert "스레드가 본인 확인 창을" in pause_notice("threads", info, "d")[1]
    unknown = {"reason": "auth", "auth_state": "unknown"}
    assert "스레드를 열어" in pause_notice("threads", unknown, "d")[1]
    assert reason_text({"reason": "auth", "auth_state": "challenge"}).startswith(
        "본인 확인 창으로 "
    )
    assert reason_text({"reason": "auth", "auth_state": "verify"}).startswith(
        "문자·이메일 인증 요구로 "
    )


def test_pause_from_before_e5_is_not_reminded_right_away(ctx: JobContext, clock) -> None:
    """리뷰: E5 전 멈춤(notified_at 없음)은 올리자마자 '(계속)'을 보내지 않고, 일시적 멈춤엔 재알림이 없다."""
    ctx.repo.set_setting(PAUSED_KEY, ["instagram", "threads"])
    old = "2026-10-01T09:00:00+09:00"
    ctx.repo.set_setting(
        PAUSE_INFO_KEY,
        {
            "instagram": {"reason": "auth", "auth_state": "challenge", "since": old},
            "threads": {"reason": "ui_changed", "since": old},
        },
    )
    ctx.adapters["instagram"].auth_state = "challenge"
    ctx.adapters["threads"].error = PlatformError("threads: 버튼 없음", "ui_changed")
    run_health(ctx)
    assert ctx.notifier.calls == []
    clock.advance(hours=25)
    run_health(ctx)
    assert [c[0] for c in ctx.notifier.calls] == ["instagram 본인 확인 창 (계속)"]


def test_switch_to_account_mismatch_is_a_change(ctx: JobContext, clock) -> None:
    a = ctx.adapters["instagram"]
    a.auth_state = "challenge"
    run_health(ctx)
    a.auth_state = None
    a.health = lambda: HealthStatus(logged_in=False, detail="로그인 계정 @other ≠ 설정 @me")  # type: ignore[method-assign]
    clock.advance(minutes=30)
    run_health(ctx)
    info = _info(ctx)
    assert "auth_state" not in info and "≠ 설정" in info["detail"]
    assert ctx.notifier.calls[-1][0] == "instagram 로그인 필요"
