"""추천 슬롯 저장소(S14 제안, ADR 0057): 계획·축소·claim·실패→재시도 복귀 단계·재시작 복구·마이그레이션."""

from __future__ import annotations

from datetime import date, timedelta

from nsm.store.db import Database
from nsm.store.slots import MAX_ATTEMPTS, SlotStore

D = date(2026, 10, 11)


def test_plan_is_idempotent_and_lowering_cancels_only_unstarted(db, clock) -> None:
    s = SlotStore(db, clock)
    assert s.plan(D, 2, 1, "기본") == {"info_added": 2, "meme_added": 1, "cancelled": 0}
    assert s.plan(D, 2, 1, "기본") == {"info_added": 0, "meme_added": 0, "cancelled": 0}
    slot = s.claim(D, "info", "run1", clock.now())
    assert slot and slot.slot_no == 1 and slot.stage == "writing"
    s.set_stage(slot, "ready", actual_kind="info")
    db.execute(
        "UPDATE pick_slots SET content_id = 'c1' WHERE slot_no = 1 AND requested_kind = 'info'"
    )
    out = s.plan(D, 0, 0, "기본")  # 목표를 0으로: 완성된 1번은 남고 2번·밈 1번만 제외
    assert out["cancelled"] == 2
    stages = {(x.requested_kind, x.slot_no): x.stage for x in s.list_for_date(D)}
    assert stages == {("info", 1): "ready", ("info", 2): "cancelled", ("meme", 1): "cancelled"}
    s.plan(D, 2, 0, "기본")  # 다시 올리면 취소한 빈 슬롯이 살아난다
    assert s.get(D, "info", 2).stage == "planned" and s.get(D, "meme", 1).stage == "cancelled"


def test_claim_is_exclusive_and_respects_next_attempt(db, clock) -> None:
    s = SlotStore(db, clock)
    s.plan(D, 1, 0, "기본")
    a = s.claim(D, "info", "r1", clock.now())
    assert (
        a is not None and s.claim(D, "info", "r2", clock.now()) is None
    )  # 같은 슬롯을 두 번 잡지 않는다
    s.fail(a, "failed_text", "AI 장애", timedelta(minutes=15))
    a = s.get(D, "info", 1)
    assert a.stage == "planned" and a.attempts == 1 and a.next_attempt_at is not None
    assert s.claim(D, "info", "r3", clock.now()) is None  # 아직 재시도 시각 전
    assert s.due(clock.now()) == []
    assert s.claim(D, "info", "r3", clock.now() + timedelta(minutes=16)) is not None


def test_fail_without_retry_and_retry_targets(db, clock) -> None:
    s = SlotStore(db, clock)
    s.plan(D, 1, 1, "기본")
    info = s.claim(D, "info", "r", clock.now())
    s.fail(info, "failed_text", "거절")  # retry_in 없음 → 그대로 실패
    assert s.get(D, "info", 1).stage == "failed_text" and s.get(D, "info", 1).retryable
    assert s.retry(s.get(D, "info", 1)) and s.get(D, "info", 1).stage == "planned"
    meme = s.claim(D, "meme", "r", clock.now())
    s.set_stage(meme, "images_pending", actual_kind="meme")
    s.set_stage(meme, "producing")
    s.fail(meme, "failed_image", "정책 차단")
    assert s.get(D, "meme", 1).stage == "failed_image"
    assert (
        s.retry(s.get(D, "meme", 1)) and s.get(D, "meme", 1).stage == "images_pending"
    )  # 원고는 유지
    for _ in range(MAX_ATTEMPTS):
        sl = s.get(D, "meme", 1)
        s.fail(sl, "failed_image", "x")
    assert not s.retry(s.get(D, "meme", 1))  # 횟수 상한
    e = s.get(D, "info", 1)
    s.set_stage(e, "empty", error="후보 없음")
    assert s.retry(s.get(D, "info", 1)) and s.get(D, "info", 1).stage == "planned"


def test_recover_stale_and_preset_filter(db, clock) -> None:
    s = SlotStore(db, clock)
    s.plan(D, 1, 1, "기본")
    a = s.claim(D, "info", "r", clock.now())
    s.set_stage(a, "writing")
    clock.advance(hours=3)
    assert s.recover_stale(clock.now()) == 1 and s.get(D, "info", 1).stage == "planned"
    m = s.get(D, "meme", 1)
    s.fail(m, "failed_text", "x", timedelta(minutes=1))
    clock.advance(minutes=2)
    assert [x.key for x in s.due(clock.now(), "기본")] == [m.key]
    assert s.due(clock.now(), "다른공간") == []  # 다른 작업 공간의 슬롯은 집지 않는다


def test_migration_from_v4_moves_existing_picks_into_slots(tmp_path, clock) -> None:
    import duckdb

    path = tmp_path / "old.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE schema_version (version INTEGER)")
    con.execute("INSERT INTO schema_version VALUES (4)")
    con.execute(
        "CREATE TABLE picks (content_id VARCHAR PRIMARY KEY, run_date DATE NOT NULL, rank INTEGER NOT NULL, "
        "reason VARCHAR NOT NULL, state VARCHAR NOT NULL, freshness VARCHAR NOT NULL, published_at TIMESTAMP, "
        "photo_status VARCHAR NOT NULL, photo_candidate_url VARCHAR, photo_source_url VARCHAR, "
        "photo_candidate_path VARCHAR, render_checked_at TIMESTAMP, rechecked_at TIMESTAMP, "
        "title_fingerprint VARCHAR NOT NULL, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL)"
    )
    con.execute(
        "CREATE TABLE contents (id VARCHAR PRIMARY KEY, status VARCHAR, topic VARCHAR, template VARCHAR, generated VARCHAR, relevance_confidence DOUBLE, is_ad BOOLEAN, review_reason VARCHAR, created_at TIMESTAMP, updated_at TIMESTAMP, approved_by VARCHAR, persona_topic VARCHAR)"
    )
    con.execute(
        "INSERT INTO picks VALUES ('c1', DATE '2026-10-10', 1, 'r', 'ready', 'fresh', NULL, 'none', NULL, NULL, NULL, "
        "NULL, NULL, 'fp', TIMESTAMP '2026-10-10 01:00:00', TIMESTAMP '2026-10-10 01:00:00')"
    )
    con.close()
    db = Database(path)
    assert db.schema_version == 5
    s = SlotStore(db, clock)
    rows = s.list_for_date(date(2026, 10, 10))
    assert len(rows) == 1 and rows[0].content_id == "c1" and rows[0].stage == "ready"
    assert db.execute("SELECT kind, slot_key FROM picks")[0] == ("info", "2026-10-10:info:1")
    db.close()
