"""전역 비용 원장(S13 제안): 작업 공간을 바꿔도 합계가 유지되고, 예약→확정은 같은 행, 미확인은 예약액으로 센다."""

from __future__ import annotations

import pytest

from nsm.store.costs import CostLedger
from nsm.store.db import Database
from nsm.store.repo import Repository


def _day(clock):
    now = clock.now()
    return now.replace(hour=0, minute=0), now.replace(hour=23, minute=59)


def test_sum_survives_workspace_switch(clock) -> None:
    current = {"ws": "기본"}
    ledger = CostLedger(Database(":memory:"), clock, workspace=lambda: current["ws"])
    ledger.record_cost("llm", "openai", 1.0, "a")
    current["ws"] = "롤판"
    ledger.record_cost("llm", "openai", 0.5, "b")
    assert ledger.spent_between(*_day(clock)) == pytest.approx(1.5)
    rows = ledger.db.execute("SELECT workspace, usd FROM cost_ledger ORDER BY usd")
    assert rows == [("롤판", 0.5), ("기본", 1.0)]


def test_reserve_confirm_is_one_row_and_release_frees(clock) -> None:
    ledger = CostLedger(Database(":memory:"), clock)
    rid = ledger.reserve("image", "openai", 0.05, "sticker", "gpt-image-2.5-flare")
    assert ledger.spent_between(*_day(clock)) == pytest.approx(0.05)
    ledger.confirm(rid, 0.031, "confirmed", {"usage": {"output_tokens": 1000}}, "req_9")
    assert ledger.spent_between(*_day(clock)) == pytest.approx(0.031) and ledger.count() == 1
    ledger.confirm(rid, 9.0)  # 이미 확정된 행은 다시 바꾸지 않는다
    assert ledger.spent_between(*_day(clock)) == pytest.approx(0.031)
    rid2 = ledger.reserve("llm", "openai", 0.06, "generate")
    ledger.release(rid2)
    assert ledger.spent_between(*_day(clock)) == pytest.approx(0.031)


def test_unknown_keeps_reservation_until_released(clock) -> None:
    ledger = CostLedger(Database(":memory:"), clock)
    rid = ledger.reserve("image", "openai", 0.2, "panel")
    ledger.mark_unknown(rid, "req_x")
    assert ledger.breakdown(*_day(clock)) == {"unknown": pytest.approx(0.2)}
    assert ledger.unknown_requests()[0]["request_id"] == "req_x"
    ledger.release(rid)  # 사람이 "과금 안 됨 확인"
    assert ledger.spent_between(*_day(clock)) == 0


def test_unknown_model_prices_conservatively() -> None:
    from nsm.adapters.pricing import image_cost_from_usage, openai_text_cost

    assert openai_text_cost("gpt-6.1-sol-2026-09-08", 1_000_000, 0) == 2.0  # 스냅샷 꼬리만 뗀다
    assert openai_text_cost("gpt-5.7", 1_000_000, 0) == 10.0  # 모르는 모델은 가장 비싼 값
    usage = {
        "input_tokens_details": {"image_tokens": 0, "text_tokens": 0},
        "output_tokens": 1_000_000,
    }
    assert image_cost_from_usage("gpt-image-2-pro", usage, "medium")[0] == 32.0


def test_workspace_getter_can_change(clock) -> None:
    ledger = CostLedger(Database(":memory:"), clock, workspace="기본")
    ledger.set_workspace(lambda: "치이카와")
    ledger.record_cost("llm", "openai", 0.1)
    assert ledger.db.execute("SELECT workspace FROM cost_ledger")[0][0] == "치이카와"


def test_mirror_and_legacy_import(db, clock) -> None:
    repo = Repository(db, clock)
    repo.record_cost("llm", "anthropic", 0.3, "old")
    ledger = CostLedger(Database(":memory:"), clock, mirror=repo.record_cost)
    rows = repo.db.execute("SELECT kind, provider, usd, occurred_at, ref FROM costs")
    assert ledger.import_legacy([tuple(r) for r in rows], "기본") == 1
    assert ledger.import_legacy([tuple(r) for r in rows], "기본") == 0  # 비어 있을 때만
    ledger.record_cost("llm", "openai", 0.1, "new")
    start, end = _day(clock)
    assert repo.cost_between(start, end) == pytest.approx(0.4)  # 작업 공간 표에도 사본
    assert ledger.spent_between(start, end) == pytest.approx(0.4)
