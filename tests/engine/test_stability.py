"""경계 1: 안정화 (배치 C12) — 계속 0건 알림, 데이터 백업, 실행 기록 정리.

2026-10-13(네이버 뉴스 구조 변경)·10-14(인스타그램 429)에 수집·게시가 오류 없이 0건으로 돌았다.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import duckdb
import pytest

from nsm.engine.context import JobContext
from nsm.engine.health_metrics import run_health
from nsm.engine.maintenance import run_maintenance
from nsm.engine.publish import PAUSED_KEY
from nsm.engine.runner import JOBS
from nsm.engine.watchdog import STALLS_KEY, check_stalls
from nsm.scheduler.app import job_triggers
from nsm.store.db import Database
from nsm.store.repo import Repository
from tests.fakes import FakeNotifier, FakePlatformAdapter, FakeTextGenerator, load_source_items


def _ctx(db, clock, app_config, **kw) -> JobContext:
    return JobContext(
        config=app_config,
        repo=Repository(db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        adapters={p: FakePlatformAdapter(p, clock=clock) for p in ("threads", "instagram")},
        **kw,
    )


def _run(ctx: JobContext, job: str, msg: str = "ok", ok: bool = True) -> None:
    rid = ctx.repo.start_run(job)
    ctx.repo.finish_run(rid, ok, msg)


def _items(ctx: JobContext, relevance: float) -> None:
    for item in load_source_items()[:3]:
        ctx.repo.insert_source_item(item)
        ctx.repo.set_source_relevance(item.id, relevance)


def _waiting(ctx: JobContext, platform: str = "instagram") -> None:
    from nsm.domain.models import Content, ContentBrief

    gen = FakeTextGenerator().generate_content(
        ContentBrief(topic="핸드드립", template="info", sources=load_source_items()[:1]),
        ctx.config.persona,
        ctx.config.brand,
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
    ctx.repo.create_publications("c1", [platform])


# --- 계속 0건 -----------------------------------------------------------------------


def test_collect_running_but_nothing_relevant_is_a_stall(db, clock, app_config) -> None:
    ctx = _ctx(db, clock, app_config)
    _items(ctx, relevance=0.1)
    _run(ctx, "collect")
    clock.advance(hours=12)
    _run(ctx, "collect")
    stalls = check_stalls(ctx)
    assert stalls and stalls[0].startswith("자료 모으기 2일째 관련 자료 0개")
    assert ctx.repo.get_setting(STALLS_KEY) == stalls
    assert [t for t, _, _ in ctx.notifier.calls] == ["계속 0건"]
    check_stalls(ctx)  # 같은 날 같은 내용은 다시 알리지 않는다
    assert len(ctx.notifier.calls) == 1
    clock.advance(days=1)
    _run(ctx, "collect")
    check_stalls(ctx)  # 다음 날에는 한 번 더
    assert len(ctx.notifier.calls) == 2


def test_no_stall_when_relevant_found_or_collect_barely_ran(db, clock, app_config) -> None:
    ctx = _ctx(db, clock, app_config)
    _run(ctx, "collect")  # PC가 꺼져 있었던 경우: 한 번만 돌았다
    assert check_stalls(ctx) == []
    _run(ctx, "collect")
    _run(ctx, "collect", "건너뜀: 활동 시간 밖")  # 건너뛴 실행은 세지 않는다
    _items(ctx, relevance=0.95)
    assert check_stalls(ctx) == []


def test_waiting_post_not_published_for_days_is_a_stall(db, clock, app_config) -> None:
    ctx = _ctx(db, clock, app_config)
    _waiting(ctx)
    assert check_stalls(ctx) == []  # 방금 승인한 원고는 아직 문제 아님
    clock.advance(days=3)
    stalls = check_stalls(ctx)
    assert stalls == [
        "인스타그램 2일째 게시 0건(올릴 원고 1개 대기): 실행 기록에서 게시하기가 안 올린 이유를 확인하세요"
    ]
    ctx.repo.set_setting(PAUSED_KEY, ["instagram"])  # 로그인 멈춤은 따로 알린다
    assert check_stalls(ctx) == []


def test_persona_off_is_not_a_stall(db, clock, app_config) -> None:
    persona = app_config.persona.model_copy(update={"enabled": False})
    ctx = _ctx(db, clock, app_config.model_copy(update={"persona": persona}))
    _waiting(ctx)
    clock.advance(days=3)
    assert check_stalls(ctx) == []


def test_health_job_reports_stalls(db, clock, app_config) -> None:
    ctx = _ctx(db, clock, app_config)
    _waiting(ctx)
    clock.advance(days=3)
    r = run_health(ctx)
    assert r.counts["stalls"] and "인스타그램 2일째 게시 0건" in r.summary()
    assert r.summary().startswith("로그인 정상 · ")


# --- 백업·실행 기록 정리 ----------------------------------------------------------------


@pytest.fixture
def file_db(tmp_path: Path):
    d = Database(tmp_path / "data.duckdb")
    yield d
    d.close()


def test_daily_backup_is_a_full_copy_and_keeps_the_newest(
    file_db, clock, app_config, tmp_path
) -> None:
    backups = tmp_path / "backups"
    ctx = _ctx(file_db, clock, app_config, backups_dir=backups)
    _items(ctx, relevance=0.9)
    for _ in range(9):
        r = run_maintenance(ctx)
        clock.advance(days=1)
    files = sorted(p.name for p in backups.glob("data-*.duckdb"))
    assert len(files) == 7 and files[-1] == "data-2026-10-19.duckdb"  # 최근 7개
    assert r.counts["backups_removed"] == 1 and not list(backups.glob("*.tmp"))
    copy = duckdb.connect(str(backups / files[-1]), read_only=True)
    try:
        assert copy.execute("SELECT count(*) FROM source_items").fetchone()[0] == 3
    finally:
        copy.close()
    assert r.summary().startswith("데이터 백업 data-2026-10-19.duckdb(")


def test_same_day_backup_overwrites(file_db, clock, app_config, tmp_path) -> None:
    ctx = _ctx(file_db, clock, app_config, backups_dir=tmp_path / "b")
    run_maintenance(ctx)
    run_maintenance(ctx)  # "지금 실행"을 또 눌러도 하루 한 개
    assert len(list((tmp_path / "b").glob("data-*.duckdb"))) == 1


def test_old_run_logs_are_pruned_only(db, clock, app_config) -> None:
    ctx = _ctx(db, clock, app_config)  # 메모리 DB·백업 폴더 없음: 백업은 건너뛴다
    _run(ctx, "reply")
    _items(ctx, relevance=0.9)
    clock.advance(days=91)
    _run(ctx, "reply")
    r = run_maintenance(ctx)
    assert r.counts == {
        "runs_pruned": 1,
        "sources_blanked": 0,
        "sources_pruned": 0,
        "evidence_pruned": 0,
    }
    assert len(ctx.repo.recent_runs()) == 1  # 방금 것만 남음(maintenance 자신은 runner가 적는다)
    assert ctx.repo.relevant_since(clock.now() - timedelta(days=365), 0.5) == 3  # 자료는 그대로
    assert r.summary() == "오래된 실행 기록 1줄 정리"


def test_maintenance_is_scheduled_daily(app_config) -> None:
    assert "maintenance" in JOBS and "hour='4'" in str(job_triggers(app_config)["maintenance"])


# --- 리뷰 반영 -----------------------------------------------------------------------


def test_changing_waiting_count_does_not_notify_again(db, clock, app_config) -> None:
    """문장 속 대기 수가 바뀌어도 같은 종류(인스타그램 게시)는 하루 한 번."""
    ctx = _ctx(db, clock, app_config)
    _waiting(ctx)
    clock.advance(days=3)
    check_stalls(ctx)
    ctx.repo.db.execute("UPDATE publications SET content_id = 'c2'")  # 다른 원고처럼
    ctx.repo.create_publications("c1", ["instagram"])
    ctx.repo.db.execute(
        "UPDATE publications SET created_at = ?", [ctx.repo._now() - timedelta(days=3)]
    )  # noqa: SLF001
    assert "올릴 원고 2개" in check_stalls(ctx)[0]
    assert [t for t, _, _ in ctx.notifier.calls].count("계속 0건") == 1


def test_cleared_stall_notifies_again_same_day(db, clock, app_config) -> None:
    ctx = _ctx(db, clock, app_config)
    _run(ctx, "collect")
    _run(ctx, "collect")
    check_stalls(ctx)
    _items(ctx, relevance=0.95)  # 풀림
    assert check_stalls(ctx) == []
    ctx.repo.db.execute("UPDATE source_items SET relevance = 0.1")  # 다시 걸림(같은 날)
    check_stalls(ctx)
    assert [t for t, _, _ in ctx.notifier.calls].count("계속 0건") == 2


def test_known_reasons_are_not_stalls(db, clock, app_config) -> None:
    """하루 한도 0, 요청 제한으로 쉬는 중은 이미 이유가 보이므로 계속 0건으로 겹쳐 알리지 않는다."""
    s = app_config.settings.model_copy(update={"quotas": {"instagram": 0, "threads": 2}})
    ctx = _ctx(db, clock, app_config.model_copy(update={"settings": s}))
    _waiting(ctx)
    clock.advance(days=3)
    assert check_stalls(ctx) == []
    ctx.config = app_config
    ctx.adapters["instagram"].blocked_until = clock.now() + timedelta(hours=2)
    assert check_stalls(ctx) == []


def test_failed_collect_runs_are_not_counted(db, clock, app_config) -> None:
    """수집이 실패하면 "잡 실패" 알림이 따로 간다. 사이트 화면 탓으로 잘못 짚지 않는다."""
    ctx = _ctx(db, clock, app_config)
    _run(ctx, "collect", "RuntimeError: network", ok=False)
    _run(ctx, "collect", "RuntimeError: network", ok=False)
    assert check_stalls(ctx) == []


def test_watchdog_error_does_not_hide_login_check(db, clock, app_config, monkeypatch) -> None:
    import nsm.engine.health_metrics as hm

    def boom(ctx):
        raise RuntimeError("db busy")

    monkeypatch.setattr(hm, "check_stalls", boom)
    ctx = _ctx(db, clock, app_config)
    r = run_health(ctx)
    assert r.counts["paused"] == [] and "계속 0건 확인 실패" in r.errors[0]


def test_failed_backup_still_prunes_and_leaves_no_temp(
    file_db, clock, app_config, tmp_path, monkeypatch
) -> None:
    import os

    ctx = _ctx(file_db, clock, app_config, backups_dir=tmp_path / "b")
    _run(ctx, "reply")
    clock.advance(days=91)

    def locked(src, dst):
        raise PermissionError("파일이 열려 있음")

    monkeypatch.setattr(os, "replace", locked)
    with pytest.raises(PermissionError):
        run_maintenance(ctx)
    assert ctx.repo.recent_runs() == []  # 정리는 먼저 됐다
    assert not list((tmp_path / "b").glob("*.tmp"))


def test_todays_backup_survives_even_if_date_goes_back(
    file_db, clock, app_config, tmp_path
) -> None:
    s = app_config.settings.model_copy(update={"backup_keep": 1})
    ctx = _ctx(
        file_db, clock, app_config.model_copy(update={"settings": s}), backups_dir=tmp_path / "b"
    )
    clock.advance(days=1)
    run_maintenance(ctx)
    clock.advance(days=-1)  # 시간대·시계가 바뀌어 날짜가 뒤로
    r = run_maintenance(ctx)
    assert [p.name for p in (tmp_path / "b").glob("*.duckdb")] == [r.counts["backup"]]


def test_backup_name_includes_preset_for_non_default(file_db, clock, app_config, tmp_path) -> None:
    """C29: 프리셋 백업은 `data-<프리셋>-날짜`로, 기본 백업과 섞이거나 덮어쓰지 않는다."""
    backups = tmp_path / "b"
    ctx = _ctx(file_db, clock, app_config, backups_dir=backups)
    run_maintenance(ctx)
    cafe = app_config.model_copy(
        update={"settings": app_config.settings.model_copy(update={"preset": "카페 브랜드"})}
    )
    ctx.config = cafe
    r = run_maintenance(ctx)
    names = sorted(p.name for p in backups.glob("*.duckdb"))
    assert len(names) == 2 and any(n.startswith("data-카페-브랜드-") for n in names)
    assert r.counts["backup"].startswith("data-카페-브랜드-") and r.counts["backups_removed"] == 0
