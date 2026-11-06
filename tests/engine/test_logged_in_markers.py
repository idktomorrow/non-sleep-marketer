"""C39: 로그인은 됐는데 계정 이름을 못 읽음(화면 변경)은 멈추지 않는다(ADR 0064, incidents/0012).

health 잡: 어댑터가 logged_in=True + ui_warning이면 멈춤 없음, 멈춰 있던 플랫폼은 풀림, 화면 변경 알림은 플랫폼별
6시간에 한 번, 장애 기록에 ui_changed 1건(증거 폴더 포함). 댓글 확인이 화면 변경으로 실패해도 플랫폼을 멈추지 않는다.
"""

from __future__ import annotations

import pytest

from nsm.adapters.platforms.aside_common import PlatformError
from nsm.domain.models import HealthStatus
from nsm.engine.context import JobContext
from nsm.engine.health_metrics import run_health
from nsm.engine.pause import PAUSE_INFO_KEY
from nsm.engine.publish import PAUSED_KEY, UI_CHANGED_NOTIFIED_KEY
from nsm.engine.reply import FETCH_FAIL_KEY, run_reply
from nsm.store.repo import Repository
from tests.fakes import FakeNotifier, FakePlatformAdapter

SHOTS = "C:/x/logs/shots/threads-20261106-090000"


class _Warn(FakePlatformAdapter):
    """health가 "로그인은 됐는데 계정 이름을 못 읽음"을 돌려주는 어댑터(AsideAdapterBase._health와 같은 모양)."""

    def __init__(self, platform: str, clock) -> None:
        super().__init__(platform, clock=clock)
        self.ui_warning: dict | None = None

    def health(self) -> HealthStatus:
        if self.ui_warning:
            return HealthStatus(logged_in=True, detail=self.ui_warning["detail"])
        return HealthStatus(logged_in=True, detail="@me")


@pytest.fixture
def hctx(db, clock, app_config) -> JobContext:
    return JobContext(
        config=app_config,
        repo=Repository(db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        adapters={p: _Warn(p, clock) for p in ("threads", "instagram")},
    )


def _warn(ctx: JobContext, evidence: str | None = SHOTS) -> None:
    ctx.adapters["threads"].ui_warning = {
        "detail": "스레드 로그인은 됐으나 계정 이름을 못 읽음(화면 변경)",
        "evidence_dir": evidence,
    }


def _ui_notes(ctx: JobContext) -> list:
    return [c for c in ctx.notifier.calls if c[0] == "threads 화면 변경"]


def test_ui_warning_does_not_pause_and_notifies_once(hctx: JobContext) -> None:
    ctx = hctx
    _warn(ctx)
    r = run_health(ctx)
    assert ctx.repo.get_setting(PAUSED_KEY, []) == []
    assert r.counts["ui_changed"] == ["스레드 계정 이름 못 읽음(화면 변경)"]
    [(title, body, level)] = _ui_notes(ctx)
    assert level == "error"
    assert body.startswith("threads 화면 변경: 로그인은 돼 있지만 계정 이름을 읽지 못합니다.")
    assert "게시·답글은 계정 확인이 될 때까지 건너뜁니다" in body and SHOTS in body
    fails = [f for f in ctx.repo.recent_failures() if f["kind"] == "ui_changed"]
    assert len(fails) == 1
    assert fails[0]["platform"] == "threads" and fails[0]["job"] == "health"
    assert fails[0]["evidence_dir"] == SHOTS
    # 30분마다 다시 봐도 6시간 안에는 알림·기록을 더하지 않는다(멈추지도 않는다)
    for _ in range(3):
        ctx.clock.advance(minutes=30)
        r = run_health(ctx)
        assert r.counts["ui_changed"] and ctx.repo.get_setting(PAUSED_KEY, []) == []
    assert len(_ui_notes(ctx)) == 1
    assert len([f for f in ctx.repo.recent_failures() if f["kind"] == "ui_changed"]) == 1
    ctx.clock.advance(hours=5)  # 첫 알림부터 6시간 넘음
    run_health(ctx)
    assert len(_ui_notes(ctx)) == 2


def test_ui_warning_unpauses_a_paused_platform(hctx: JobContext) -> None:
    """0012처럼 "로그인 필요"로 멈춰 있던 플랫폼: 표지로 로그인이 확인되면 푼다."""
    ctx = hctx
    ctx.repo.set_setting(PAUSED_KEY, ["threads"])
    ctx.repo.set_setting(
        PAUSE_INFO_KEY,
        {"threads": {"reason": "auth", "auth_state": "unknown", "since": "2026-11-05T11:56:00"}},
    )
    _warn(ctx)
    r = run_health(ctx)
    assert ctx.repo.get_setting(PAUSED_KEY, []) == [] and "paused_why" not in r.counts
    assert "threads" not in (ctx.repo.get_setting(PAUSE_INFO_KEY, {}) or {})
    titles = [c[0] for c in ctx.notifier.calls]
    assert "threads 다시 연결됨" in titles and "threads 화면 변경" in titles
    back = next(c for c in ctx.notifier.calls if c[0] == "threads 다시 연결됨")
    assert (
        "게시·답글은 계정 확인이 될 때까지 하지 않습니다" in back[1]
    )  # "다시 시작"이라고 하지 않는다


def test_no_warning_no_ui_record(hctx: JobContext) -> None:
    ctx = hctx
    r = run_health(ctx)
    assert "ui_changed" not in r.counts and not ctx.notifier.calls
    assert not [f for f in ctx.repo.recent_failures() if f["kind"] == "ui_changed"]


def test_warning_without_evidence_folder(hctx: JobContext) -> None:
    ctx = hctx
    _warn(ctx, evidence=None)  # 증거는 한 시간에 한 번이라 없을 수 있다
    run_health(ctx)
    [(_, body, _)] = _ui_notes(ctx)
    assert "증거 폴더" not in body


def test_health_and_publish_share_the_six_hour_clock(hctx: JobContext) -> None:
    """같은 플랫폼의 화면 변경 알림은 게시·health 어디서 나든 6시간에 한 번(같은 키)."""
    ctx = hctx
    ctx.repo.set_setting(UI_CHANGED_NOTIFIED_KEY, {"threads": ctx.clock.now().isoformat()})
    _warn(ctx)
    run_health(ctx)
    assert _ui_notes(ctx) == []


# ------------------------------------------------------------------ 댓글 확인: 화면 변경은 그 회차만 실패


def test_reply_fetch_ui_changed_records_and_does_not_pause(ctx: JobContext) -> None:

    def ui(ids, since):
        raise PlatformError(
            "threads: 로그인은 됐으나 계정 이름을 못 읽음(화면 변경)", "ui_changed", "ui"
        )

    ctx.adapters["threads"].fetch_new_comments = ui
    r = run_reply(ctx)
    assert any("계정 이름을 못 읽음" in e for e in r.errors)
    assert ctx.repo.get_setting(PAUSED_KEY, []) == []  # 플랫폼은 멈추지 않는다
    assert "threads" not in (
        ctx.repo.get_setting(FETCH_FAIL_KEY, {}) or {}
    )  # 연속 쉬기에도 안 센다
    fails = [f for f in ctx.repo.recent_failures() if f["job"] == "reply"]
    assert fails and fails[0]["kind"] == "ui_changed"
    ctx.clock.advance(minutes=15)
    ctx.adapters["threads"].fetch_new_comments = lambda ids, since: []
    assert run_reply(ctx).ok  # 다음 회차는 그대로 확인
