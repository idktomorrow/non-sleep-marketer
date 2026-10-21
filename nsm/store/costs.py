"""전역 비용 원장(S13 제안): 작업 공간(프리셋)을 바꿔도 하루 비용 상한은 하나로 센다.

작업 공간 DB의 `costs` 표는 그 공간의 지출 표시용 사본이고, 상한 판정은 이 원장(`<NSM_HOME>/costs.duckdb`)이 한다.
- reserve → confirm/release/mark_unknown: 유료 호출 전에 예약하고, 응답의 usage로 **같은 행**을 확정한다
  (estimated→confirmed 전환이 이중 합산되지 않게). 전송 전 실패는 해제, 전송 뒤 응답 없음은 미확인으로 남겨
  예약액을 계속 센다(사람이 "과금 안 됨 확인"으로 풀 때까지).
- record_cost(옛 인터페이스)는 바로 confirmed 행 하나.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from nsm.domain.clock import new_id
from nsm.store.db import Database, from_db, to_db

SCHEMA = """
CREATE TABLE IF NOT EXISTS cost_ledger (
    id VARCHAR PRIMARY KEY,
    workspace VARCHAR NOT NULL,
    kind VARCHAR NOT NULL,
    provider VARCHAR NOT NULL,
    model VARCHAR,
    usd DOUBLE NOT NULL DEFAULT 0,
    reserved_usd DOUBLE NOT NULL DEFAULT 0,
    status VARCHAR NOT NULL,
    request_id VARCHAR,
    ref VARCHAR,
    detail VARCHAR,
    occurred_at TIMESTAMP NOT NULL,
    confirmed_at TIMESTAMP
);
"""
# 상한에 더하는 금액: 확정·추정은 금액, 예약·미확인은 예약액, 해제는 0
SPENT_EXPR = (
    "CASE WHEN status IN ('confirmed','estimated') THEN usd "
    "WHEN status IN ('reserved','unknown') THEN reserved_usd ELSE 0 END"
)

Mirror = Callable[[str, str, float, str], None]  # 작업 공간 DB의 costs 표에 사본을 남기는 함수


class CostLedger:
    def __init__(
        self,
        db: Database,
        clock: Any,
        workspace: Callable[[], str] | str = "기본",
        mirror: Mirror | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self._workspace = workspace
        self.mirror = mirror
        db.execute(SCHEMA)

    # ------------------------------------------------------------------ helpers
    @property
    def workspace(self) -> str:
        return self._workspace() if callable(self._workspace) else self._workspace

    def set_workspace(self, getter: Callable[[], str] | str) -> None:
        """작업 공간 이름을 매번 읽을 함수로 바꾼다(프리셋 전환 뒤에도 지금 공간으로 기록)."""
        self._workspace = getter

    def _now(self) -> datetime:
        return to_db(self.clock.now())  # type: ignore[return-value]

    # ------------------------------------------------------------------ CostSink
    def record_cost(self, kind: str, provider: str, usd: float, ref: str = "") -> None:
        self.db.execute(
            "INSERT INTO cost_ledger (id, workspace, kind, provider, usd, status, ref, occurred_at, "
            "confirmed_at) VALUES (?, ?, ?, ?, ?, 'confirmed', ?, ?, ?)",
            [new_id(), self.workspace, kind, provider, float(usd), ref, self._now(), self._now()],
        )
        if self.mirror:
            self.mirror(kind, provider, float(usd), ref)

    # ------------------------------------------------------------------ 예약·확정
    def reserve(
        self, kind: str, provider: str, estimate_usd: float, ref: str = "", model: str = ""
    ) -> str:
        rid = new_id()
        self.db.execute(
            "INSERT INTO cost_ledger (id, workspace, kind, provider, model, reserved_usd, status, ref, "
            "occurred_at) VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?, ?)",
            [rid, self.workspace, kind, provider, model, float(estimate_usd), ref, self._now()],
        )
        return rid

    def confirm(
        self,
        reservation_id: str,
        usd: float,
        status: Literal["confirmed", "estimated"] = "confirmed",
        detail: dict[str, Any] | None = None,
        request_id: str = "",
    ) -> None:
        rows = self.db.execute(
            "UPDATE cost_ledger SET usd = ?, status = ?, detail = ?, request_id = ?, confirmed_at = ? "
            "WHERE id = ? AND status IN ('reserved', 'unknown') RETURNING kind, provider, ref",
            [
                float(usd),
                status,
                json.dumps(detail, ensure_ascii=False) if detail else None,
                request_id or None,
                self._now(),
                reservation_id,
            ],
        )
        if rows and self.mirror:
            kind, provider, ref = rows[0]
            self.mirror(kind, provider, float(usd), ref or "")

    def release(self, reservation_id: str) -> None:
        self.db.execute(
            "UPDATE cost_ledger SET status = 'released', reserved_usd = 0 "
            "WHERE id = ? AND status IN ('reserved', 'unknown')",
            [reservation_id],
        )

    def mark_unknown(self, reservation_id: str, request_id: str = "") -> None:
        self.db.execute(
            "UPDATE cost_ledger SET status = 'unknown', request_id = coalesce(?, request_id) "
            "WHERE id = ? AND status = 'reserved'",
            [request_id or None, reservation_id],
        )

    # ------------------------------------------------------------------ 조회
    def spent_between(self, start: datetime, end: datetime) -> float:
        row = self.db.execute(
            f"SELECT coalesce(sum({SPENT_EXPR}), 0) FROM cost_ledger "
            "WHERE occurred_at >= ? AND occurred_at < ?",
            [to_db(start), to_db(end)],
        )
        return float(row[0][0])

    def breakdown(self, start: datetime, end: datetime) -> dict[str, float]:
        """상태별 합계(대시보드 표시: 확정·추정·미확인)."""
        rows = self.db.execute(
            f"SELECT status, coalesce(sum({SPENT_EXPR}), 0) FROM cost_ledger "
            "WHERE occurred_at >= ? AND occurred_at < ? GROUP BY 1",
            [to_db(start), to_db(end)],
        )
        return {str(r[0]): float(r[1]) for r in rows}

    def unknown_requests(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT id, workspace, kind, provider, model, reserved_usd, ref, request_id, occurred_at "
            "FROM cost_ledger WHERE status = 'unknown' ORDER BY occurred_at DESC LIMIT ?",
            [limit],
        )
        keys = (
            "id",
            "workspace",
            "kind",
            "provider",
            "model",
            "reserved_usd",
            "ref",
            "request_id",
            "occurred_at",
        )
        out = []
        for r in rows:
            d = dict(zip(keys, r, strict=True))
            d["occurred_at"] = from_db(d["occurred_at"])
            out.append(d)
        return out

    def count(self) -> int:
        return int(self.db.execute("SELECT count(*) FROM cost_ledger")[0][0])

    def import_legacy(
        self, rows: list[tuple[str, str, float, datetime, str]], workspace: str
    ) -> int:
        """예전 작업 공간 DB의 costs 표를 원장으로 옮긴다(원장이 비어 있을 때 한 번)."""
        if self.count():
            return 0
        n = 0
        with self.db.transaction() as cur:
            for kind, provider, usd, occurred_at, ref in rows:
                cur.execute(
                    "INSERT INTO cost_ledger (id, workspace, kind, provider, usd, status, ref, "
                    "occurred_at, confirmed_at) VALUES (?, ?, ?, ?, ?, 'confirmed', ?, ?, ?)",
                    [
                        new_id(),
                        workspace,
                        kind,
                        provider,
                        float(usd),
                        ref,
                        occurred_at,
                        occurred_at,
                    ],
                )
                n += 1
        return n
