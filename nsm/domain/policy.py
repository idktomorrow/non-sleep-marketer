"""정책 규칙: 입력 → 결정만 하는 순수 함수 (docs/contracts.md 4절).

외부 호출·DB 접근이 없어서 테스트가 쉽고, 규칙을 바꾸면 이 파일 하나만 고친다.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from nsm.domain.models import Persona


def local(now: datetime, tz: str) -> datetime:
    return now.astimezone(ZoneInfo(tz))


def _in_window(t: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # 자정을 넘는 구간 (예: 22:00-02:00)


def in_active_hours(now: datetime, persona: Persona, tz: str) -> bool:
    t = local(now, tz).time()
    return any(_in_window(t, w.start, w.end) for w in persona.active_hours)


def should_act(now: datetime, persona: Persona, tz: str) -> tuple[bool, str]:
    """수집·생성·게시·답글 잡이 지금 움직여도 되는지와 그 이유."""
    if not persona.enabled:
        return False, "페르소나가 꺼져 있음"
    if not in_active_hours(now, persona, tz):
        return False, "활동 시간 밖"
    return True, ""


def local_day_bounds(now: datetime, tz: str) -> tuple[datetime, datetime]:
    """현지 날짜 기준 오늘 0시 ~ 내일 0시 (aware)."""
    loc = local(now, tz)
    start = loc.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)
