"""성과 화면 맨 위 요약 카드(배치 C45, ADR 0072).

새 지표를 계산하지 않는다. 화면 아래 표가 이미 쓰는 값(팔로워 날짜별, 최근 14일 비용, 30일 댓글 처리)을 한 줄로 올린다.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from nsm.engine.summary import PLATFORMS

_ORDER = ("instagram", "threads")
FOLLOWER_DAYS = 7  # "7일 전보다"
FOLLOWER_SLACK = 3  # 7일 전 기록이 없으면 그 전 3일 안의 가장 가까운 날까지만(더 오래된 값은 "7일" 변화가 아니다)


def _key(platform: str) -> tuple[int, str]:
    return (_ORDER.index(platform) if platform in _ORDER else len(_ORDER), platform)


def followers_card(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """팔로워 날짜별 행 → 플랫폼마다 {name, now, delta}. delta는 7일 전과의 차.

    7일 전 기록이 없으면 그 전 3일(8~10일 전) 안의 가장 가까운 날과 비교한다. 거기에도 없으면 delta는 None
    (화면에서 변화를 뺀다).
    """
    by_pl: dict[str, list[tuple[date, int]]] = {}
    for r in rows:
        if r.get("followers") is None or r.get("day") is None:
            continue
        by_pl.setdefault(r["platform"], []).append((r["day"], int(r["followers"])))
    out = []
    for pl in sorted(by_pl, key=_key):
        series = sorted(by_pl[pl])
        last_day, now = series[-1]
        oldest = last_day - timedelta(days=FOLLOWER_DAYS + FOLLOWER_SLACK)
        before = [n for d, n in series if oldest <= d <= last_day - timedelta(days=FOLLOWER_DAYS)]
        out.append(
            {
                "name": PLATFORMS.get(pl, pl),
                "now": now,
                "delta": now - before[-1] if before else None,
            }
        )
    return out


def summary_cards(
    followers: list[dict[str, Any]], costs: list[dict[str, Any]], comment_stats: dict[str, Any]
) -> dict[str, Any]:
    by_status = comment_stats.get("by_status") or {}
    return {
        "followers": followers_card(followers),
        "cost_usd": round(sum(float(r.get("usd") or 0) for r in costs), 4),
        "auto_replied": int(by_status.get("auto_replied", 0)),
        "human": int(by_status.get("resolved", 0)),
        "avg_minutes": comment_stats.get("avg_response_minutes"),
    }
