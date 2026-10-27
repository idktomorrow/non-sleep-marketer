"""플랫폼 요청 제한(HTTP 429) 다루기 (ADR 0038).

플랫폼이 "요청이 너무 많다"며 페이지를 막으면, 어댑터가 정한 시각(`retry_at`)까지 그 플랫폼에
브라우저 요청을 보내지 않는다. 로그인 문제가 아니므로 플랫폼을 멈추거나 "로그인 필요"를 알리지 않는다.
포트는 동결이라 어댑터 예외 클래스를 가져오지 않고 속성(`retry_at`, `blocked_until`)으로 알아본다.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from nsm.engine.context import JobContext

NOTIFIED_KEY = "rate_limit_notified"  # {플랫폼: 쉬는 끝 시각} 같은 쉬는 구간에 알림을 한 번만

NAMES = {"instagram": "인스타그램", "threads": "스레드", "naver_blog": "네이버 블로그"}


def _subject(name: str) -> str:
    """이름 + 주격 조사(받침 있으면 '이', 없으면 '가')."""
    last = name[-1]
    has_final = "가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 != 0
    return name + ("이" if has_final else "가")


def retry_at(exc: BaseException) -> datetime | None:
    """요청 제한 예외면 다시 해도 되는 시각."""
    t = getattr(exc, "retry_at", None)
    return t if isinstance(t, datetime) else None


def rest_until(exc: BaseException) -> datetime | None:
    """오류 종류 표(ADR 0054)에서 "정한 시각까지 쉬었다 다시"인 오류면 그 시각(요청 제한)."""
    from nsm.domain.errors import policy_for

    return retry_at(exc) if policy_for(exc).retry == "until" else None


def blocked_until(adapter: object, now: datetime) -> datetime | None:
    """어댑터가 요청 제한으로 쉬는 중이면 그 끝 시각."""
    t = getattr(adapter, "blocked_until", None)
    return t if isinstance(t, datetime) and t > now else None


def resting(ctx: JobContext, platform: str, until: datetime) -> str:
    """사람이 읽을 한 줄을 돌려주고, 이 쉬는 구간에 처음이면 알린다."""
    name = NAMES.get(platform, platform)
    at = until.astimezone(ZoneInfo(ctx.tz)).strftime("%H:%M")
    seen = dict(ctx.repo.get_setting(NOTIFIED_KEY, {}) or {})
    if seen.get(platform) != until.isoformat():
        seen[platform] = until.isoformat()
        ctx.repo.set_setting(NOTIFIED_KEY, seen)
        ctx.notifier.notify(
            f"{name} 요청 제한",
            f"{_subject(name)} 요청이 너무 많다고 잠시 막았습니다(HTTP 429). {at}까지 {name} 게시·댓글 확인을 "
            "쉬고, 그 뒤 자동으로 다시 합니다. 로그인은 풀리지 않았습니다.",
            "warn",
        )
    return f"{name} 요청 제한으로 쉬는 중({at} 이후 다시)"
