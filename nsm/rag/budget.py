"""Gemini 임베딩 하루 예산(R2 리뷰, ADR 0057). 색인 잡·`nsm index`·`nsm ask`가 같은 판단을 쓴다.

- 무료 구간 하루 한도(1,000건)는 태평양 시간 자정에 풀린다. 그래서 날짜도 태평양 시간으로 센다.
- 보낸 건수를 설정 저장소(`gemini_embed_used`)에 적어, "지금 실행"이나 `nsm ask`가 같은 날 한도를 두 번 쓰지 않게 한다.
- 키가 없거나 하루 비용 상한이면 임베딩하지 않는다(조각은 저장).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from nsm.domain.policy import local_day_bounds, under_cost_cap

PT = ZoneInfo("America/Los_Angeles")
USED_KEY = "gemini_embed_used"  # {"day": "2026-10-30", "n": 123}


def quota_day(now: datetime) -> str:
    return now.astimezone(PT).date().isoformat()


def used_today(repo: Any, now: datetime) -> int:
    v = repo.get_setting(USED_KEY, {}) or {}
    return int(v.get("n", 0)) if isinstance(v, dict) and v.get("day") == quota_day(now) else 0


def add_used(repo: Any, now: datetime, n: int) -> None:
    if n > 0:
        repo.set_setting(USED_KEY, {"day": quota_day(now), "n": used_today(repo, now) + n})


@dataclass
class Plan:
    embedder: Any | None  # None이면 임베딩하지 않는다(조각만)
    max_embed: int  # 이번에 보낼 수 있는 최대 조각 수
    note: str = ""  # 조용히 끝나는 이유(실행 기록·안내 문구)


def plan_embedding(cfg: Any, repo: Any, embedder: Any | None, now: datetime, want: int) -> Plan:
    """키·비용 상한·하루 한도를 보고 이번에 몇 개까지 보낼지. want: 원하는 최대(실행당 상한 또는 질문 1)."""
    s = cfg.settings
    if embedder is None:
        return Plan(None, 0, "Gemini 키 없음(조각만 저장)")
    start, end = local_day_bounds(now, s.timezone)
    if not under_cost_cap(repo.cost_between(start, end), s.daily_cost_cap_usd):
        return Plan(None, 0, "오늘 비용 상한(조각만 저장)")
    left = s.rag.embed_daily_limit - used_today(repo, now)
    if left <= 0:
        return Plan(None, 0, "오늘 Gemini 하루 한도를 이미 씀(내일 이어서)")
    return Plan(embedder, min(want, left))


def close_embedder(embedder: Any | None) -> None:
    close = getattr(getattr(embedder, "client", None), "close", None)
    if close:
        close()
