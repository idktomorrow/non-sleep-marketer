"""대시보드용 읽기 전용 집계 쿼리. 쓰기는 Repository만 한다."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from nsm.store.db import Database, from_db, to_db


def _rows(db: Database, sql: str, params: list | None = None) -> list[dict[str, Any]]:
    cur = db.cursor()
    try:
        cur.execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    finally:
        cur.close()


def overview(db: Database, day_start: datetime, day_end: datetime) -> dict[str, Any]:
    one = lambda sql, p=None: db.execute(sql, p or [])[0][0]  # noqa: E731
    published_today = _rows(
        db,
        "SELECT platform, count(*) AS n FROM publications WHERE status = 'published' "
        "AND published_at >= ? AND published_at < ? GROUP BY platform",
        [to_db(day_start), to_db(day_end)],
    )
    return {
        "pending_review": one("SELECT count(*) FROM contents WHERE status = 'pending_review'"),
        "approved_waiting": one("SELECT count(*) FROM contents WHERE status = 'approved'"),
        "escalated_comments": one("SELECT count(*) FROM comments WHERE status = 'escalated'"),
        "published_today": {r["platform"]: r["n"] for r in published_today},
        "cost_today": float(
            one(
                "SELECT coalesce(sum(usd), 0) FROM costs WHERE occurred_at >= ? AND occurred_at < ?",
                [to_db(day_start), to_db(day_end)],
            )
        ),
        "sources_unused": one(
            "SELECT count(*) FROM source_items WHERE used_in_content_id IS NULL AND relevance >= 0.8"
        ),
    }


def _offset_seconds(tz: str, at: datetime) -> int:
    """UTC로 저장된 시각을 현지 날짜로 묶기 위한 오프셋(초)."""
    off = at.astimezone(ZoneInfo(tz)).utcoffset()
    return int(off.total_seconds()) if off else 0


def costs_by_day(db: Database, since: datetime, tz: str = "Asia/Seoul") -> list[dict[str, Any]]:
    return _rows(
        db,
        "SELECT CAST(occurred_at + to_seconds(?) AS DATE) AS day, kind, round(sum(usd), 4) AS usd "
        "FROM costs WHERE occurred_at >= ? GROUP BY 1, 2 ORDER BY 1 DESC, 2",
        [_offset_seconds(tz, since), to_db(since)],
    )


def post_performance(db: Database, limit: int = 50) -> list[dict[str, Any]]:
    """게시물별 최신 지표 + 콘텐츠 정보."""
    rows = _rows(
        db,
        "WITH latest AS ("
        "  SELECT *, row_number() OVER (PARTITION BY platform, post_external_id ORDER BY captured_at DESC) AS rn"
        "  FROM metrics_posts) "
        "SELECT p.platform, p.url, p.published_at, c.topic, c.template, c.is_ad, "
        "  m.views, m.reach, m.impressions, m.likes, m.comments, m.saves, m.shares "
        "FROM publications p JOIN contents c ON c.id = p.content_id "
        "LEFT JOIN latest m ON m.platform = p.platform AND m.post_external_id = p.external_id AND m.rn = 1 "
        "WHERE p.status = 'published' ORDER BY p.published_at DESC LIMIT ?",
        [limit],
    )
    for r in rows:
        r["published_at"] = from_db(r["published_at"])
    return rows


def follower_series(db: Database, since: datetime, tz: str = "Asia/Seoul") -> list[dict[str, Any]]:
    return _rows(
        db,
        "SELECT platform, CAST(captured_at + to_seconds(?) AS DATE) AS day, max(followers) AS followers "
        "FROM metrics_account WHERE captured_at >= ? GROUP BY 1, 2 ORDER BY 2, 1",
        [_offset_seconds(tz, since), to_db(since)],
    )


def comment_stats(db: Database, since: datetime) -> dict[str, Any]:
    by_status = _rows(
        db,
        "SELECT status, count(*) AS n FROM comments WHERE fetched_at >= ? GROUP BY 1",
        [to_db(since)],
    )
    by_category = _rows(
        db,
        "SELECT coalesce(category, '미분류') AS category, count(*) AS n FROM comments "
        "WHERE fetched_at >= ? GROUP BY 1 ORDER BY 2 DESC",
        [to_db(since)],
    )
    avg = db.execute(
        "SELECT avg(epoch(handled_at) - epoch(created_at)) / 60.0 FROM comments "
        "WHERE status = 'auto_replied' AND fetched_at >= ?",
        [to_db(since)],
    )[0][0]
    return {
        "by_status": {r["status"]: r["n"] for r in by_status},
        "by_category": by_category,
        "avg_response_minutes": round(float(avg), 1) if avg is not None else None,
    }


def default_since(now: datetime, days: int = 30) -> datetime:
    return now - timedelta(days=days)


def posts_between(db: Database, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """기간 [start, end) 에 게시된 글과 최신 지표. 주간 리포트용."""
    rows = _rows(
        db,
        "WITH latest AS ("
        "  SELECT *, row_number() OVER (PARTITION BY platform, post_external_id ORDER BY captured_at DESC) AS rn"
        "  FROM metrics_posts) "
        "SELECT p.platform, p.url, p.published_at, c.topic, c.template, c.is_ad, c.persona_topic, "
        "  m.views, m.likes, m.comments, m.saves, m.shares "
        "FROM publications p JOIN contents c ON c.id = p.content_id "
        "LEFT JOIN latest m ON m.platform = p.platform AND m.post_external_id = p.external_id AND m.rn = 1 "
        "WHERE p.status = 'published' AND p.published_at >= ? AND p.published_at < ?",
        [to_db(start), to_db(end)],
    )
    for r in rows:
        r["published_at"] = from_db(r["published_at"])
    return rows


def follower_change(db: Database, start: datetime, end: datetime) -> dict[str, tuple[int, int]]:
    """플랫폼별 (기간 첫 팔로워 수, 마지막 팔로워 수). 읽지 못한(NULL) 기록은 뺀다."""
    rows = db.execute(
        "SELECT platform, arg_min(followers, captured_at), arg_max(followers, captured_at) "
        "FROM metrics_account WHERE followers IS NOT NULL AND captured_at >= ? AND captured_at < ? "
        "GROUP BY platform",
        [to_db(start), to_db(end)],
    )
    return {r[0]: (int(r[1]), int(r[2])) for r in rows}


def cost_by_kind(db: Database, start: datetime, end: datetime) -> dict[str, float]:
    rows = db.execute(
        "SELECT kind, round(sum(usd), 2) FROM costs WHERE occurred_at >= ? AND occurred_at < ? GROUP BY 1",
        [to_db(start), to_db(end)],
    )
    return {r[0]: float(r[1]) for r in rows}


def unused_counts_by_topic(db: Database, threshold: float) -> dict[str, int]:
    """아직 콘텐츠에 쓰지 않은 관련 자료 수(주제별, 상한 없이 전부)."""
    rows = db.execute(
        "SELECT topic, count(*) FROM source_items WHERE used_in_content_id IS NULL "
        "AND relevance >= ? AND topic IS NOT NULL GROUP BY topic",
        [threshold],
    )
    return {r[0]: int(r[1]) for r in rows}
