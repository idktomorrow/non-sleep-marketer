"""C41: 자료 모으기·원고 만들기 따라잡기(ADR 0066 개정 1, incidents/0014). 가짜 시계·임시 DB·멈춘 JobScheduler."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from nsm.config.schema import CATCH_UP_CHOICES, Settings
from nsm.config.store import parse_windows
from nsm.engine import runner
from nsm.engine.catch_up import (
    CATCH_UP_JOBS,
    TRIED_KEY,
    activity_slots,
    due_catch_ups,
    queue_catch_ups,
)
from nsm.engine.collect import run_collect
from nsm.engine.context import JobContext
from nsm.engine.generate import run_generate
from nsm.scheduler.app import JobScheduler, job_triggers
from nsm.store.repo import Repository
from tests.conftest import KST
from tests.engine.test_catch_up import FakeSched, add_run
from tests.fakes import FakeNotifier, FixedClock


@pytest.fixture
def actx(db, app_config) -> JobContext:
    clk = FixedClock(datetime(2026, 11, 8, 21, 0, tzinfo=KST))  # 일요일 21:00
    return JobContext(
        config=app_config,
        repo=Repository(db, clk),
        clock=clk,
        notifier=FakeNotifier(),
        adapters={},
    )


def _all_day(cfg) -> None:
    cfg.persona.active_hours = parse_windows("00:00-23:59")


# ------------------------------------------------------------------ (1) 예정 시각은 스케줄러와 같은 계산


def test_activity_slots_match_scheduler_triggers(app_config) -> None:
    two = activity_slots(app_config)  # 창 두 개: 09:00-12:00, 18:00-23:00
    assert [(s.hour, s.minute) for s in two["collect"]] == [(9, 20), (18, 20)]
    assert [(s.hour, s.minute) for s in two["generate"]] == [(9, 50)]
    t = {k: str(v) for k, v in job_triggers(app_config).items()}
    assert set(k for k in t if k.startswith("collect")) == {"collect#0", "collect#1"}
    assert "hour='9', minute='20'" in t["collect#0"] and "hour='18', minute='20'" in t["collect#1"]
    assert "hour='9', minute='50'" in t["generate"]
    one = app_config.model_copy(deep=True)
    _all_day(one)
    slots = activity_slots(one)
    assert [(s.hour, s.minute) for s in slots["collect"]] == [(0, 20)]  # 창이 하나면 하루 1회
    assert [(s.hour, s.minute) for s in slots["generate"]] == [(0, 50)]
    assert set(job_triggers(one)) >= {"collect#0", "generate"} and "collect#1" not in job_triggers(
        one
    )


# ------------------------------------------------------------------ (2) 순서·간격·하루 2회 수집


def test_after_midnight_order_is_fixed_and_one_minute_apart(actx) -> None:
    _all_day(actx.config)
    actx.config.settings.catch_up_jobs = list(reversed(CATCH_UP_CHOICES))  # 저장 순서와 무관
    s = FakeSched()
    queued = queue_catch_ups(s, actx.config, actx.repo, actx.clock.now(), "b1")
    assert (
        queued
        == list(CATCH_UP_JOBS)
        == [
            "collect",
            "generate",
            "maintenance",
            "metrics",
            "index",
            "weekly_report",
        ]
    )
    now = actx.clock.now()
    assert [c[1] for c in s.calls] == [now + timedelta(minutes=i) for i in range(6)]
    assert [c[2] for c in s.calls][:2] == ["따라잡기(00:20 놓침) · ", "따라잡기(00:50 놓침) · "]


def test_collect_twice_a_day_catches_only_latest_then_evening(actx) -> None:
    clk = actx.clock
    clk.set(datetime(2026, 11, 8, 12, 0, tzinfo=KST))  # 09:20 회차만 지남
    s = FakeSched()
    assert "collect" in queue_catch_ups(s, actx.config, actx.repo, clk.now(), "b1")
    assert s.calls[0][3] == datetime(2026, 11, 8, 9, 20, tzinfo=KST)
    add_run(actx.repo.db, "collect", clk.now(), message="따라잡기(09:20 놓침) · 자료 3건")
    clk.set(datetime(2026, 11, 8, 15, 0, tzinfo=KST))
    assert "collect" not in queue_catch_ups(s, actx.config, actx.repo, clk.now(), "b1")
    clk.set(datetime(2026, 11, 8, 19, 0, tzinfo=KST))  # 같은 기동: 저녁 18:20 회차도 놓침
    s2 = FakeSched()
    assert "collect" in queue_catch_ups(s2, actx.config, actx.repo, clk.now(), "b1")
    assert s2.calls[0][2] == "따라잡기(18:20 놓침) · "
    keys = actx.repo.get_setting(TRIED_KEY)["jobs"]
    assert {k for k in keys if k.startswith("collect@")} == {
        "collect@2026-11-08T09:20:00+09:00",
        "collect@2026-11-08T18:20:00+09:00",
    }


def test_both_collect_slots_missed_catches_one(actx) -> None:
    actx.clock.set(datetime(2026, 11, 8, 20, 0, tzinfo=KST))  # 09:20·18:20 둘 다 놓침
    due = dict(due_catch_ups(actx.config, actx.repo, actx.clock.now()))
    assert due["collect"] == datetime(2026, 11, 8, 18, 20, tzinfo=KST)  # 가장 최근 회차 하나


def test_persona_off_skips_collect_and_generate(actx) -> None:
    actx.config.persona.enabled = False
    jobs = [j for j, _ in due_catch_ups(actx.config, actx.repo, actx.clock.now())]
    assert "collect" not in jobs and "generate" not in jobs and "maintenance" in jobs


# ------------------------------------------------------------------ (3) 오늘 목표를 채웠으면 새 원고 없음


def test_generate_catch_up_does_not_exceed_daily_target(ctx: JobContext, monkeypatch) -> None:
    """따라잡기로 돌아도 원고 만들기는 오늘 목표만큼만: 늦게 돌려도 하루 비용은 같다."""
    from nsm.domain.policy import daily_content_target

    run_collect(ctx)
    run_generate(ctx)
    first = ctx.repo.list_contents()[0]
    st = ctx.config.settings
    target = daily_content_target(st.enabled_platforms, st.quotas)
    for i in range(target - ctx.repo.count_contents()):  # 오늘 목표를 채운다(자료가 모자라도)
        ctx.repo.insert_content(first.model_copy(update={"id": f"fill{i}"}))
    made = ctx.repo.count_contents()
    assert made >= target
    sched = JobScheduler(lambda: ctx, set(runner.JOBS))
    now = ctx.clock.now()
    at = now.replace(hour=0, minute=50, second=0, microsecond=0)
    result = sched._run_catch_up("generate", "따라잡기(00:50 놓침) · ", at)
    assert result is not None and result.skipped == "오늘 생성 목표 달성"
    assert ctx.repo.count_contents() == made
    assert (
        ctx.repo.recent_runs(1)[0]["message"]
        == "따라잡기(00:50 놓침) · 건너뜀: 오늘 생성 목표 달성"
    )


# ------------------------------------------------------------------ (7) 설정: 칸 여섯·예전 설정 파일


def test_settings_form_has_six_checkboxes(app_config) -> None:
    from nsm.app.settings_form import apply_settings_form, form_values

    form = form_values(app_config)
    assert [k for k in form if k.startswith("catch_up_")] == [
        f"catch_up_{j}" for j in CATCH_UP_CHOICES
    ]
    assert all(form[f"catch_up_{j}"] == "on" for j in CATCH_UP_CHOICES)
    data = {k: v for k, v in form.items() if not k.startswith("catch_up_")} | {
        "catch_up_form": "1",
        "catch_up_generate": "on",
        "catch_up_maintenance": "on",
    }
    assert apply_settings_form(app_config, data).settings.catch_up_jobs == [
        "generate",
        "maintenance",
    ]


def test_settings_page_shows_six_boxes(db, clock, app_config, tmp_path) -> None:
    from fastapi.testclient import TestClient

    from nsm.app.server import AppState, create_app
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
    html = TestClient(create_app(state)).get("/settings").text
    for j in CATCH_UP_CHOICES:
        assert f'name="catch_up_{j}" checked' in html


def test_old_config_file_gets_new_boxes_turned_on() -> None:
    old = Settings.model_validate(
        {"catch_up_jobs": ["maintenance", "index", "metrics", "weekly_report"]}
    )
    assert old.catch_up_jobs == [
        "collect",
        "generate",
        "maintenance",
        "index",
        "metrics",
        "weekly_report",
    ]
    assert old.catch_up_offered == list(CATCH_UP_CHOICES)
    some = Settings.model_validate({"catch_up_jobs": ["maintenance"]})
    assert some.catch_up_jobs == ["collect", "generate", "maintenance"]  # 새 칸은 켠 채로
    off = Settings.model_validate({"catch_up_jobs": []})
    assert off.catch_up_jobs == []  # 따라잡기를 모두 꺼 둔 파일은 그대로
    # 0.4.27에서 저장한 파일(칸 여섯을 본 뒤 끈 것)은 다시 켜지 않는다
    now = Settings.model_validate(
        {"catch_up_jobs": ["maintenance"], "catch_up_offered": list(CATCH_UP_CHOICES)}
    )
    assert now.catch_up_jobs == ["maintenance"]
    dumped = Settings.model_validate(now.model_dump())
    assert dumped.catch_up_jobs == ["maintenance"]


def test_morning_collect_is_not_caught_up_right_before_evening_run(actx) -> None:
    """저녁 회차(18:20)가 곧 돌거나 지터로 도는 중이면 오전 회차를 따라잡지 않는다(두 번 연달아 모으지 않게)."""
    for hm in ((17, 30), (18, 30)):
        actx.clock.set(datetime(2026, 11, 8, *hm, tzinfo=KST))
        assert "collect" not in [
            j for j, _ in due_catch_ups(actx.config, actx.repo, actx.clock.now())
        ]
    actx.clock.set(datetime(2026, 11, 8, 16, 0, tzinfo=KST))  # 저녁 회차까지 1시간 넘게 남음
    assert "collect" in [j for j, _ in due_catch_ups(actx.config, actx.repo, actx.clock.now())]
