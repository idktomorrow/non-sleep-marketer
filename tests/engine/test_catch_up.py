"""C40: 하루·주 한 번 잡 따라잡기(ADR 0066, incidents/0013). 가짜 시계·임시 DB·실제 JobScheduler(멈춘 채)로 본다."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from nsm.engine import runner
from nsm.engine.catch_up import (
    TRIED_KEY,
    caught_up_today,
    due_catch_ups,
    fixed_slots,
    queue_catch_ups,
    scheduled_at,
)
from nsm.engine.context import JobContext, JobResult
from nsm.engine.runner import INTERRUPTED_PREFIX, run_job
from nsm.scheduler.app import DEFAULT_GRACE, LONG_GRACE, JobScheduler
from nsm.store.db import to_db
from nsm.store.repo import Repository
from tests.conftest import KST
from tests.fakes import FakeNotifier, FixedClock

SAT_10 = datetime(2026, 11, 7, 10, 0, tzinfo=KST)  # 토요일 10:00: 03:10·04:30 지남, 20:00 전
M430 = datetime(2026, 11, 7, 4, 30, tzinfo=KST)  # 그날 놓친 백업 회차


@pytest.fixture
def clk() -> FixedClock:
    return FixedClock(SAT_10)


@pytest.fixture
def cctx(db, clk, app_config) -> JobContext:
    return JobContext(
        config=app_config,
        repo=Repository(db, clk),
        clock=clk,
        notifier=FakeNotifier(),
        adapters={},
    )


@pytest.fixture
def ran(monkeypatch) -> list[str]:
    """대상 잡 넷을 가짜로: 무엇이 돌았는지만 남긴다(진짜 백업·지표는 하지 않음)."""
    calls: list[str] = []
    for name in ("maintenance", "index", "metrics", "weekly_report"):

        def fake(ctx, name=name):
            calls.append(name)
            return JobResult(name, counts={"done": 1})

        monkeypatch.setitem(runner.JOBS, name, fake)
    return calls


def add_run(db, job: str, at: datetime, *, ok=True, message="끝", finished=True) -> None:
    db.execute(
        "INSERT INTO run_logs VALUES (?, ?, ?, ?, ?, ?)",
        [
            f"{job}-{at.isoformat()}",
            job,
            to_db(at),
            to_db(at + timedelta(minutes=1)) if finished else None,
            ok if finished else None,
            message if finished else None,
        ],
    )


class FakeSched:
    def __init__(self) -> None:
        self.calls: list[tuple[str, datetime, str]] = []

    def schedule_once(
        self, name: str, run_date: datetime, prefix: str, missed_at: datetime
    ) -> None:
        self.calls.append((name, run_date, prefix, missed_at))


def _due(cctx, boot: str | None = None) -> list[str]:
    return [j for j, _ in due_catch_ups(cctx.config, cctx.repo, cctx.clock.now(), boot)]


def _record_all_but(cctx, *missing: str) -> None:
    for job, slot in fixed_slots(cctx.config).items():
        if job not in missing:
            add_run(cctx.repo.db, job, scheduled_at(slot, cctx.clock.now(), "Asia/Seoul"))


# ------------------------------------------------------------------ (1)~(4) 놓침 기준


def test_before_scheduled_time_nothing_is_due(cctx, clk) -> None:
    clk.set(datetime(2026, 11, 7, 3, 0, tzinfo=KST))  # 03:10 전, 그리고 주간은 월요일(지남)
    _record_all_but(cctx, "metrics", "maintenance", "index")
    assert _due(cctx) == []
    clk.set(
        datetime(2026, 11, 7, 4, 40, tzinfo=KST)
    )  # 04:30 지났지만 지터(10분)+5분 안: 예약 실행을 기다림
    assert _due(cctx) == ["metrics"]


def test_missed_jobs_after_scheduled_time_only_targets(cctx) -> None:
    assert _due(cctx) == ["maintenance", "metrics", "weekly_report"]  # 설정 순서, 20:00 색인은 아직
    cctx.clock.set(datetime(2026, 11, 7, 21, 0, tzinfo=KST))
    assert "index" in _due(cctx)


def test_runs_since_one_hour_before_count_but_interrupted_or_unfinished_do_not(cctx) -> None:
    db = cctx.repo.db
    add_run(db, "maintenance", datetime(2026, 11, 7, 3, 35, tzinfo=KST))  # 04:30의 1시간 전 이후
    add_run(db, "metrics", datetime(2026, 11, 7, 2, 0, tzinfo=KST))  # 03:10의 1시간 전보다 이전
    add_run(db, "weekly_report", datetime(2026, 11, 2, 8, 5, tzinfo=KST), ok=False, message="실패")
    assert _due(cctx) == ["metrics"]  # 실패로 끝난 주간 리포트도 "돌았다"
    db.execute("DELETE FROM run_logs WHERE job = 'maintenance'")
    add_run(
        db,
        "maintenance",
        datetime(2026, 11, 7, 4, 35, tzinfo=KST),
        ok=False,
        message=f"{INTERRUPTED_PREFIX}KeyboardInterrupt: ",
    )
    add_run(
        db, "maintenance", datetime(2026, 11, 7, 4, 50, tzinfo=KST), finished=False
    )  # 실행 중 꺼짐
    assert _due(cctx) == ["maintenance", "metrics"]


def test_weekly_report_uses_this_weeks_monday_eight(cctx, clk) -> None:
    _record_all_but(cctx, "weekly_report")
    clk.set(datetime(2026, 11, 9, 7, 30, tzinfo=KST))  # 월요일 07:30: 아직 예정 전
    db = cctx.repo.db
    db.execute("DELETE FROM run_logs")
    add_run(db, "weekly_report", datetime(2026, 11, 2, 8, 5, tzinfo=KST))  # 지난주 것
    assert "weekly_report" not in _due(cctx)
    clk.set(datetime(2026, 11, 10, 9, 0, tzinfo=KST))  # 화요일: 이번 주 월요일 08:00을 놓침
    assert "weekly_report" in _due(cctx)
    assert scheduled_at(
        fixed_slots(cctx.config)["weekly_report"], clk.now(), "Asia/Seoul"
    ) == datetime(2026, 11, 9, 8, 0, tzinfo=KST)


def test_index_time_follows_setting(cctx, clk) -> None:
    clk.set(datetime(2026, 11, 7, 19, 0, tzinfo=KST))
    _record_all_but(cctx, "index")
    assert _due(cctx) == []
    cctx.config.settings.rag.index_time = "17:00"  # 설정을 바꾸면 다음 점검부터
    assert _due(cctx) == ["index"]


# ------------------------------------------------------------------ (5)·(7) 하루 한 번·1분 간격


def test_queue_spaces_jobs_one_minute_apart_with_prefix(cctx) -> None:
    s = FakeSched()
    queued = queue_catch_ups(s, cctx.config, cctx.repo, SAT_10, "boot-1")
    assert queued == ["maintenance", "metrics", "weekly_report"]
    assert [c[1] for c in s.calls] == [SAT_10 + timedelta(minutes=i) for i in range(3)]
    assert [c[2] for c in s.calls] == [
        "따라잡기(04:30 놓침) · ",
        "따라잡기(03:10 놓침) · ",
        "따라잡기(월 08:00 놓침) · ",
    ]
    rec = cctx.repo.get_setting(TRIED_KEY)
    assert rec == {"day": "2026-11-07", "boot": "boot-1", "jobs": queued}


def test_tried_today_is_not_requeued_even_if_it_failed(cctx, clk) -> None:
    s = FakeSched()
    queue_catch_ups(s, cctx.config, cctx.repo, SAT_10, "boot-1")
    clk.advance(
        minutes=30
    )  # 30분 뒤 점검: 실행이 실패했거나 아직 기다리는 중이어도 다시 띄우지 않는다
    assert queue_catch_ups(s, cctx.config, cctx.repo, clk.now(), "boot-1") == []
    add_run(cctx.repo.db, "maintenance", clk.now(), ok=False, message="따라잡기(04:30 놓침) · 실패")
    # 다음 기동: 실패로 끝난 것은 기록이 있어 안 띄우고, 실행 전에 꺼진 것(기록 없음)은 다시 띄운다
    assert queue_catch_ups(s, cctx.config, cctx.repo, clk.now(), "boot-2") == [
        "metrics",
        "weekly_report",
    ]
    clk.set(datetime(2026, 11, 8, 10, 0, tzinfo=KST))  # 다음 날은 새로 판단
    assert "maintenance" in queue_catch_ups(s, cctx.config, cctx.repo, clk.now(), "boot-2")


# ------------------------------------------------------------------ 실제 스케줄러 경로·접두어·(6) 끄는 중


@pytest.fixture
def sched(cctx):
    s = JobScheduler(lambda: cctx, set(runner.JOBS))
    s.scheduler.start(paused=True)  # 작업은 돌지 않게(예약만 본다)
    yield s
    s.shutdown()


def test_check_schedules_date_jobs_and_run_adds_prefix(cctx, sched, ran) -> None:
    assert sched.check_catch_up() == ["maintenance", "metrics", "weekly_report"]
    dates = sorted(
        j.next_run_time for j in sched.scheduler.get_jobs() if j.func == sched._run_catch_up
    )
    assert len(dates) == 3 and dates[1] - dates[0] == timedelta(minutes=1)
    assert sched.check_catch_up() == []  # 같은 기동·같은 날 두 번 띄우지 않는다
    sched._run_catch_up("maintenance", "따라잡기(04:30 놓침) · ", M430)
    assert ran == ["maintenance"]
    msg = cctx.repo.recent_runs(1)[0]["message"]
    assert msg.startswith("따라잡기(04:30 놓침) · ")
    assert cctx.notifier.calls == []  # 조용히: 알림 없음
    sched._run_catch_up("maintenance", "따라잡기(04:30 놓침) · ", M430)  # 그새 돌았으면 건너뜀
    assert ran == ["maintenance"]
    assert caught_up_today(cctx.repo, cctx.clock.now(), "Asia/Seoul") == ["maintenance"]


def test_shutting_down_does_nothing(cctx, sched, ran) -> None:
    cctx.shutting_down = lambda: True
    assert sched.check_catch_up() == []
    assert sched._run_catch_up("maintenance", "따라잡기(04:30 놓침) · ", M430) is None
    assert ran == [] and cctx.repo.get_setting(TRIED_KEY) is None


def test_interrupted_prefix_stays_first(cctx, monkeypatch) -> None:
    def boom(ctx):
        raise RuntimeError("끊김")

    monkeypatch.setitem(runner.JOBS, "maintenance", boom)
    cctx.shutting_down = lambda: True
    run_job("maintenance", cctx, prefix="따라잡기(04:30 놓침) · ")
    msg = cctx.repo.recent_runs(1)[0]["message"]
    assert msg.startswith(f"{INTERRUPTED_PREFIX}따라잡기(04:30 놓침) · RuntimeError")

    def stopped(ctx):
        return JobResult("maintenance", ok=False, errors=["x"], interrupted=True)

    monkeypatch.setitem(runner.JOBS, "maintenance", stopped)
    run_job("maintenance", cctx, prefix="따라잡기(04:30 놓침) · ")
    assert cctx.repo.recent_runs(1)[0]["message"].startswith(f"{INTERRUPTED_PREFIX}따라잡기(")
    # 다음 기동에서 다시 따라잡을 수 있다(중단은 "돌았다"가 아님)
    cctx.shutting_down = lambda: False
    assert "maintenance" in _due(cctx, "boot-9")


def test_failed_catch_up_still_notifies_like_any_failed_job(cctx, monkeypatch) -> None:
    def boom(ctx):
        raise RuntimeError("디스크 가득")

    monkeypatch.setitem(runner.JOBS, "maintenance", boom)
    run_job("maintenance", cctx, prefix="따라잡기(04:30 놓침) · ")
    assert (
        cctx.repo.recent_runs(1)[0]["message"] == "따라잡기(04:30 놓침) · RuntimeError: 디스크 가득"
    )


# ------------------------------------------------------------------ (8) 빈 목록이면 꺼짐


def test_empty_list_turns_catch_up_off(cctx, sched) -> None:
    cctx.config.settings.catch_up_jobs = []
    assert _due(cctx) == [] and sched.check_catch_up() == []


def test_settings_form_checkboxes(app_config) -> None:
    from nsm.app.settings_form import apply_settings_form, form_values

    form = form_values(app_config)
    assert all(
        form[f"catch_up_{j}"] == "on" for j in ("maintenance", "index", "metrics", "weekly_report")
    )
    off = {k: v for k, v in form.items() if not k.startswith("catch_up_")} | {"catch_up_form": "1"}
    assert apply_settings_form(app_config, off).settings.catch_up_jobs == []
    some = off | {"catch_up_index": "on", "catch_up_maintenance": "on"}
    assert apply_settings_form(app_config, some).settings.catch_up_jobs == ["maintenance", "index"]
    old = {k: v for k, v in form.items() if not k.startswith("catch_up_")}  # 칸이 없는 옛 폼
    assert (
        apply_settings_form(app_config, old).settings.catch_up_jobs
        == app_config.settings.catch_up_jobs
    )


def test_settings_page_saves_all_unchecked(db, clock, app_config, tmp_path) -> None:
    from fastapi.testclient import TestClient

    from nsm.app.server import AppState, create_app
    from nsm.app.settings_form import form_values
    from tests.app.test_dashboard import _run_now

    saved = {}
    state = AppState(
        repo=Repository(db, clock),
        clock=clock,
        get_config=lambda: saved.get("cfg", app_config),
        set_config=lambda c: saved.update(cfg=c),
        submit=_run_now,
        adapters={},
        media_dir=tmp_path / "media",
    )
    client = TestClient(create_app(state))
    page = client.get("/settings").text
    assert 'name="catch_up_maintenance" checked' in page and 'name="catch_up_form"' in page
    data = {k: v for k, v in form_values(app_config).items() if not k.startswith("catch_up_")}
    data |= {"catch_up_form": "1", "csrf": state.csrf_token}
    data = {k: v for k, v in data.items() if v != ""}  # 브라우저는 끈 체크 칸을 보내지 않는다
    r = client.post("/settings", data=data, follow_redirects=False)
    assert r.status_code == 303 and saved["cfg"].settings.catch_up_jobs == []


# ------------------------------------------------------------------ (9) 유예 시간·잡 ID


def test_long_grace_only_for_daily_jobs(app_config) -> None:
    s = JobScheduler(lambda: None, set(runner.JOBS))
    installed = s.install(app_config)
    assert not any(
        i.startswith("catch_up") for i in installed
    )  # 따라잡기 점검은 따로(install_catch_up)
    s.scheduler.start(paused=True)
    try:
        grace = {j.id: j.misfire_grace_time for j in s.scheduler.get_jobs()}
        for name in ("maintenance", "index", "metrics", "weekly_report"):
            assert grace[name] == LONG_GRACE == 10800
        for name in ("health", "publish", "reply", "generate", "collect#0"):
            assert grace[name] == DEFAULT_GRACE == 900
        assert s.install_catch_up() == ["catch_up#start", "catch_up"]
        start = s.scheduler.get_job("catch_up#start").next_run_time
        assert (
            timedelta(seconds=60) <= start - datetime.now().astimezone() <= timedelta(seconds=120)
        )
        assert "0:30:00" in str(s.scheduler.get_job("catch_up").trigger)
    finally:
        s.shutdown()


def test_triggers_keep_their_times(app_config) -> None:
    from nsm.scheduler.app import job_triggers

    t = {k: str(v) for k, v in job_triggers(app_config).items()}
    assert "hour='4'" in t["maintenance"] and "minute='30'" in t["maintenance"]
    assert "hour='3'" in t["metrics"] and "minute='10'" in t["metrics"]
    assert "day_of_week='mon'" in t["weekly_report"] and "hour='8'" in t["weekly_report"]
    assert t["index"] == "cron[hour='20', minute='0']"  # 매일(요일 제한 없음)


def test_overview_shows_caught_up_line(db, app_config, tmp_path) -> None:
    from fastapi.testclient import TestClient

    from nsm.app.server import AppState, create_app
    from tests.app.test_dashboard import _run_now

    clk = FixedClock(SAT_10)
    repo = Repository(db, clk)
    state = AppState(
        repo=repo,
        clock=clk,
        get_config=lambda: app_config,
        set_config=lambda c: None,
        submit=_run_now,
        adapters={},
        media_dir=tmp_path / "media",
    )
    client = TestClient(create_app(state))
    assert "오늘 따라잡은 일" not in client.get("/").text
    add_run(
        db,
        "maintenance",
        datetime(2026, 11, 7, 7, 1, tzinfo=KST),
        message="따라잡기(04:30 놓침) · 백업 1개",
    )
    add_run(
        db,
        "index",
        datetime(2026, 11, 7, 7, 2, tzinfo=KST),
        message="따라잡기(20:00 놓침) · 조각 3개",
    )
    add_run(
        db,
        "metrics",
        datetime(2026, 11, 7, 7, 3, tzinfo=KST),
        ok=False,
        message="따라잡기(03:10 놓침) · 실패",
    )
    add_run(
        db,
        "metrics",
        datetime(2026, 11, 6, 7, 3, tzinfo=KST),
        message="따라잡기(03:10 놓침) · 어제",
    )
    html = client.get("/").text
    assert "오늘 따라잡은 일: 백업·정리·문서 색인" in html  # 실패·어제 것은 빼고


# ------------------------------------------------------------------ 리뷰 반영: 자정 근처·건너뜀·유예·폼 다시 보이기


def test_slot_near_midnight_is_caught_up_after_midnight(cctx, clk) -> None:
    """23:55 예정이면 기다림(지터 10분+5분)이 다음 날 00:10까지 넘어간다. 그 회차는 다음 날에 따라잡는다."""
    _record_all_but(cctx, "index")
    cctx.config.settings.rag.index_time = "23:55"
    add_run(cctx.repo.db, "index", datetime(2026, 11, 6, 23, 58, tzinfo=KST))  # 어제 회차는 돌았음
    clk.set(datetime(2026, 11, 7, 23, 59, tzinfo=KST))
    assert "index" not in _due(cctx)  # 오늘 23:55 회차는 아직 기다리는 중
    clk.set(datetime(2026, 11, 8, 0, 20, tzinfo=KST))
    [at] = [a for j, a in due_catch_ups(cctx.config, cctx.repo, clk.now()) if j == "index"]
    assert at == datetime(2026, 11, 7, 23, 55, tzinfo=KST)
    add_run(cctx.repo.db, "index", datetime(2026, 11, 8, 0, 21, tzinfo=KST))
    clk.set(datetime(2026, 11, 8, 9, 0, tzinfo=KST))
    assert "index" not in _due(cctx)


def test_run_queued_before_midnight_still_runs_after_midnight(cctx, clk, sched, ran) -> None:
    clk.set(datetime(2026, 11, 7, 23, 59, tzinfo=KST))
    _record_all_but(cctx, "index")
    queued = sched.check_catch_up()
    assert queued == ["index"]
    clk.set(datetime(2026, 11, 8, 0, 1, tzinfo=KST))  # 자정을 넘겨 집힘
    sched._run_catch_up(
        "index", "따라잡기(20:00 놓침) · ", datetime(2026, 11, 7, 20, 0, tzinfo=KST)
    )
    assert ran == ["index"]


def test_skipped_catch_up_is_not_listed_as_caught_up(cctx) -> None:
    add_run(cctx.repo.db, "index", SAT_10, message="따라잡기(20:00 놓침) · 건너뜀: Gemini 키 없음")
    assert caught_up_today(cctx.repo, SAT_10, "Asia/Seoul") == []


def test_catch_up_check_has_no_misfire_limit() -> None:
    s = JobScheduler(lambda: None, set())
    s.install_catch_up()
    s.scheduler.start(paused=True)
    try:
        assert s.scheduler.get_job("catch_up").misfire_grace_time is None
    finally:
        s.shutdown()


def test_form_error_keeps_unchecked_catch_up_boxes(db, clock, app_config, tmp_path) -> None:
    from fastapi.testclient import TestClient

    from nsm.app.server import AppState, create_app
    from nsm.app.settings_form import form_values
    from tests.app.test_dashboard import _run_now

    state = AppState(
        repo=Repository(db, clock),
        clock=clock,
        get_config=lambda: app_config,
        set_config=lambda c: None,
        submit=_run_now,
        adapters={},
        media_dir=tmp_path / "media",
    )
    client = TestClient(create_app(state))
    data = {k: v for k, v in form_values(app_config).items() if not k.startswith("catch_up_")}
    data |= {"catch_up_form": "1", "catch_up_index": "on", "csrf": state.csrf_token}
    data["daily_cost_cap_usd"] = "9999"  # 틀린 값: 저장하지 않고 다시 보여 준다
    data = {k: v for k, v in data.items() if v != ""}
    r = client.post("/settings", data=data)
    assert r.status_code == 400
    assert 'name="catch_up_index" checked' in r.text
    assert 'name="catch_up_maintenance" checked' not in r.text
