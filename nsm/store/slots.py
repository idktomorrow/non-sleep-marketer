"""추천 슬롯 저장소(S14 제안, ADR 0057): "오늘 정보 N개 + 밈 M개"의 **계획과 제작 상태**를 콘텐츠와 분리해 둔다.

pick 행은 완성된 content가 있어야 존재하지만, 슬롯은 계획 단계부터 존재한다. 그래서 "기획됨·쓰는 중·실패·비어 있음"이
살 곳이 생기고, 같은 날 재실행·동시 실행이 슬롯을 claim(조건부 UPDATE)해 중복 생성하지 않는다.

단계(stage): planned → writing → written → images_pending → producing → ready
  failed_text(← writing, 재시도 → planned) · failed_image(← producing, 재시도 → images_pending, content 유지)
  empty(후보 없음, 다음 실행·수동 재시도 → planned) · unknown_image(S15, 사람 결정 대기) · cancelled(목표 축소로 제외)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from nsm.ports import Clock
from nsm.store.db import Database, from_db, to_db

KINDS = ("info", "meme")
STAGES = (
    "planned",
    "writing",
    "written",
    "images_pending",
    "producing",
    "ready",
    "failed_text",
    "failed_image",
    "empty",
    "unknown_image",
    "cancelled",
)
RETRY_TO = {"failed_text": "planned", "failed_image": "images_pending", "empty": "planned"}
MAX_ATTEMPTS = 3
STALE_AFTER = timedelta(hours=2)  # writing/producing인데 이만큼 지났으면 중단된 것(재시작 복구)
_COLS = (
    "run_date, requested_kind, slot_no, actual_kind, stage, content_id, preset, run_id, attempts, "
    "next_attempt_at, last_error, plan_json, cast_json, mix, created_at, updated_at"
)


@dataclass
class Slot:
    run_date: date
    requested_kind: str
    slot_no: int
    actual_kind: str | None = None
    stage: str = "planned"
    content_id: str | None = None
    preset: str | None = None
    run_id: str | None = None
    attempts: int = 0
    next_attempt_at: datetime | None = None
    last_error: str | None = None
    plan: dict[str, Any] | None = None
    cast: dict[str, Any] | None = None
    mix: str = "none"
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def key(self) -> str:
        return f"{self.run_date.isoformat()}:{self.requested_kind}:{self.slot_no}"

    @property
    def retryable(self) -> bool:
        return self.stage in RETRY_TO and self.attempts < MAX_ATTEMPTS

    @property
    def has_content(self) -> bool:
        return self.content_id is not None


def parse_key(key: str) -> tuple[date, str, int]:
    d, kind, no = key.split(":")
    return date.fromisoformat(d), kind, int(no)


class SlotStore:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    def _now(self) -> datetime:
        return to_db(self.clock.now())  # type: ignore[return-value]

    @staticmethod
    def _row(r: tuple) -> Slot:
        return Slot(
            run_date=r[0],
            requested_kind=r[1],
            slot_no=int(r[2]),
            actual_kind=r[3],
            stage=r[4],
            content_id=r[5],
            preset=r[6],
            run_id=r[7],
            attempts=int(r[8] or 0),
            next_attempt_at=from_db(r[9]),
            last_error=r[10],
            plan=json.loads(r[11]) if r[11] else None,
            cast=json.loads(r[12]) if r[12] else None,
            mix=r[13] or "none",
            created_at=from_db(r[14]),
            updated_at=from_db(r[15]),
        )

    # ------------------------------------------------------------ 계획
    def plan(self, run_date: date, info: int, meme: int, preset: str) -> dict[str, int]:
        """목표만큼 슬롯을 둔다. 부족분은 새로 만들고, 목표를 **낮추면 content 없는 초과 슬롯만** cancelled.
        이미 완성된 결과는 그대로. 같은 날 다시 불러도 안전(멱등)."""
        out = {"info_added": 0, "meme_added": 0, "cancelled": 0}
        now = self._now()
        for kind, target in (("info", info), ("meme", meme)):
            rows = self.db.execute(
                "SELECT slot_no, stage, content_id FROM pick_slots "
                "WHERE run_date = ? AND requested_kind = ? ORDER BY slot_no",
                [run_date, kind],
            )
            existing = {int(r[0]): (r[1], r[2]) for r in rows}
            for no in range(1, target + 1):
                if no not in existing:
                    self.db.execute(
                        "INSERT INTO pick_slots (run_date, requested_kind, slot_no, stage, preset, "
                        "attempts, mix, created_at, updated_at) VALUES (?, ?, ?, 'planned', ?, 0, 'none', ?, ?)",
                        [run_date, kind, no, preset, now, now],
                    )
                    out[f"{kind}_added"] += 1
                elif existing[no][0] == "cancelled":
                    # 목표를 다시 올렸다: 취소했던 빈 슬롯을 되살린다
                    self.db.execute(
                        "UPDATE pick_slots SET stage = 'planned', updated_at = ? "
                        "WHERE run_date = ? AND requested_kind = ? AND slot_no = ? AND content_id IS NULL",
                        [now, run_date, kind, no],
                    )
            for no, (stage, cid) in existing.items():
                if no > target and cid is None and stage != "cancelled":
                    self.db.execute(
                        "UPDATE pick_slots SET stage = 'cancelled', updated_at = ? "
                        "WHERE run_date = ? AND requested_kind = ? AND slot_no = ?",
                        [now, run_date, kind, no],
                    )
                    out["cancelled"] += 1
        return out

    # ------------------------------------------------------------ 조회
    def get(self, run_date: date, kind: str, slot_no: int) -> Slot | None:
        rows = self.db.execute(
            f"SELECT {_COLS} FROM pick_slots WHERE run_date = ? AND requested_kind = ? AND slot_no = ?",
            [run_date, kind, slot_no],
        )
        return self._row(rows[0]) if rows else None

    def by_key(self, key: str) -> Slot | None:
        return self.get(*parse_key(key))

    def by_content(self, content_id: str) -> Slot | None:
        rows = self.db.execute(f"SELECT {_COLS} FROM pick_slots WHERE content_id = ?", [content_id])
        return self._row(rows[0]) if rows else None

    def list_for_date(self, run_date: date) -> list[Slot]:
        rows = self.db.execute(
            f"SELECT {_COLS} FROM pick_slots WHERE run_date = ? ORDER BY requested_kind, slot_no",
            [run_date],
        )
        return [self._row(r) for r in rows]

    def counts(self, run_date: date) -> dict[str, dict[str, int]]:
        """{kind: {stage: n}} — 화면·요약용."""
        out: dict[str, dict[str, int]] = {k: {} for k in KINDS}
        for s in self.list_for_date(run_date):
            out.setdefault(s.requested_kind, {})
            out[s.requested_kind][s.stage] = out[s.requested_kind].get(s.stage, 0) + 1
        return out

    def due(self, now: datetime, preset: str | None = None) -> list[Slot]:
        """재시도할 때가 된 슬롯(planned이고 next_attempt_at이 지났거나 없음). 다른 작업 공간 것은 제외."""
        rows = self.db.execute(
            f"SELECT {_COLS} FROM pick_slots WHERE stage = 'planned' AND attempts > 0 "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY run_date, requested_kind, slot_no",
            [to_db(now)],
        )
        out = [self._row(r) for r in rows]
        return [s for s in out if preset is None or s.preset in (None, preset)]

    # ------------------------------------------------------------ 상태 전이
    def claim(self, run_date: date, kind: str, run_id: str, now: datetime) -> Slot | None:
        """planned 슬롯 하나를 writing으로 잡는다(조건부 UPDATE: 동시 실행·재실행이 같은 슬롯을 두 번 쓰지 않게)."""
        rows = self.db.execute(
            "SELECT slot_no FROM pick_slots WHERE run_date = ? AND requested_kind = ? AND stage = 'planned' "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY slot_no LIMIT 1",
            [run_date, kind, to_db(now)],
        )
        if not rows:
            return None
        no = int(rows[0][0])
        updated = self.db.execute(
            "UPDATE pick_slots SET stage = 'writing', run_id = ?, updated_at = ? "
            "WHERE run_date = ? AND requested_kind = ? AND slot_no = ? AND stage = 'planned' RETURNING slot_no",
            [run_id, self._now(), run_date, kind, no],
        )
        return self.get(run_date, kind, no) if updated else None

    def set_stage(
        self,
        slot: Slot,
        stage: str,
        *,
        error: str | None = None,
        plan: dict[str, Any] | None = None,
        actual_kind: str | None = None,
        mix: str | None = None,
    ) -> None:
        if stage not in STAGES:
            raise ValueError(stage)
        self.db.execute(
            "UPDATE pick_slots SET stage = ?, last_error = ?, plan_json = coalesce(?, plan_json), "
            "actual_kind = coalesce(?, actual_kind), mix = coalesce(?, mix), updated_at = ? "
            "WHERE run_date = ? AND requested_kind = ? AND slot_no = ?",
            [
                stage,
                error,
                json.dumps(plan, ensure_ascii=False) if plan is not None else None,
                actual_kind,
                mix,
                self._now(),
                slot.run_date,
                slot.requested_kind,
                slot.slot_no,
            ],
        )
        slot.stage, slot.last_error = stage, error

    def fail(self, slot: Slot, stage: str, error: str, retry_in: timedelta | None = None) -> None:
        """실패 기록. retry_in이 있으면 next_attempt_at을 두고 planned로 돌려 재시도 잡이 집어 가게 한다."""
        attempts = slot.attempts + 1
        back = (
            RETRY_TO.get(stage, "planned")
            if retry_in is not None and attempts < MAX_ATTEMPTS
            else stage
        )
        self.db.execute(
            "UPDATE pick_slots SET stage = ?, attempts = ?, next_attempt_at = ?, last_error = ?, updated_at = ? "
            "WHERE run_date = ? AND requested_kind = ? AND slot_no = ?",
            [
                back,
                attempts,
                to_db(self.clock.now() + retry_in) if retry_in is not None else None,
                error[:300],
                self._now(),
                slot.run_date,
                slot.requested_kind,
                slot.slot_no,
            ],
        )
        slot.stage, slot.attempts, slot.last_error = back, attempts, error[:300]

    def retry(self, slot: Slot) -> bool:
        """사람이 누른 재시도: failed_text/empty → planned, failed_image → images_pending. 상한 넘으면 False."""
        if slot.stage not in RETRY_TO or slot.attempts >= MAX_ATTEMPTS:
            return False
        self.db.execute(
            "UPDATE pick_slots SET stage = ?, next_attempt_at = NULL, last_error = NULL, updated_at = ? "
            "WHERE run_date = ? AND requested_kind = ? AND slot_no = ?",
            [RETRY_TO[slot.stage], self._now(), slot.run_date, slot.requested_kind, slot.slot_no],
        )
        return True

    def recover_stale(self, now: datetime) -> int:
        """재시작 복구: writing/producing에서 오래 멈춘 슬롯을 다음 단계 시작점으로 되돌린다."""
        cutoff = to_db(now - STALE_AFTER)
        n = 0
        for stage, back in (("writing", "planned"), ("producing", "images_pending")):
            rows = self.db.execute(
                "UPDATE pick_slots SET stage = ?, run_id = NULL, updated_at = ? "
                "WHERE stage = ? AND updated_at <= ? RETURNING slot_no",
                [back, self._now(), stage, cutoff],
            )
            n += len(rows)
        return n

    # ------------------------------------------------------------ 밈 중복 판정용
    def recent_meme_plans(self, since: datetime) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT plan_json FROM pick_slots WHERE actual_kind = 'meme' AND plan_json IS NOT NULL "
            "AND created_at >= ? ORDER BY created_at DESC",
            [to_db(since)],
        )
        return [json.loads(r[0]) for r in rows if r[0]]
