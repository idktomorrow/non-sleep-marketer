"""대시보드 "오늘": 승인된 원고가 언제 올라갈지(플랫폼별 다음 게시 시각, C26).

게시 잡(engine/publish.run_publish)이 보는 조건과 같은 순서로 "가장 이른 시각"을 센다.
정확한 예약이 아니라 "이 시각 이후 첫 게시 차례"다(게시 잡은 30분마다 돈다).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from nsm.domain.policy import local, local_day_bounds, next_window_start
from nsm.engine.publish import PAUSED_KEY


@dataclass(frozen=True)
class NextPublish:
    platform: str
    at: datetime | None  # None이면 reason에 이유
    reason: str = ""


def next_publishes(
    repo: Any, cfg: Any, now: datetime, adapters: dict | None = None
) -> list[NextPublish]:
    s = cfg.settings
    tz = s.timezone
    paused = set(repo.get_setting(PAUSED_KEY, []))
    out: list[NextPublish] = []
    waiting = repo.publications_in_status("queued") + repo.publications_in_status("failed")
    for platform in s.enabled_platforms:
        mine = [p for p in waiting if p.platform == platform]
        if not mine:
            continue
        if not cfg.persona.enabled:
            out.append(NextPublish(platform, None, "페르소나 꺼짐"))
            continue
        if platform in paused:
            out.append(NextPublish(platform, None, "일시 중지됨"))
            continue
        # 올릴 수 있게 되는 가장 이른 시각: 대기 중이면 지금, 실패 재시도면 그 시각
        ready = min((p.next_attempt_at or now) if p.status == "failed" else now for p in mine)
        at = max(now, ready)
        blocked = getattr(adapters.get(platform), "blocked_until", None) if adapters else None
        if blocked and blocked > at:
            at = blocked
        last = repo.last_published_at(platform)
        gap = timedelta(hours=s.min_hours_between_posts)
        if last and last + gap > at:
            at = last + gap
        start, end = local_day_bounds(now, tz)
        quota = s.quotas.get(platform, 0)
        if quota <= 0:
            out.append(NextPublish(platform, None, "하루 게시 수 0"))
            continue
        if repo.count_published_between(platform, start, end) >= quota and at < end:
            at = end  # 내일 0시부터
        windows = s.posting_hours if s.posting_hours is not None else cfg.persona.active_hours
        opened = next_window_start(at, windows, tz)
        if opened is None:
            out.append(NextPublish(platform, None, "게시 시간 없음"))
            continue
        out.append(NextPublish(platform, local(opened, tz)))
    return out
