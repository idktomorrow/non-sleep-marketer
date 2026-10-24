"""정책 규칙: 입력 → 결정만 하는 순수 함수 (docs/contracts.md 4절).

외부 호출·DB 접근이 없어서 테스트가 쉽고, 규칙을 바꾸면 이 파일 하나만 고친다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from nsm.domain import labels as L
from nsm.domain.models import (
    ActiveWindow,
    CommentAction,
    CommentCategory,
    Persona,
    Platform,
    TemplateKind,
)


def local(now: datetime, tz: str) -> datetime:
    return now.astimezone(ZoneInfo(tz))


def _in_window(t: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # 자정을 넘는 구간 (예: 22:00-02:00)


def in_windows(now: datetime, windows: Iterable[ActiveWindow], tz: str) -> bool:
    t = local(now, tz).time()
    return any(_in_window(t, w.start, w.end) for w in windows)


def in_active_hours(now: datetime, persona: Persona, tz: str) -> bool:
    return in_windows(now, persona.active_hours, tz)


def next_window_start(now: datetime, windows: Iterable[ActiveWindow], tz: str) -> datetime | None:
    """지금이 구간 안이면 now, 아니면 다음으로 구간이 열리는 시각(현지 시간대, aware). 구간이 없으면 None(C26)."""
    ws = list(windows)
    if not ws:
        return None
    if in_windows(now, ws, tz):
        return now
    loc = local(now, tz)
    best: datetime | None = None
    for day in (0, 1):
        base = (loc + timedelta(days=day)).replace(hour=0, minute=0, second=0, microsecond=0)
        for w in ws:
            start = base.replace(hour=w.start.hour, minute=w.start.minute)
            if start > loc and (best is None or start < best):
                best = start
    return best


def should_act(now: datetime, persona: Persona, tz: str) -> tuple[bool, str]:
    """수집·생성·게시·답글 잡이 지금 움직여도 되는지와 그 이유."""
    if not persona.enabled:
        return False, "페르소나가 꺼져 있음"
    if not in_active_hours(now, persona, tz):
        return False, "활동 시간 밖"
    return True, ""


def should_post(
    now: datetime, persona: Persona, tz: str, posting_hours: Iterable[ActiveWindow] | None
) -> tuple[bool, str]:
    """게시 잡만: 활동 시간 안이면서 게시 시간 안(설정이 없으면 활동 시간과 같음, ADR 0027)."""
    ok, why = should_act(now, persona, tz)
    if ok and posting_hours is not None and not in_windows(now, posting_hours, tz):
        return False, "게시 시간 밖"
    return ok, why


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
    for pat in persona.banned_patterns:  # Persona 검증에서 컴파일 가능함이 보장된다
        m = re.search(pat, joined)
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


# ------------------------------------------------------------------ 표기 (ADR 0007)


def apply_labels(text: str, is_ad: bool) -> str:
    """게시물 첫 줄에 가상인물 문구, 협찬이면 그 앞에 #광고. 이미 붙어 있으면 다시 붙이지 않는다."""
    body = text.lstrip()
    while True:  # 이미 붙은 표기(공백·줄바꿈 변형 포함)를 모두 걷어낸다
        if body.startswith(L.AD_HASHTAG):
            body = body[len(L.AD_HASHTAG) :].lstrip()
        elif old := next(
            (
                x
                for x in (L.VIRTUAL_PERSON_TEXT, *L.LEGACY_VIRTUAL_PERSON_TEXTS)
                if body.startswith(x)
            ),
            None,
        ):
            body = body[len(old) :].lstrip()
        else:
            break
    head = f"{L.AD_HASHTAG} {L.VIRTUAL_PERSON_TEXT}" if is_ad else L.VIRTUAL_PERSON_TEXT
    return f"{head}\n\n{body}"


# 플랫폼 본문 글자 수 상한(표기 포함). 네이버 블로그는 사실상 제한이 없어 넉넉히 둔다.
TEXT_LIMITS: dict[str, int] = {"threads": 500, "instagram": 2200, "naver_blog": 50_000}


def fit_with_tail(text: str, platform: str, tail: str) -> str:
    """꼬리(출처 줄)는 그대로 두고 앞부분만 줄여 상한에 맞춘다. 표기 문구는 앞에 있어 항상 남는다."""
    limit = TEXT_LIMITS.get(platform)
    if not tail:
        return fit_to_limit(text, platform)
    if limit is None or len(text) + len(tail) <= limit:
        return text + tail
    return text[: max(0, limit - len(tail) - 1)].rstrip() + "…" + tail


def fit_to_limit(text: str, platform: str) -> str:
    """상한을 넘으면 끝을 잘라 말줄임표. 앞쪽 표기 문구는 항상 남는다."""
    limit = TEXT_LIMITS.get(platform)
    if limit is None or len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def labeled_blog_title(title: str) -> str:
    return title if title.endswith(L.BLOG_TITLE_SUFFIX) else f"{title}{L.BLOG_TITLE_SUFFIX}"


# ------------------------------------------------------------------ 댓글 처리 (contracts 4절)

ESCALATE: frozenset[str] = frozenset({"complaint", "price_refund", "legal"})
# 불만 댓글의 범위(C32, ADR 0052): 우리 브랜드·제품·거래에 대한 불만이면 사람에게, 소식에 대한 반응이면 공감 답글
COMPLAINT_SCOPES: tuple[str, ...] = ("brand_complaint", "topic_reaction")
SPAM_IGNORED_REASON = "스팸 추정(답하지 않고 자동 처리)"


def decide_comment(
    category: CommentCategory,
    author_replies_today: int,
    daily_limit: int,
    capabilities: set[str],
    complaint_scope: str | None = None,
) -> tuple[CommentAction, str]:
    """댓글 유형 → 처리 방법과 사유.

    complaint_scope: 불만 댓글의 2차 판정. "topic_reaction"(소식에 대한 반응)이면 자동 답글,
    "brand_complaint"이거나 모르면(None) 사람에게. 돈·약속이 걸린 가격·환불·법적 문제는 늘 사람에게.
    """
    if category == "complaint" and complaint_scope != "topic_reaction":
        return "escalate", (
            "브랜드·거래에 대한 불만"
            if complaint_scope == "brand_complaint"
            else "불만(브랜드 관련인지 불확실)"
        )
    if category in ESCALATE and category != "complaint":
        return "escalate", {"price_refund": "가격·환불 문의", "legal": "법적 문제"}[category]
    if category == "spam":
        if "hide_comment" in capabilities:
            return "hide", "스팸"
        # 숨기기가 없는 플랫폼(인스타그램): 답하지 않는 게 정답이라 사람이 확인할 일이 아니다(C32)
        return "ignore", SPAM_IGNORED_REASON
    if author_replies_today >= daily_limit:
        return "ignore", f"같은 사람에게 오늘 이미 {daily_limit}번 답함"
    if "reply" not in capabilities:
        return "escalate", "이 플랫폼은 자동 답글을 지원하지 않음"
    return "auto_reply", ""
