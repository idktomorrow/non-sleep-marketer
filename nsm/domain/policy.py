"""정책 규칙: 입력 → 결정만 하는 순수 함수 (docs/contracts.md 4절).

외부 호출·DB 접근이 없어서 테스트가 쉽고, 규칙을 바꾸면 이 파일 하나만 고친다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from nsm.domain.models import Persona, Platform, TemplateKind


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


# ------------------------------------------------------------------ 콘텐츠 검사·게시 판정


def find_banned(texts: Iterable[str], persona: Persona) -> list[str]:
    """금칙어(부분 일치, 대소문자 무시)와 금칙 패턴(정규식)에 걸린 문구 목록."""
    hits: list[str] = []
    joined = "\n".join(texts)
    low = joined.lower()
    for w in persona.banned_words:
        if w and w.lower() in low and w not in hits:
            hits.append(w)
    for pat in persona.banned_patterns:
        try:
            m = re.search(pat, joined)
        except re.error:
            continue
        if m and m.group(0) not in hits:
            hits.append(m.group(0))
    return hits


@dataclass(frozen=True)
class PublishDecision:
    status: str  # "approved" | "pending_review"
    reasons: list[str] = field(default_factory=list)

    @property
    def reason_text(self) -> str | None:
        return "; ".join(self.reasons) or None


def decide_publish(
    *,
    claim_source_ids: list[str],
    existing_source_ids: set[str],
    relevance: float,
    threshold: float,
    banned_hits: list[str],
    auto_publish_enabled: bool,
    human_approved: int,
    protect_count: int,
) -> PublishDecision:
    """자동 게시 판정 (ADR 0008). 하나라도 걸리면 사유와 함께 검토 대기열."""
    reasons: list[str] = []
    if not auto_publish_enabled:
        reasons.append("자동 게시가 꺼져 있음")
    elif human_approved < protect_count:
        reasons.append(f"보호 모드: 사람 승인 {human_approved}/{protect_count}건")
    if not claim_source_ids:
        reasons.append("출처가 연결된 사실 주장이 없음")
    missing = [i for i in claim_source_ids if i not in existing_source_ids]
    if missing:
        reasons.append(f"출처가 확인되지 않는 주장 {len(missing)}건")
    if relevance < threshold:
        reasons.append(f"자료 관련성 {relevance:.2f} < {threshold:.2f}")
    if banned_hits:
        reasons.append("금칙어·금칙 패턴: " + ", ".join(banned_hits[:5]))
    return PublishDecision("pending_review" if reasons else "approved", reasons)


def daily_content_target(enabled: list[Platform], quotas: dict[Platform, int]) -> int:
    return max((quotas.get(p, 0) for p in enabled), default=0)


def assign_platforms(
    index_today: int, enabled: list[Platform], quotas: dict[Platform, int]
) -> list[Platform]:
    """오늘 n번째(0부터) 콘텐츠를 올릴 플랫폼. 쿼터가 남은 플랫폼에만 배정해 밀린 게시가 쌓이지 않게 한다."""
    return [p for p in enabled if quotas.get(p, 0) > index_today]


TEMPLATES: tuple[TemplateKind, ...] = ("info", "list", "compare")


def choose_template(recent_newest_first: list[str]) -> TemplateKind:
    """최근에 안 쓴 템플릿부터. 피드가 한 가지 형식으로 반복되지 않게 한다."""
    for t in TEMPLATES:
        if t not in recent_newest_first:
            return t
    return max(TEMPLATES, key=recent_newest_first.index)


def under_cost_cap(spent_today: float, cap: float) -> bool:
    return spent_today < cap
