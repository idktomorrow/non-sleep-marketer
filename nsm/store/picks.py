"""오늘의 추천 저장소(S11 제안, ADR 0050). 콘텐츠 상태(동결)는 그대로 두고 `picks` 표를 옆에 둔다.

- 추천 저장 = contents(검토 대기) + 자료 사용 표시 + picks 행을 한 트랜잭션으로
- 게시 선택 = 승인(human) + publications 생성 + 포기한 게시 되살림 + picks(queued)를 한 트랜잭션으로
- 관심 없음 = 폐기 + 자료 해제 + picks(dismissed, 지문은 남김)를 한 트랜잭션으로
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from nsm.domain.clock import new_id
from nsm.domain.models import Content, Platform
from nsm.ports import Clock
from nsm.store.db import Database, from_db, to_db
from nsm.store.repo import InvalidTransitionError

PICK_STATES = ("ready", "saved", "queued", "dismissed")
PHOTO_STATES = ("none", "candidate", "applied", "dropped")
FRESHNESS = ("fresh", "stale", "unknown")


@dataclass
class Pick:
    content_id: str
    run_date: date
    rank: int
    reason: str
    state: str = "ready"
    freshness: str = "unknown"
    published_at: datetime | None = None
    photo_status: str = "none"
    photo_candidate_url: str | None = None
    photo_source_url: str | None = None
    photo_candidate_path: str | None = None
    render_checked_at: datetime | None = None
    rechecked_at: datetime | None = None
    title_fingerprint: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def ready_to_publish(self) -> bool:
        """사진을 정했고(쓰기·안 쓰기) 미리보기 렌더에서 넘침이 없었다. 그 전에는 "사진 확인 대기"."""
        return (
            self.photo_status in ("applied", "none", "dropped")
            and self.render_checked_at is not None
        )


class PicksStore:
    _COLS = (
        "content_id, run_date, rank, reason, state, freshness, published_at, photo_status, "
        "photo_candidate_url, photo_source_url, photo_candidate_path, render_checked_at, "
        "rechecked_at, title_fingerprint, created_at, updated_at"
    )

    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    def _now(self) -> datetime:
        return to_db(self.clock.now())  # type: ignore[return-value]

    @staticmethod
    def _count(rows: list[tuple]) -> int:
        return int(rows[0][0]) if rows and rows[0] and rows[0][0] is not None else 0

    def _row(self, r: tuple) -> Pick:
        return Pick(
            content_id=r[0],
            run_date=r[1],
            rank=int(r[2]),
            reason=r[3],
            state=r[4],
            freshness=r[5],
            published_at=from_db(r[6]),
            photo_status=r[7],
            photo_candidate_url=r[8],
            photo_source_url=r[9],
            photo_candidate_path=r[10],
            render_checked_at=from_db(r[11]),
            rechecked_at=from_db(r[12]),
            title_fingerprint=r[13],
            created_at=from_db(r[14]),
            updated_at=from_db(r[15]),
        )

    # ------------------------------------------------------------ 저장·조회

    def save(
        self, content: Content, *, persona_topic: str | None, item_ids: list[str], pick: Pick
    ) -> None:
        """추천 원고 저장을 한 트랜잭션으로. 게시 레코드는 만들지 않는다(게시로 보낼 때 만든다)."""
        if content.status != "pending_review":
            raise ValueError("추천 원고는 검토 대기로만 저장한다")
        now = self._now()
        with self.db.transaction() as cur:
            cur.execute(
                "INSERT INTO contents (id, status, topic, template, generated, relevance_confidence, "
                "is_ad, review_reason, created_at, updated_at, persona_topic) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    content.id,
                    content.status,
                    content.generated.topic,
                    content.generated.template,
                    content.generated.model_dump_json(),
                    content.relevance_confidence,
                    content.is_ad,
                    content.review_reason,
                    to_db(content.created_at),
                    now,
                    persona_topic,
                ],
            )
            if item_ids:
                marks = ",".join("?" * len(item_ids))
                cur.execute(
                    f"UPDATE source_items SET used_in_content_id = ? WHERE id IN ({marks})",
                    [content.id, *item_ids],
                )
            cur.execute(
                f"INSERT INTO picks ({self._COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    content.id,
                    pick.run_date,
                    pick.rank,
                    pick.reason,
                    pick.state,
                    pick.freshness,
                    to_db(pick.published_at),
                    pick.photo_status,
                    pick.photo_candidate_url,
                    pick.photo_source_url,
                    pick.photo_candidate_path,
                    to_db(pick.render_checked_at),
                    to_db(pick.rechecked_at),
                    pick.title_fingerprint,
                    now,
                    now,
                ],
            )

    def get(self, content_id: str) -> Pick | None:
        rows = self.db.execute(f"SELECT {self._COLS} FROM picks WHERE content_id = ?", [content_id])
        return self._row(rows[0]) if rows else None

    def list(
        self, state: str | None = None, run_date: date | None = None, limit: int = 50
    ) -> list[Pick]:
        where, params = [], []
        if state:
            where.append("state = ?")
            params.append(state)
        if run_date:
            where.append("run_date = ?")
            params.append(run_date)
        sql = f"SELECT {self._COLS} FROM picks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY run_date DESC, rank, created_at LIMIT ?"
        return [self._row(r) for r in self.db.execute(sql, [*params, limit])]

    def count(self, state: str) -> int:
        return self._count(self.db.execute("SELECT count(*) FROM picks WHERE state = ?", [state]))

    def count_for_date(self, run_date: date) -> int:
        return self._count(
            self.db.execute("SELECT count(*) FROM picks WHERE run_date = ?", [run_date])
        )

    def set_state(self, content_id: str, expected: tuple[str, ...], target: str) -> bool:
        """조건부 상태 변경. 기대한 상태가 아니면 False(두 번 누르기·동시 변경 방지)."""
        marks = ",".join("?" * len(expected))
        return (
            self._count(
                self.db.execute(
                    f"UPDATE picks SET state = ?, updated_at = ? WHERE content_id = ? AND state IN ({marks})",
                    [target, self._now(), content_id, *expected],
                )
            )
            > 0
        )

    def set_photo(
        self,
        content_id: str,
        status: str,
        *,
        image_url: str | None = None,
        source_url: str | None = None,
        path: str | None = None,
    ) -> None:
        if status not in PHOTO_STATES:
            raise ValueError(status)
        self.db.execute(
            "UPDATE picks SET photo_status = ?, photo_candidate_url = coalesce(?, photo_candidate_url), "
            "photo_source_url = coalesce(?, photo_source_url), "
            "photo_candidate_path = coalesce(?, photo_candidate_path), "
            "render_checked_at = NULL, updated_at = ? WHERE content_id = ?",
            [status, image_url, source_url, path, self._now(), content_id],
        )

    def set_render_checked(self, content_id: str, at: datetime | None) -> None:
        self.db.execute(
            "UPDATE picks SET render_checked_at = ?, updated_at = ? WHERE content_id = ?",
            [to_db(at), self._now(), content_id],
        )

    def set_rechecked(self, content_id: str, at: datetime) -> None:
        self.db.execute(
            "UPDATE picks SET rechecked_at = ?, updated_at = ? WHERE content_id = ?",
            [to_db(at), self._now(), content_id],
        )

    def dismissed_fingerprints(self, since: datetime) -> list[str]:
        """'관심 없음'으로 버린 사건의 지문. 자료가 풀려도 같은 사건을 다음날 다시 추천하지 않게."""
        rows = self.db.execute(
            "SELECT title_fingerprint FROM picks WHERE state = 'dismissed' AND updated_at >= ?",
            [to_db(since)],
        )
        return [r[0] for r in rows if r[0]]

    def recent_topics_text(self, since: datetime) -> list[str]:
        """최근에 만든 원고 제목(폐기 제외). 같은 사건을 다시 추천하지 않게 비교한다."""
        rows = self.db.execute(
            "SELECT topic FROM contents WHERE created_at >= ? AND status != 'discarded'",
            [to_db(since)],
        )
        return [r[0] for r in rows]

    # ------------------------------------------------------------ 상태 묶음(트랜잭션)

    def queue(self, content_id: str, platforms: list[Platform]) -> None:
        """게시로 보내기: 승인(사람) + 게시 레코드 + 포기했던 게시 되살림 + picks(queued)를 한 번에."""
        now = self._now()
        with self.db.transaction() as cur:
            cur.execute(
                "UPDATE contents SET status = 'approved', approved_by = 'human', updated_at = ? "
                "WHERE id = ? AND status IN ('draft', 'pending_review')",
                [now, content_id],
            )
            if self._count(cur.fetchall()) == 0:
                raise InvalidTransitionError("검토 대기 중인 원고만 게시로 보낼 수 있습니다")
            for p in platforms:
                cur.execute(
                    "SELECT 1 FROM publications WHERE content_id = ? AND platform = ?",
                    [content_id, p],
                )
                if cur.fetchall():
                    continue
                cur.execute(
                    "INSERT INTO publications (id, content_id, platform, status, attempts, created_at) "
                    "VALUES (?, ?, ?, 'queued', 0, ?)",
                    [new_id(), content_id, p, now],
                )
            cur.execute(
                "UPDATE publications SET status = 'queued', attempts = 0, next_attempt_at = NULL "
                "WHERE content_id = ? AND status = 'abandoned'",
                [content_id],
            )
            cur.execute(
                "UPDATE picks SET state = 'queued', updated_at = ? WHERE content_id = ?",
                [now, content_id],
            )

    def dismiss(self, content_id: str) -> None:
        """관심 없음: 폐기 + 자료 해제 + picks(dismissed). 지문은 남겨 재추천을 막는다."""
        now = self._now()
        with self.db.transaction() as cur:
            cur.execute(
                "UPDATE contents SET status = 'discarded', review_reason = '관심 없음(추천)', updated_at = ? "
                "WHERE id = ? AND status IN ('draft', 'pending_review', 'approved')",
                [now, content_id],
            )
            if self._count(cur.fetchall()) == 0:
                raise InvalidTransitionError("이미 게시했거나 폐기한 원고입니다")
            cur.execute(
                "UPDATE source_items SET used_in_content_id = NULL WHERE used_in_content_id = ?",
                [content_id],
            )
            cur.execute(
                "UPDATE picks SET state = 'dismissed', updated_at = ? WHERE content_id = ?",
                [now, content_id],
            )

    def unqueue(self, content_id: str) -> None:
        """게시 대기에서 되돌리기: 콘텐츠는 검토 대기로, 추천은 보관으로."""
        now = self._now()
        with self.db.transaction() as cur:
            cur.execute(
                "UPDATE contents SET status = 'pending_review', review_reason = '사람이 게시 대기에서 되돌림', "
                "updated_at = ? WHERE id = ? AND status = 'approved'",
                [now, content_id],
            )
            if self._count(cur.fetchall()) == 0:
                raise InvalidTransitionError("게시 대기 중인 원고만 되돌릴 수 있습니다")
            cur.execute(
                "UPDATE picks SET state = 'saved', updated_at = ? WHERE content_id = ? AND state = 'queued'",
                [now, content_id],
            )

    def as_dict(self, p: Pick) -> dict[str, Any]:
        return {k: getattr(p, k) for k in p.__dataclass_fields__}
