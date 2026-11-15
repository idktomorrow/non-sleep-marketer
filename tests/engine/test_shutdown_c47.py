"""C47: Ctrl+C 종료 지연과 중단 기록.

- 끄는 중에 줄 서 있던 잡은 시작하지 않고 "중단"으로 남긴다(JobScheduler._run).
- 지표 읽기는 끄는 중이면 다음 글·계정 지표·다음 플랫폼으로 가지 않고 "중단"으로 끝난다(알림 없음).
- 끌 때 브라우저 닫기를 기다리다 시간이 지나도 스택트레이스 ERROR 없이 WARNING 한 줄, 닫기는 마무리된다.
"""

from __future__ import annotations

import logging
import threading
from contextlib import ExitStack

import pytest

from nsm.adapters.platforms.aside_common import PlatformError
from nsm.engine import runner
from nsm.engine.context import JobContext, JobResult
from nsm.engine.health_metrics import run_metrics
from nsm.engine.runner import INTERRUPTED_PREFIX, run_job
from nsm.runtime import close_on_worker
from nsm.scheduler.app import SKIPPED_ON_SHUTDOWN, JobScheduler
from nsm.store.repo import Repository
from tests.engine.test_health_metrics import _published
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


# ------------------------------------------------------------------ 1. 끄는 중 시작 안 함


def test_queued_job_does_not_start_while_shutting_down(ctx, monkeypatch) -> None:
    ran: list[str] = []
    monkeypatch.setitem(runner.JOBS, "reply", lambda c: ran.append("reply") or JobResult("reply"))
    sched = JobScheduler(lambda: ctx, {"reply"})
    ctx.shutting_down = lambda: True
    r = sched._run("reply")
    assert ran == [] and r.ok is False and r.interrupted
    row = ctx.repo.recent_runs(1)[0]
    assert row["job"] == "reply" and row["ok"] is False
    assert row["message"] == SKIPPED_ON_SHUTDOWN == f"{INTERRUPTED_PREFIX}시작 전 건너뜀"
    assert not ctx.notifier.calls
    # 실행 기록 배지가 "중단"으로 본다(C33 규칙)
    from nsm.app.run_view import job_statuses

    repo = ctx.repo
    statuses = job_statuses(
        ctx.config, repo.latest_runs(), repo.last_success_at(), ctx.clock.now(), str
    )
    st = {s.job: s for s in statuses}
    assert st["reply"].state == "interrupted"


def test_job_runs_normally_when_not_shutting_down(ctx, monkeypatch) -> None:
    ran: list[str] = []
    monkeypatch.setitem(runner.JOBS, "reply", lambda c: ran.append("reply") or JobResult("reply"))
    JobScheduler(lambda: ctx, {"reply"})._run("reply")
    assert ran == ["reply"] and ctx.repo.recent_runs(1)[0]["ok"] is True


# ------------------------------------------------------------------ 2. 지표 읽기 중단


def _count_accounts(ctx) -> list[str]:
    calls: list[str] = []
    for p, a in ctx.adapters.items():
        orig = a.read_account_metrics

        def wrapped(p=p, orig=orig):
            calls.append(p)
            return orig()

        a.read_account_metrics = wrapped
    return calls


def test_metrics_stop_after_current_post_when_shutting_down(ctx, app_config) -> None:
    for n in range(3):
        _published(ctx, app_config, n, 1, platform="threads")
    _published(ctx, app_config, 9, 1, platform="instagram")
    stop = threading.Event()
    ctx.shutting_down = stop.is_set
    posts: list[str] = []
    orig = ctx.adapters["threads"].read_post_metrics

    def read(ext: str):
        posts.append(ext)
        stop.set()  # 첫 글을 읽는 동안 Ctrl+C
        return orig(ext)

    ctx.adapters["threads"].read_post_metrics = read
    insta: list[str] = []
    ctx.adapters["instagram"].read_post_metrics = lambda ext: insta.append(ext)
    accounts = _count_accounts(ctx)
    run_job("metrics", ctx)
    assert len(posts) == 1 and insta == [] and accounts == []  # 다음 글·계정 지표·다음 플랫폼 없음
    row = ctx.repo.recent_runs(1)[0]
    assert row["ok"] is False and row["message"].startswith(INTERRUPTED_PREFIX)
    assert "오류" not in row["message"]
    assert "끄는 중이라 멈춤(남은 곳: 인스타그램)" in row["message"]
    assert not ctx.notifier.calls


def test_metrics_interrupted_error_is_not_incident_or_error(ctx, app_config) -> None:
    _published(ctx, app_config, 0, 1, platform="threads")
    stop = threading.Event()
    ctx.shutting_down = stop.is_set

    def cut(ext: str):
        stop.set()
        raise PlatformError("끊김", "transient", "interrupted")

    ctx.adapters["threads"].read_post_metrics = cut
    accounts = _count_accounts(ctx)
    r = run_metrics(ctx)
    assert r.interrupted and r.ok is False and r.errors == []
    assert accounts == []
    assert ctx.repo.db.execute("SELECT count(*) FROM failures")[0][0] == 0
    assert not ctx.notifier.calls


def test_metrics_cut_while_reading_account_is_interrupted(ctx) -> None:
    stop = threading.Event()
    ctx.shutting_down = stop.is_set

    def cut():
        stop.set()
        raise PlatformError("끊김", "transient", "interrupted")

    accounts = _count_accounts(ctx)  # 다른 플랫폼의 계정 지표를 읽나 센다
    ctx.adapters["threads"].read_account_metrics = cut  # 스레드는 읽다 끊긴다
    r = run_metrics(ctx)
    assert r.interrupted and r.errors == [] and accounts == []
    assert r.counts["stopped_note"] == "끄는 중이라 멈춤(남은 곳: 인스타그램)"


def test_metrics_do_nothing_when_already_shutting_down(ctx) -> None:
    ctx.shutting_down = lambda: True
    accounts = _count_accounts(ctx)
    r = run_metrics(ctx)
    assert accounts == [] and r.interrupted and not r.ok
    assert r.counts["stopped_note"] == "끄는 중이라 멈춤(남은 곳: 스레드, 인스타그램)"


def test_stopped_note_leaves_out_paused_platforms(ctx) -> None:
    from nsm.engine.publish import PAUSED_KEY

    ctx.repo.set_setting(PAUSED_KEY, ["instagram"])
    ctx.shutting_down = lambda: True
    assert run_metrics(ctx).counts["stopped_note"] == "끄는 중이라 멈춤(남은 곳: 스레드)"


def test_metrics_without_shutdown_unchanged(ctx) -> None:
    r = run_metrics(ctx)
    assert r.ok and not r.interrupted and r.counts == {"posts": 0, "accounts": 2}


# ------------------------------------------------------------------ 3. 끌 때 브라우저 닫기


def _scheduler() -> JobScheduler:
    s = JobScheduler(lambda: None, set())  # type: ignore[arg-type,return-value]
    s.start()
    return s


def test_close_waits_and_logs_nothing_when_quick(caplog) -> None:
    sched = _scheduler()
    drivers = ExitStack()
    closed: list[str] = []
    drivers.callback(lambda: closed.append(threading.current_thread().name))
    with caplog.at_level(logging.INFO, logger="nsm.runtime"):
        close_on_worker(sched, drivers, wait_s=5)
    assert len(closed) == 1 and closed[0] != threading.current_thread().name  # 작업 스레드에서
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not sched.scheduler.running


def test_close_timeout_is_one_warning_and_close_still_finishes(caplog) -> None:
    """돌던 잡이 기다림보다 길면 WARNING 한 줄만. 닫기는 shutdown(wait=True)이 끝까지 기다려 마무리한다."""
    sched = _scheduler()
    release = threading.Event()
    started = threading.Event()

    def long_job() -> None:
        started.set()
        release.wait(10)

    sched.submit(long_job)
    assert started.wait(5)
    drivers = ExitStack()
    closed: list[bool] = []
    drivers.callback(lambda: closed.append(True))
    threading.Timer(2.0, release.set).start()  # 기다림(0.3초)보다 넉넉히 늦게
    with caplog.at_level(logging.INFO, logger="nsm.runtime"):
        close_on_worker(sched, drivers, wait_s=0.3)
    assert closed == [True]
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warns] == [
        "브라우저 닫기가 0초 안에 끝나지 않아 기다리지 않고 종료"
    ]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR or r.exc_info]
    assert any(r.getMessage() == "브라우저 닫기 끝(돌던 작업이 끝난 뒤)" for r in caplog.records)


def test_close_error_is_still_logged_as_error(caplog) -> None:
    sched = _scheduler()
    drivers = ExitStack()

    def boom() -> None:
        raise RuntimeError("닫다가 오류")

    drivers.callback(boom)
    with caplog.at_level(logging.INFO, logger="nsm.runtime"):
        close_on_worker(sched, drivers, wait_s=5)
    assert any(
        r.levelno == logging.ERROR and "브라우저 종료 실패" in r.getMessage()
        for r in caplog.records
    )


def test_close_timeout_error_from_close_itself_is_error(caplog) -> None:
    """닫기 자체가 TimeoutError를 내면 기다림 초과가 아니라 닫기 실패(3.12에서 둘은 같은 예외 종류)."""
    sched = _scheduler()
    drivers = ExitStack()

    def slow_fail() -> None:
        raise TimeoutError("브라우저가 응답 없음")

    drivers.callback(slow_fail)
    with caplog.at_level(logging.INFO, logger="nsm.runtime"):
        close_on_worker(sched, drivers, wait_s=5)
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    assert not [r for r in caplog.records if "안에 끝나지 않아" in r.getMessage()]


def test_close_when_scheduler_never_started() -> None:
    """켜는 중 실패로 스케줄러가 시작도 안 했으면 기다리지 않고 바로 닫는다."""
    import time

    sched = JobScheduler(lambda: None, set())  # type: ignore[arg-type,return-value]
    drivers = ExitStack()
    closed: list[bool] = []
    drivers.callback(lambda: closed.append(True))
    t = time.monotonic()
    close_on_worker(sched, drivers, wait_s=30)
    assert closed == [True] and time.monotonic() - t < 5


def test_unknown_job_while_shutting_down_still_raises(ctx) -> None:
    ctx.shutting_down = lambda: True
    with pytest.raises(KeyError):
        JobScheduler(lambda: ctx, set())._run("bogus")
    assert ctx.repo.recent_runs(5) == []
