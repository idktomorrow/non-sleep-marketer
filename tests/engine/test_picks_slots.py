"""추천 슬롯 엔진(S14 제안, ADR 0057): 정보 N + 밈 M 계획, 밈 경로 분리, 실패 격리·재시도, 대체 종류 기록, 오늘만 변경."""

from __future__ import annotations

from datetime import date

import pytest

from nsm.engine.picks import PLAN_OVERRIDE_KEY, run_picks, run_slots_retry
from nsm.store.picks import PicksStore
from nsm.store.slots import SlotStore
from tests.engine.test_picks_job import _enable, _fake_photo, _item, ctx  # noqa: F401
from tests.fakes import FakeScenarioWriter, FakeSource


def _workspace(c, **kw):
    c.config = c.config.model_copy(update={"workspace": c.config.workspace.model_copy(update=kw)})


def test_info_two_plus_meme_one(ctx) -> None:  # noqa: F811
    _workspace(ctx, info_count=2, meme_count=1, meme_tone="밈 말투")
    ctx.scenario_writer = FakeScenarioWriter()
    r = run_picks(ctx, fetch_photo=_fake_photo)
    assert (r.counts["made_info"], r.counts["made_meme"]) == (2, 1)
    slots = SlotStore(ctx.repo.db, ctx.clock)
    today = ctx.clock.now().date()
    stages = {(s.requested_kind, s.slot_no): s.stage for s in slots.list_for_date(today)}
    assert stages == {("info", 1): "ready", ("info", 2): "ready", ("meme", 1): "images_pending"}
    store = PicksStore(ctx.repo.db, ctx.clock)
    meme = next(p for p in store.list(run_date=today) if p.kind == "meme")
    c = ctx.repo.get_content(meme.content_id)
    assert c.generated.template == "meme" and c.generated.deck.slides[0].kind == "comic"
    assert c.generated.deck.labels.ai_slides == [] and c.status == "pending_review"
    assert "오늘의 추천(밈)" in c.review_reason and meme.slot_key == "2026-10-11:meme:1"
    req = ctx.scenario_writer.requests[0]
    assert (
        req.tone == "밈 말투" and ctx.config.persona.tone != "밈 말투"
    )  # 호출별 말투, 전역은 그대로
    assert c.generated.claims == []  # 뉴스 훅 없는 창작: 가짜 출처 없음
    # 밈 원고는 그림이 끝나기 전엔 게시로 보낼 수 없다
    from nsm.app.review_actions import ActionError, ReviewActions

    actions = ReviewActions(ctx.repo, store, ctx.clock)
    with pytest.raises(ActionError):
        actions.approve(meme.content_id, persona=ctx.config.persona, platforms=["threads"])


def test_second_run_fills_only_remaining_and_lowering_keeps_results(ctx) -> None:  # noqa: F811
    _workspace(ctx, info_count=3, meme_count=0)
    run_picks(ctx, fetch_photo=_fake_photo)
    assert ctx.repo.count_contents() == 3
    _workspace(ctx, info_count=1, meme_count=0)
    r = run_picks(ctx, fetch_photo=_fake_photo)
    assert r.skipped.startswith("오늘 슬롯") and ctx.repo.count_contents() == 3  # 완성된 것은 유지
    slots = SlotStore(ctx.repo.db, ctx.clock)
    assert all(s.stage == "ready" for s in slots.list_for_date(ctx.clock.now().date()))


def test_meme_failure_does_not_block_info_and_is_retried(ctx) -> None:  # noqa: F811
    _workspace(ctx, info_count=1, meme_count=1)
    ctx.scenario_writer = FakeScenarioWriter(fail=True)
    r = run_picks(ctx, fetch_photo=_fake_photo)
    assert r.counts["made_info"] == 1 and r.counts["made_meme"] == 0 and r.counts["failed"] == 1
    slots = SlotStore(ctx.repo.db, ctx.clock)
    meme = slots.get(ctx.clock.now().date(), "meme", 1)
    assert meme.stage == "planned" and meme.attempts == 1 and "밈 아이디어 실패" in meme.last_error
    assert "다시 시도할 슬롯이 없음" in run_slots_retry(ctx).skipped  # 아직 재시도 시각 전
    ctx.clock.advance(minutes=20)
    ctx.scenario_writer = FakeScenarioWriter()
    r2 = run_slots_retry(ctx)
    assert r2.counts["made_meme"] == 1 and ctx.repo.count_contents() == 2
    # 정보 원고의 자료는 밈 실패로 풀리지 않았다
    used = ctx.repo.db.execute(
        "SELECT count(*) FROM source_items WHERE used_in_content_id IS NOT NULL"
    )
    assert used[0][0] >= 1


def test_no_writer_marks_meme_failed_with_reason(ctx) -> None:  # noqa: F811
    _workspace(ctx, info_count=0, meme_count=1)
    ctx.scenario_writer = None
    r = run_picks(ctx, fetch_photo=_fake_photo)
    assert r.counts["made"] == 0
    slot = SlotStore(ctx.repo.db, ctx.clock).get(ctx.clock.now().date(), "meme", 1)
    assert "공급자" in slot.last_error


def test_duplicate_ideas_leave_slot_empty_without_forcing(ctx) -> None:  # noqa: F811
    from nsm.ports import MemeIdea

    _workspace(ctx, info_count=0, meme_count=2)
    one = MemeIdea("극중 월급 vs 현실 연봉", "시무룩 vs 느긋", "'시러~'는 무슨.", "")
    ctx.scenario_writer = FakeScenarioWriter(ideas=[one])
    r = run_picks(ctx, fetch_photo=_fake_photo)
    # 아이디어가 하나뿐이라 두 번째 슬롯은 중복 → 비어 있음(억지로 채우지 않는다)
    assert r.counts["made_meme"] == 1 and r.counts["empty"] == 1
    slots = SlotStore(ctx.repo.db, ctx.clock)
    assert slots.get(ctx.clock.now().date(), "meme", 2).stage == "empty"


def test_substitute_records_requested_and_actual_kind(ctx) -> None:  # noqa: F811
    ctx.sources = [FakeSource("naver", [])]  # 정보 후보 없음
    _workspace(ctx, info_count=1, meme_count=0, allow_substitute=True)
    ctx.scenario_writer = FakeScenarioWriter()
    r = run_picks(ctx, fetch_photo=_fake_photo)
    assert r.counts["made_meme"] == 1 and r.counts["made_info"] == 0
    slot = SlotStore(ctx.repo.db, ctx.clock).get(ctx.clock.now().date(), "info", 1)
    assert (slot.requested_kind, slot.actual_kind) == ("info", "meme")
    _workspace(ctx, allow_substitute=False)
    ctx.clock.advance(days=1)
    r = run_picks(ctx, fetch_photo=_fake_photo)
    assert r.counts["made"] == 0 and r.counts["empty"] == 1  # 기본은 대체하지 않는다


def test_today_override_beats_workspace(ctx) -> None:  # noqa: F811
    _workspace(ctx, info_count=3, meme_count=0)
    today = ctx.clock.now().date()
    ctx.repo.set_setting(PLAN_OVERRIDE_KEY, {"date": today.isoformat(), "info": 1, "meme": 0})
    r = run_picks(ctx, fetch_photo=_fake_photo)
    assert r.counts["made"] == 1 and r.counts["planned_info"] == 1
    ctx.repo.set_setting(
        PLAN_OVERRIDE_KEY, {"date": date(2020, 1, 1).isoformat(), "info": 5, "meme": 0}
    )
    assert run_picks(ctx, fetch_photo=_fake_photo).counts["planned_info"] == 3  # 날짜가 다르면 무시


def test_zero_plan_skips(ctx) -> None:  # noqa: F811
    _workspace(ctx, info_count=0, meme_count=0)
    assert "추천 개수가 0" in run_picks(ctx, fetch_photo=_fake_photo).skipped
