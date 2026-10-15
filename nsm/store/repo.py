"""저장소 레이어. 엔진·대시보드는 SQL 대신 이 함수들만 쓴다.

- 모든 시각 인자는 aware datetime. 저장할 때 UTC로 바꾼다(`to_db`).
- 상태 전이는 허용된 경로만 통과한다(`ContentStatus`, `PublicationStatus`, ADR 0009).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from nsm.domain.clock import new_id
from nsm.domain.models import (
    AccountMetrics,
    Comment,
    CommentAction,
    CommentCategory,
    Content,
    ContentStatus,
    CostKind,
    GeneratedContent,
    Platform,
    PostMetrics,
    Publication,
    PublicationStatus,
    SourceItem,
)
from nsm.ports import Clock
from nsm.store.db import Database, from_db, to_db


class InvalidTransitionError(Exception):
    pass


CONTENT_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"pending_review", "approved", "discarded"},
    "pending_review": {"pending_review", "approved", "discarded"},
    "approved": {"published", "pending_review", "discarded"},
    "published": set(),
    "discarded": set(),
}

PUBLICATION_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"publishing", "abandoned"},
    "publishing": {"published", "failed", "abandoned"},
    "failed": {"publishing", "abandoned"},
    "published": set(),
    "abandoned": {"queued"},  # 사람이 대시보드에서 다시 시도
}


def _sources_for(table: dict[str, set[str]], target: str) -> list[str]:
    """target으로 갈 수 있는 이전 상태 목록."""
    return sorted(s for s, nexts in table.items() if target in nexts)


@dataclass
class StoredComment:
    id: str
    comment: Comment
    status: str
    category: str | None
    confidence: float | None
    action: str | None
    reason: str | None
    reply_text: str | None
    handled_at: datetime | None
    handled_by: str | None


class Repository:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    def _now(self) -> datetime:
        return to_db(self.clock.now())  # type: ignore[return-value]

    @staticmethod
    def _count(rows: list[tuple]) -> int:
        """DuckDB는 UPDATE 결과로 바뀐 행 수 한 줄을 돌려준다."""
        return int(rows[0][0]) if rows and rows[0] and rows[0][0] is not None else 0

    # ------------------------------------------------------------ settings_kv

    def get_setting(self, key: str, default: Any = None) -> Any:
        rows = self.db.execute("SELECT value FROM settings_kv WHERE key = ?", [key])
        return json.loads(rows[0][0]) if rows else default

    def set_setting(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO settings_kv VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [key, json.dumps(value, ensure_ascii=False)],
        )

    # ------------------------------------------------------------ source items

    def insert_source_item(self, item: SourceItem) -> bool:
        """새 자료면 True, 같은 URL 해시가 이미 있으면 False(중복 수집 방지)."""
        if self.db.execute("SELECT 1 FROM source_items WHERE url_hash = ?", [item.url_hash]):
            return False
        self.db.execute(
            "INSERT INTO source_items (id, source, url, url_hash, title, text, published_at, collected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                item.id,
                item.source,
                item.url,
                item.url_hash,
                item.title,
                item.text,
                to_db(item.published_at),
                to_db(item.collected_at),
            ],
        )
        return True

    def has_url_hash(self, url_hash: str) -> bool:
        return bool(self.db.execute("SELECT 1 FROM source_items WHERE url_hash = ?", [url_hash]))

    def record_classification(
        self,
        target_type: str,
        target_id: str,
        kind: str,
        label: str,
        confidence: float,
        classifier: str,
    ) -> None:
        self.db.execute(
            "INSERT INTO classifications VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [new_id(), target_type, target_id, kind, label, confidence, classifier, self._now()],
        )

    def set_source_relevance(self, item_id: str, relevance: float) -> None:
        self.db.execute("UPDATE source_items SET relevance = ? WHERE id = ?", [relevance, item_id])

    BODY_RETRIES = 3

    def set_source_text(self, item_id: str, text: str) -> None:
        self.db.execute("UPDATE source_items SET text = ? WHERE id = ?", [text, item_id])

    def relevant_items_without_body(self, threshold: float, limit: int) -> list[SourceItem]:
        """원문 본문을 아직 못 가져온 관련 자료(아직 콘텐츠에 쓰지 않은 것). 최근 것부터.

        가져왔거나 건너뛴 것은 빼고, 실패는 BODY_RETRIES번까지 다시 시도한다(일시 장애 대비).
        """
        rows = self.db.execute(
            f"SELECT {self._ITEM_COLS} FROM source_items s "
            "WHERE relevance >= ? AND used_in_content_id IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM classifications c WHERE c.target_type = 'source_item' "
            "  AND c.target_id = s.id AND c.kind = 'body' AND c.label IN ('fetched', 'skipped')) "
            "AND (SELECT count(*) FROM classifications c WHERE c.target_type = 'source_item' "
            "  AND c.target_id = s.id AND c.kind = 'body' AND c.label = 'failed') < ? "
            "ORDER BY collected_at DESC LIMIT ?",
            [threshold, self.BODY_RETRIES, limit],
        )
        return [self._row_to_item(r) for r in rows]

    def set_source_topic(self, item_id: str, topic: str, confidence: float) -> None:
        self.db.execute(
            "UPDATE source_items SET topic = ?, topic_confidence = ? WHERE id = ?",
            [topic, confidence, item_id],
        )

    def _row_to_item(self, r: tuple) -> SourceItem:
        return SourceItem(
            id=r[0],
            source=r[1],
            url=r[2],
            url_hash=r[3],
            title=r[4],
            text=r[5],
            published_at=from_db(r[6]),
            collected_at=from_db(r[7]),  # type: ignore[arg-type]
        )

    _ITEM_COLS = "id, source, url, url_hash, title, text, published_at, collected_at"

    def unused_relevant_items(
        self, threshold: float, limit: int = 200
    ) -> list[tuple[SourceItem, float, str | None]]:
        """아직 콘텐츠에 쓰이지 않은 관련 자료: (자료, 관련성, 주제), 최신순."""
        rows = self.db.execute(
            f"SELECT {self._ITEM_COLS}, relevance, topic FROM source_items "
            "WHERE used_in_content_id IS NULL AND relevance >= ? "
            "ORDER BY collected_at DESC LIMIT ?",
            [threshold, limit],
        )
        return [(self._row_to_item(r[:8]), float(r[8]), r[9]) for r in rows]

    def unclassified_items(self, limit: int = 200) -> list[SourceItem]:
        rows = self.db.execute(
            f"SELECT {self._ITEM_COLS} FROM source_items WHERE relevance IS NULL "
            "ORDER BY collected_at LIMIT ?",
            [limit],
        )
        return [self._row_to_item(r) for r in rows]

    def relevant_items_without_topic(self, threshold: float, limit: int = 200) -> list[SourceItem]:
        rows = self.db.execute(
            f"SELECT {self._ITEM_COLS} FROM source_items "
            "WHERE relevance >= ? AND topic IS NULL AND used_in_content_id IS NULL "
            "ORDER BY collected_at LIMIT ?",
            [threshold, limit],
        )
        return [self._row_to_item(r) for r in rows]

    def get_source_item(self, item_id: str) -> SourceItem | None:
        rows = self.db.execute(
            f"SELECT {self._ITEM_COLS} FROM source_items WHERE id = ?", [item_id]
        )
        return self._row_to_item(rows[0]) if rows else None

    def existing_source_ids(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        marks = ",".join("?" * len(ids))
        return {
            r[0] for r in self.db.execute(f"SELECT id FROM source_items WHERE id IN ({marks})", ids)
        }

    def mark_items_used(self, ids: list[str], content_id: str) -> None:
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        self.db.execute(
            f"UPDATE source_items SET used_in_content_id = ? WHERE id IN ({marks})",
            [content_id, *ids],
        )

    # ------------------------------------------------------------ contents

    def insert_content(self, content: Content, persona_topic: str | None = None) -> None:
        now = self._now()
        self.db.execute(
            "INSERT INTO contents (id, status, topic, template, generated, relevance_confidence, is_ad, "
            "review_reason, created_at, updated_at, persona_topic) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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

    def save_generated(
        self,
        content: Content,
        *,
        persona_topic: str | None,
        approved_by_policy: bool,
        item_ids: list[str],
        platforms: list[Platform],
    ) -> None:
        """생성 결과 저장을 한 트랜잭션으로: 콘텐츠, 승인 기록, 자료 사용 표시, 게시 레코드.

        중간에 실패하면 전부 취소돼 "게시 레코드 없는 승인 콘텐츠"나 "같은 자료 재사용"이 생기지 않는다.
        """
        now = self._now()
        status = "approved" if approved_by_policy else content.status
        with self.db.transaction() as cur:
            cur.execute(
                "INSERT INTO contents (id, status, topic, template, generated, relevance_confidence, "
                "is_ad, review_reason, created_at, updated_at, persona_topic, approved_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    content.id,
                    status,
                    content.generated.topic,
                    content.generated.template,
                    content.generated.model_dump_json(),
                    content.relevance_confidence,
                    content.is_ad,
                    content.review_reason,
                    to_db(content.created_at),
                    now,
                    persona_topic,
                    "policy" if approved_by_policy else None,
                ],
            )
            if item_ids:
                marks = ",".join("?" * len(item_ids))
                cur.execute(
                    f"UPDATE source_items SET used_in_content_id = ? WHERE id IN ({marks})",
                    [content.id, *item_ids],
                )
            for p in platforms:
                cur.execute(
                    "INSERT INTO publications (id, content_id, platform, status, attempts, created_at) "
                    "VALUES (?, ?, ?, 'queued', 0, ?)",
                    [new_id(), content.id, p, now],
                )

    _CONTENT_COLS = "id, status, generated, relevance_confidence, is_ad, created_at, review_reason"

    def _row_to_content(self, r: tuple) -> Content:
        return Content(
            id=r[0],
            status=r[1],
            generated=GeneratedContent.model_validate_json(r[2]),
            relevance_confidence=r[3],
            is_ad=r[4],
            created_at=from_db(r[5]),  # type: ignore[arg-type]
            review_reason=r[6],
        )

    def get_content(self, content_id: str) -> Content | None:
        rows = self.db.execute(
            f"SELECT {self._CONTENT_COLS} FROM contents WHERE id = ?", [content_id]
        )
        return self._row_to_content(rows[0]) if rows else None

    def list_contents(self, status: ContentStatus | None = None, limit: int = 100) -> list[Content]:
        if status:
            rows = self.db.execute(
                f"SELECT {self._CONTENT_COLS} FROM contents WHERE status = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                [status, limit],
            )
        else:
            rows = self.db.execute(
                f"SELECT {self._CONTENT_COLS} FROM contents ORDER BY created_at DESC, rowid DESC LIMIT ?",
                [limit],
            )
        return [self._row_to_content(r) for r in rows]

    def count_contents(self) -> int:
        return int(self.db.execute("SELECT count(*) FROM contents")[0][0])

    def count_human_approved(self) -> int:
        return int(
            self.db.execute("SELECT count(*) FROM contents WHERE approved_by = 'human'")[0][0]
        )

    def transition_content(
        self,
        content_id: str,
        new_status: ContentStatus,
        reason: str | None = None,
        approved_by: str | None = None,
    ) -> None:
        sources = _sources_for(CONTENT_TRANSITIONS, new_status)
        marks = ",".join("?" * len(sources)) or "NULL"
        # 읽고-쓰기를 한 문장으로: 허용된 이전 상태일 때만 바뀐다(동시 변경 경쟁 방지)
        changed = self._count(
            self.db.execute(
                "UPDATE contents SET status = ?, review_reason = coalesce(?, review_reason), "
                "approved_by = coalesce(?, approved_by), updated_at = ? "
                f"WHERE id = ? AND status IN ({marks})",
                [new_status, reason, approved_by, self._now(), content_id, *sources],
            )
        )
        if changed == 0:
            rows = self.db.execute("SELECT status FROM contents WHERE id = ?", [content_id])
            if not rows:
                raise KeyError(content_id)
            raise InvalidTransitionError(f"콘텐츠 {content_id}: {rows[0][0]} → {new_status} 불가")

    def update_generated(self, content_id: str, generated: GeneratedContent) -> None:
        """검토 대기열에서 사람이 고친 원고 저장."""
        self.db.execute(
            "UPDATE contents SET generated = ?, updated_at = ? WHERE id = ?",
            [generated.model_dump_json(), self._now(), content_id],
        )

    def count_contents_between(self, start: datetime, end: datetime) -> int:
        return int(
            self.db.execute(
                "SELECT count(*) FROM contents WHERE created_at >= ? AND created_at < ?",
                [to_db(start), to_db(end)],
            )[0][0]
        )

    def recent_templates(self, limit: int = 3) -> list[str]:
        """최근 콘텐츠의 템플릿, 최신순."""
        rows = self.db.execute(
            "SELECT template FROM contents ORDER BY created_at DESC, rowid DESC LIMIT ?", [limit]
        )
        return [r[0] for r in rows]

    def persona_topic_last_used(self) -> dict[str, datetime]:
        rows = self.db.execute(
            "SELECT persona_topic, max(created_at) FROM contents "
            "WHERE persona_topic IS NOT NULL GROUP BY persona_topic"
        )
        return {r[0]: from_db(r[1]) for r in rows}  # type: ignore[misc]

    def recent_topics(self, since: datetime) -> set[str]:
        rows = self.db.execute(
            "SELECT DISTINCT topic FROM contents WHERE created_at >= ? AND status != 'discarded'",
            [to_db(since)],
        )
        return {r[0] for r in rows}

    # ------------------------------------------------------------ publications

    def create_publications(self, content_id: str, platforms: list[Platform]) -> list[str]:
        ids = []
        now = self._now()
        with self.db.transaction() as cur:
            for p in platforms:
                cur.execute(
                    "SELECT 1 FROM publications WHERE content_id = ? AND platform = ?",
                    [content_id, p],
                )
                if cur.fetchall():
                    continue
                pid = new_id()
                cur.execute(
                    "INSERT INTO publications (id, content_id, platform, status, attempts, created_at) "
                    "VALUES (?, ?, ?, 'queued', 0, ?)",
                    [pid, content_id, p, now],
                )
                ids.append(pid)
        return ids

    _PUB_COLS = "id, content_id, platform, status, attempts, next_attempt_at, external_id, url, last_error, published_at"

    def _row_to_pub(self, r: tuple) -> Publication:
        return Publication(
            id=r[0],
            content_id=r[1],
            platform=r[2],
            status=r[3],
            attempts=r[4],
            next_attempt_at=from_db(r[5]),
            external_id=r[6],
            url=r[7],
            last_error=r[8],
            published_at=from_db(r[9]),
        )

    def get_publication(self, pub_id: str) -> Publication | None:
        rows = self.db.execute(f"SELECT {self._PUB_COLS} FROM publications WHERE id = ?", [pub_id])
        return self._row_to_pub(rows[0]) if rows else None

    def publications_for_content(self, content_id: str) -> list[Publication]:
        rows = self.db.execute(
            f"SELECT {self._PUB_COLS} FROM publications WHERE content_id = ? ORDER BY platform",
            [content_id],
        )
        return [self._row_to_pub(r) for r in rows]

    def due_publications(self, platform: Platform, now: datetime) -> list[Publication]:
        """승인된 콘텐츠의 게시 대기(queued) 또는 재시도 시각이 된 실패 건, 오래된 순."""
        rows = self.db.execute(
            f"SELECT {', '.join('p.' + c.strip() for c in self._PUB_COLS.split(','))} "
            "FROM publications p JOIN contents c ON c.id = p.content_id "
            "WHERE p.platform = ? AND c.status = 'approved' AND ("
            "  p.status = 'queued' OR (p.status = 'failed' AND p.next_attempt_at <= ?)) "
            "ORDER BY p.created_at",
            [platform, to_db(now)],
        )
        return [self._row_to_pub(r) for r in rows]

    def transition_publication(
        self,
        pub_id: str,
        new_status: PublicationStatus,
        *,
        external_id: str | None = None,
        url: str | None = None,
        error: str | None = None,
        next_attempt_at: datetime | None = None,
        increment_attempts: bool = False,
        refund_attempt: bool = False,  # 이번 시도를 횟수에서 뺀다(요청 제한처럼 원고 탓이 아닌 실패)
    ) -> None:
        if new_status == "failed" and next_attempt_at is None:
            raise ValueError("failed로 바꿀 때는 next_attempt_at(재시도 시각)이 필요합니다")
        sources = _sources_for(PUBLICATION_TRANSITIONS, new_status)
        marks = ",".join("?" * len(sources)) or "NULL"
        published_at = self._now() if new_status == "published" else None
        changed = self._count(
            self.db.execute(
                "UPDATE publications SET status = ?, "
                "external_id = coalesce(?, external_id), url = coalesce(?, url), "
                "last_error = CASE WHEN ? = 'published' THEN NULL ELSE coalesce(?, last_error) END, "
                "next_attempt_at = ?, attempts = attempts + ?, "
                f"published_at = coalesce(?, published_at) WHERE id = ? AND status IN ({marks})",
                [
                    new_status,
                    external_id,
                    url,
                    new_status,
                    error,
                    to_db(next_attempt_at),
                    (1 if increment_attempts else 0) - (1 if refund_attempt else 0),
                    published_at,
                    pub_id,
                    *sources,
                ],
            )
        )
        if changed == 0:
            rows = self.db.execute("SELECT status FROM publications WHERE id = ?", [pub_id])
            if not rows:
                raise KeyError(pub_id)
            raise InvalidTransitionError(f"게시 {pub_id}: {rows[0][0]} → {new_status} 불가")

    def mark_content_published_if_complete(self, content_id: str) -> bool:
        """모든 플랫폼 게시가 끝났을 때만 콘텐츠를 published로 바꾼다.

        일부 플랫폼만 게시된 상태에서 콘텐츠를 published로 바꾸면 남은 게시가
        due_publications에서 빠져 영영 올라가지 않는다(ADR 0009).
        """
        pubs = self.publications_for_content(content_id)
        if not pubs or any(p.status != "published" for p in pubs):
            return False
        self.transition_content(content_id, "published")
        return True

    def requeue_abandoned(self, content_id: str) -> int:
        """사람이 콘텐츠를 다시 승인하면 포기했던 게시를 처음부터 다시 시도한다."""
        return self._count(
            self.db.execute(
                "UPDATE publications SET status = 'queued', attempts = 0, next_attempt_at = NULL "
                "WHERE content_id = ? AND status = 'abandoned'",
                [content_id],
            )
        )

    def publications_in_status(self, status: PublicationStatus) -> list[Publication]:
        rows = self.db.execute(
            f"SELECT {self._PUB_COLS} FROM publications WHERE status = ?", [status]
        )
        return [self._row_to_pub(r) for r in rows]

    def count_published_between(self, platform: Platform, start: datetime, end: datetime) -> int:
        return int(
            self.db.execute(
                "SELECT count(*) FROM publications WHERE platform = ? AND status = 'published' "
                "AND published_at >= ? AND published_at < ?",
                [platform, to_db(start), to_db(end)],
            )[0][0]
        )

    def last_published_at(self, platform: Platform) -> datetime | None:
        row = self.db.execute(
            "SELECT max(published_at) FROM publications WHERE platform = ? AND status = 'published'",
            [platform],
        )
        return from_db(row[0][0]) if row and row[0][0] else None

    def published_posts_since(self, platform: Platform, since: datetime) -> list[Publication]:
        rows = self.db.execute(
            f"SELECT {self._PUB_COLS} FROM publications WHERE platform = ? AND status = 'published' "
            "AND published_at >= ? ORDER BY published_at DESC",
            [platform, to_db(since)],
        )
        return [self._row_to_pub(r) for r in rows]

    def content_for_post(self, platform: Platform, external_id: str) -> Content | None:
        rows = self.db.execute(
            "SELECT content_id FROM publications WHERE platform = ? AND external_id = ?",
            [platform, external_id],
        )
        return self.get_content(rows[0][0]) if rows else None

    # ------------------------------------------------------------ comments

    @staticmethod
    def comment_key(c: Comment) -> str:
        return f"{c.platform}:{c.external_id}"

    def comment_exists(self, c: Comment) -> bool:
        return bool(self.db.execute("SELECT 1 FROM comments WHERE id = ?", [self.comment_key(c)]))

    def insert_comment(self, c: Comment) -> bool:
        if self.comment_exists(c):
            return False
        self.db.execute(
            "INSERT INTO comments (id, platform, external_id, post_external_id, author_id, author_name, "
            "text, created_at, fetched_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')",
            [
                self.comment_key(c),
                c.platform,
                c.external_id,
                c.post_external_id,
                c.author_id,
                c.author_name,
                c.text,
                to_db(c.created_at),
                self._now(),
            ],
        )
        return True

    def update_comment(
        self,
        key: str,
        *,
        status: str,
        category: CommentCategory | None = None,
        confidence: float | None = None,
        action: CommentAction | None = None,
        reason: str | None = None,
        reply_text: str | None = None,
        reply_external_id: str | None = None,
        handled_by: str | None = None,
    ) -> None:
        handled_at = self._now() if handled_by else None
        self.db.execute(
            "UPDATE comments SET status = ?, category = coalesce(?, category), "
            "confidence = coalesce(?, confidence), action = coalesce(?, action), "
            "reason = coalesce(?, reason), reply_text = coalesce(?, reply_text), "
            "reply_external_id = coalesce(?, reply_external_id), "
            "handled_at = coalesce(?, handled_at), handled_by = coalesce(?, handled_by) WHERE id = ?",
            [
                status,
                category,
                confidence,
                action,
                reason,
                reply_text,
                reply_external_id,
                handled_at,
                handled_by,
                key,
            ],
        )

    _COMMENT_COLS = (
        "id, platform, external_id, post_external_id, author_id, author_name, text, created_at, "
        "status, category, confidence, action, reason, reply_text, handled_at, handled_by"
    )

    def _row_to_stored(self, r: tuple) -> StoredComment:
        return StoredComment(
            id=r[0],
            comment=Comment(
                platform=r[1],
                external_id=r[2],
                post_external_id=r[3],
                author_id=r[4],
                author_name=r[5],
                text=r[6],
                created_at=from_db(r[7]),  # type: ignore[arg-type]
            ),
            status=r[8],
            category=r[9],
            confidence=r[10],
            action=r[11],
            reason=r[12],
            reply_text=r[13],
            handled_at=from_db(r[14]),
            handled_by=r[15],
        )

    def get_comment(self, key: str) -> StoredComment | None:
        rows = self.db.execute(f"SELECT {self._COMMENT_COLS} FROM comments WHERE id = ?", [key])
        return self._row_to_stored(rows[0]) if rows else None

    def list_comments(self, status: str | None = None, limit: int = 200) -> list[StoredComment]:
        if status:
            rows = self.db.execute(
                f"SELECT {self._COMMENT_COLS} FROM comments WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                [status, limit],
            )
        else:
            rows = self.db.execute(
                f"SELECT {self._COMMENT_COLS} FROM comments ORDER BY created_at DESC LIMIT ?",
                [limit],
            )
        return [self._row_to_stored(r) for r in rows]

    def latest_comment_time(self, platform: Platform) -> datetime | None:
        row = self.db.execute("SELECT max(created_at) FROM comments WHERE platform = ?", [platform])
        return from_db(row[0][0]) if row and row[0][0] else None

    def reply_count(self, platform: Platform, author_id: str, day: date) -> int:
        rows = self.db.execute(
            "SELECT count FROM reply_counts WHERE platform = ? AND author_id = ? AND day = ?",
            [platform, author_id, day],
        )
        return int(rows[0][0]) if rows else 0

    def increment_reply_count(self, platform: Platform, author_id: str, day: date) -> None:
        self.db.execute(
            "INSERT INTO reply_counts VALUES (?, ?, ?, 1) ON CONFLICT (platform, author_id, day) "
            "DO UPDATE SET count = reply_counts.count + 1",
            [platform, author_id, day],
        )

    # ------------------------------------------------------------ metrics

    def insert_post_metrics(self, m: PostMetrics) -> None:
        self.db.execute(
            "INSERT INTO metrics_posts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                m.platform,
                m.post_external_id,
                to_db(m.captured_at),
                m.views,
                m.reach,
                m.impressions,
                m.likes,
                m.comments,
                m.saves,
                m.shares,
            ],
        )

    def insert_account_metrics(self, m: AccountMetrics) -> None:
        self.db.execute(
            "INSERT INTO metrics_account VALUES (?, ?, ?)",
            [m.platform, to_db(m.captured_at), m.followers],
        )

    # ------------------------------------------------------------ costs (CostSink)

    def record_cost(self, kind: CostKind, provider: str, usd: float, ref: str = "") -> None:
        self.db.execute(
            "INSERT INTO costs VALUES (?, ?, ?, ?, ?, ?)",
            [new_id(), kind, provider, float(usd), self._now(), ref],
        )

    def cost_between(self, start: datetime, end: datetime) -> float:
        row = self.db.execute(
            "SELECT coalesce(sum(usd), 0) FROM costs WHERE occurred_at >= ? AND occurred_at < ?",
            [to_db(start), to_db(end)],
        )
        return float(row[0][0])

    # ------------------------------------------------------------ run logs

    def start_run(self, job: str) -> str:
        rid = new_id()
        self.db.execute(
            "INSERT INTO run_logs (id, job, started_at) VALUES (?, ?, ?)", [rid, job, self._now()]
        )
        return rid

    def finish_run(self, run_id: str, ok: bool, message: str = "") -> None:
        self.db.execute(
            "UPDATE run_logs SET finished_at = ?, ok = ?, message = ? WHERE id = ?",
            [self._now(), ok, message[:2000], run_id],
        )

    def recent_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT job, started_at, finished_at, ok, message FROM run_logs ORDER BY started_at DESC, rowid DESC LIMIT ?",
            [limit],
        )
        return [
            {
                "job": r[0],
                "started_at": from_db(r[1]),
                "finished_at": from_db(r[2]),
                "ok": r[3],
                "message": r[4],
            }
            for r in rows
        ]

    # ------------------------------------------------------------ notifications (알림함, N2)

    def add_notification(self, title: str, body: str, level: str, link: str | None) -> str:
        nid = new_id()
        self.db.execute(
            "INSERT INTO notifications (id, created_at, title, body, level, link) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [nid, self._now(), title[:200], body[:1000], level, link],
        )
        return nid

    def list_notifications(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT id, created_at, title, body, level, link, read_at FROM notifications "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            [limit],
        )
        return [
            {
                "id": r[0],
                "created_at": from_db(r[1]),
                "title": r[2],
                "body": r[3],
                "level": r[4],
                "link": r[5],
                "read": r[6] is not None,
            }
            for r in rows
        ]

    def unread_notification_count(self) -> int:
        rows = self.db.execute("SELECT count(*) FROM notifications WHERE read_at IS NULL")
        return int(rows[0][0])

    def mark_notification_read(self, nid: str) -> str | None:
        """읽음 처리하고 그 알림의 링크를 돌려준다. 없는 알림이면 None."""
        rows = self.db.execute("SELECT link FROM notifications WHERE id = ?", [nid])
        if not rows:
            return None
        self.db.execute(
            "UPDATE notifications SET read_at = ? WHERE id = ? AND read_at IS NULL",
            [self._now(), nid],
        )
        return rows[0][0] or "/"

    def mark_all_notifications_read(self) -> int:
        return self._count(
            self.db.execute(
                "UPDATE notifications SET read_at = ? WHERE read_at IS NULL", [self._now()]
            )
        )

    def prune_notifications(self, keep: int = 200) -> None:
        """오래된 알림은 지운다(최근 keep개만)."""
        self.db.execute(
            "DELETE FROM notifications WHERE id NOT IN "
            "(SELECT id FROM notifications ORDER BY created_at DESC, rowid DESC LIMIT ?)",
            [keep],
        )

    # ------------------------------------------------------------ 자료 화면 (M18)

    def list_source_items_view(
        self, source: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """자료 화면용: 자료와 관련성 판정(방법 포함), 주제, 쓰인 원고. source는 'naver'·'upload'·접두어."""
        where, params = "", []
        if source:
            where = "WHERE s.source = ? OR s.source LIKE ?"
            params = [source, f"{source}:%"]
        rows = self.db.execute(
            "SELECT s.id, s.source, s.url, s.title, length(s.text), s.collected_at, s.relevance, "
            "s.topic, s.used_in_content_id, c.label, c.classifier "
            "FROM source_items s LEFT JOIN ("
            "  SELECT target_id, arg_max(label, created_at) AS label, "
            "         arg_max(classifier, created_at) AS classifier "
            "  FROM classifications WHERE target_type = 'source_item' AND kind = 'relevance' "
            "  GROUP BY target_id) c ON c.target_id = s.id "
            f"{where} ORDER BY s.collected_at DESC, s.rowid DESC LIMIT ?",
            [*params, limit],
        )
        return [
            {
                "id": r[0],
                "source": r[1],
                "url": r[2],
                "title": r[3],
                "chars": int(r[4] or 0),
                "collected_at": from_db(r[5]),
                "relevance": r[6],
                "topic": r[7],
                "used_in": r[8],
                "label": r[9],
                "classifier": r[10],
            }
            for r in rows
        ]

    def source_counts(self) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT CASE WHEN source LIKE 'community:%' THEN 'community' ELSE source END, count(*) "
            "FROM source_items GROUP BY 1"
        )
        return {r[0]: int(r[1]) for r in rows}
