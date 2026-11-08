"""C41: 게시 감시가 폐기·검토 대기 원고의 게시 줄을 세지 않고, 올릴 원고가 0개면 이유를 사실대로 알린다.
폐기할 때·유지보수 때 남은 대기 줄을 포기로 정리한다(incidents/0014)."""

from __future__ import annotations

from datetime import timedelta

from nsm.engine.maintenance import run_maintenance
from nsm.engine.watchdog import check_stalls
from nsm.store.db import to_db
from tests.engine.test_stability import _ctx, _run, _waiting


def _published(ctx, platform: str, days_ago: float, cid: str = "old") -> None:
    t = to_db(ctx.clock.now() - timedelta(days=days_ago))
    ctx.repo.db.execute(
        "INSERT INTO publications (id, content_id, platform, status, published_at, created_at) "
        "VALUES (?, ?, ?, 'published', ?, ?)",
        [f"{cid}-{platform}", cid, platform, t, t],
    )


def _run_at(ctx, job: str, days_ago: float, msg: str = "ok", ok: bool = True) -> None:
    ctx.clock.advance(days=-days_ago)
    _run(ctx, job, msg, ok)
    ctx.clock.advance(days=days_ago)


def _status(ctx, cid: str) -> dict[str, str]:
    return dict(
        ctx.repo.db.execute("SELECT platform, status FROM publications WHERE content_id = ?", [cid])
    )


# ------------------------------------------------------------------ (4) 감시


def test_discarded_and_pending_drafts_are_not_waiting(db, clock, app_config) -> None:
    ctx = _ctx(db, clock, app_config)
    _waiting(ctx, "instagram", cid="c1")
    _waiting(ctx, "threads", cid="c2")
    # C41 전에 폐기된 원고처럼 대기 줄이 남은 것, 검토 대기로 돌아간 원고
    db.execute("UPDATE contents SET status = 'discarded' WHERE id = 'c1'")
    db.execute("UPDATE contents SET status = 'pending_review' WHERE id = 'c2'")
    _published(ctx, "instagram", 5)
    _published(ctx, "threads", 5)
    clock.advance(days=3)
    _run_at(ctx, "generate", 0.5)  # 원고 만들기는 돌고 있다
    texts = check_stalls(ctx)
    assert not any("올릴 원고" in t for t in texts)
    [t] = texts
    assert t.startswith("인스타그램·스레드 2일째 게시 0건: 승인된 원고가 없고 검토 대기 원고 1개")


def test_fresh_install_is_quiet(db, clock, app_config) -> None:
    ctx = _ctx(db, clock, app_config)
    clock.advance(days=5)
    assert check_stalls(ctx) == [] and ctx.notifier.calls == []


def test_generate_not_running_is_one_line_with_last_run(db, clock, app_config) -> None:
    ctx = _ctx(db, clock, app_config)
    _published(ctx, "instagram", 4)
    _published(ctx, "threads", 4)
    _run_at(ctx, "generate", 4)  # 4일 전 00:50이 마지막
    texts = check_stalls(ctx)
    [t] = texts  # 두 플랫폼이어도 한 줄
    last = (clock.now() - timedelta(days=4)).strftime("%m-%d %H:%M")
    assert t.startswith(
        f"원고 만들기 2일째 안 돎(마지막 실행 {last}), 그래서 인스타그램·스레드 게시 0건"
    )
    assert "절전" in t
    [(title, body, _)] = ctx.notifier.calls
    assert title == "계속 0건" and body.count("안 돎") == 1
    check_stalls(ctx)  # 같은 날 다시 알리지 않는다
    assert len(ctx.notifier.calls) == 1


def test_generate_never_run_but_posts_existed(db, clock, app_config) -> None:
    ctx = _ctx(db, clock, app_config)
    _published(ctx, "instagram", 4)
    texts = check_stalls(ctx)
    assert any("원고 만들기 2일째 안 돎(마지막 실행 기록 없음)" in t for t in texts)


def test_generate_ran_but_only_skipped(db, clock, app_config) -> None:
    ctx = _ctx(db, clock, app_config)
    _published(ctx, "instagram", 4)
    _published(ctx, "threads", 4)
    _run_at(ctx, "generate", 0.5, msg="건너뜀: 쓸 자료 없음(관련 자료 수집 필요)")
    [t] = check_stalls(ctx)
    assert t.startswith(
        "인스타그램·스레드 2일째 게시 0건: 원고 만들기는 돌았지만 올릴 새 원고가 없습니다"
    )


def test_approved_waiting_still_counts(db, clock, app_config) -> None:
    ctx = _ctx(db, clock, app_config)
    _waiting(ctx, "instagram")
    clock.advance(days=3)
    assert any("올릴 원고 1개 대기" in t for t in check_stalls(ctx))


# ------------------------------------------------------------------ (5) 폐기하면 대기 줄은 포기


def test_discard_abandons_queued_and_failed_rows(db, clock, app_config) -> None:
    ctx = _ctx(db, clock, app_config)
    _waiting(ctx, "instagram", cid="c1")
    ctx.repo.create_publications("c1", ["threads"])
    db.execute(
        "UPDATE publications SET status = 'failed' WHERE content_id = 'c1' AND platform = 'threads'"
    )
    _published(ctx, "naver_blog", 1, cid="c1")  # 이미 올라간 줄은 그대로
    ctx.repo.transition_content("c1", "discarded", reason="사람이 폐기")
    assert _status(ctx, "c1") == {
        "instagram": "abandoned",
        "threads": "abandoned",
        "naver_blog": "published",
    }
    err = db.execute(
        "SELECT last_error FROM publications WHERE content_id = 'c1' AND platform = 'instagram'"
    )
    assert err[0][0] == "원고 폐기"


def test_other_transitions_leave_rows_alone(db, clock, app_config) -> None:
    ctx = _ctx(db, clock, app_config)
    _waiting(ctx, "instagram", cid="c1")
    ctx.repo.transition_content("c1", "pending_review", reason="화면 변경")
    assert _status(ctx, "c1") == {"instagram": "queued"}


# ------------------------------------------------------------------ (6) 유지보수가 예전 찌꺼기 정리


def test_maintenance_cleans_leftovers_of_discarded_drafts(db, clock, app_config) -> None:
    ctx = _ctx(db, clock, app_config)
    _waiting(ctx, "instagram", cid="c1")
    _waiting(ctx, "threads", cid="c2")
    _waiting(ctx, "threads", cid="c3")
    db.execute(
        "UPDATE contents SET status = 'discarded' WHERE id IN ('c1', 'c2')"
    )  # 예전에 폐기된 것
    res = run_maintenance(ctx)
    assert res.counts["leftover_pubs"] == 2
    assert _status(ctx, "c1") == {"instagram": "abandoned"}
    assert _status(ctx, "c3") == {"threads": "queued"}  # 승인된 원고는 그대로
    assert "폐기된 원고의 게시 대기 2줄 정리" in res.summary()
    again = run_maintenance(ctx)
    assert "leftover_pubs" not in again.counts  # 다음부터는 생기지 않는다


# ------------------------------------------------------------------ 리뷰 반영


def test_recently_approved_draft_is_not_reported_as_no_drafts(db, clock, app_config) -> None:
    """며칠 꺼져 있다가 오늘 원고가 승인돼 기다리면(게시 시간 전) "새 원고 없음"이라고 하지 않는다."""
    ctx = _ctx(db, clock, app_config)
    _published(ctx, "instagram", 5)
    _published(ctx, "threads", 5)
    _run_at(ctx, "generate", 0.1)
    _waiting(ctx, "instagram", cid="new1")  # 방금 승인(감시 기간보다 새것)
    texts = check_stalls(ctx)
    assert not any(t.startswith("인스타그램") for t in texts)
    assert texts == [
        "스레드 2일째 게시 0건: 원고 만들기는 돌았지만 올릴 새 원고가 없습니다. "
        "실행 기록에서 원고 만들기가 건너뛰거나 실패한 이유(쓸 자료 없음·비용 상한 등)를 확인하세요"
    ]


def test_failed_generate_counts_as_ran(db, clock, app_config) -> None:
    ctx = _ctx(db, clock, app_config)
    _published(ctx, "instagram", 4)
    _run_at(ctx, "generate", 0.5, msg="RuntimeError: 연결", ok=False)
    texts = check_stalls(ctx)
    assert not any("안 돎" in t for t in texts) and any(
        "건너뛰거나 실패한 이유" in t for t in texts
    )
