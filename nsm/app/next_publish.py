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
    # 게시 잡(due_publications)처럼 승인된 원고의 게시만 센다. 검토 대기·폐기 원고에도 queued 행은 있다
    approved = {c.id for c in repo.list_contents("approved", limit=1000)}
    waiting = [
        p
        for p in repo.publications_in_status("queued") + repo.publications_in_status("failed")
        if p.content_id in approved
    ]
    for platform in s.enabled_platforms:
        mine = [p for p in waiting if p.platform == platform]
        if not mine:
            continue
        if adapters is not None and platform not in adapters:
            out.append(NextPublish(platform, None, "연결 안 됨"))
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
        # 게시 잡(should_post)은 활동 시간 안이면서 게시 시간 안이어야 한다. 두 구간을 번갈아 맞춘다
        sets = [cfg.persona.active_hours]
        if s.posting_hours is not None:
            sets.append(s.posting_hours)
        opened: datetime | None = at
        converged = False
        for _ in range(3):
            for windows in sets:
                opened = next_window_start(opened, windows, tz) if opened is not None else None
            if opened is None or all(next_window_start(opened, w, tz) == opened for w in sets):
                converged = True
                break
        # 두 구간이 겹치지 않으면(활동 09-12, 게시 13-18) 영원히 안 맞는다. 엉뚱한 시각을 보여 주지 않는다
        if opened is None or not converged:
            out.append(NextPublish(platform, None, "게시 시간 없음"))
            continue
        out.append(NextPublish(platform, local(opened, tz)))
    return out
