"""조용히 멈춘 것 알아보기 (배치 C12).

화면 구조가 바뀌면 수집·게시는 오류 없이 "0건"으로 계속 돈다(2026-10-13 네이버 뉴스, 2026-10-14 인스타그램 429).
실패로 잡히지 않으니 알림도 없었다. health 잡(30분마다)이 여기를 불러 확인한다.

- 자료 모으기: 최근 `stall_alert_days`일 동안 자료 모으기가 2번 넘게 성공했는데 관련 자료가 0개
  (실패한 실행은 세지 않는다: 그건 "잡 실패" 알림이 따로 간다)
- 게시: 그 플랫폼에 올릴 원고가 그 기간보다 오래 기다리는데 그동안 한 건도 못 올림
  (하루 한도 0, 로그인 멈춤, 요청 제한으로 쉬는 중은 이유가 이미 보이므로 빼고 본다)

찾은 것은 "오늘 할 일"에 띄우고(`STALLS_KEY`), 같은 종류는 하루 한 번만 알린다.
"""

from __future__ import annotations

from datetime import timedelta

from nsm.domain.policy import local
from nsm.engine.context import JobContext
from nsm.engine.publish import PAUSED_KEY, PLATFORM_NAMES
from nsm.engine.rate_limit import blocked_until

STALLS_KEY = "stalls"  # 지금 걸린 것(오늘 할 일에 보일 문장)
NOTIFIED_KEY = "stalls_notified"  # {종류: 알린 날짜}. 문장에 바뀌는 숫자가 있어 종류로 센다

MIN_COLLECT_RUNS = 2  # 이만큼은 성공했어야 "0건"을 문제로 본다(PC가 꺼져 있었던 경우 제외)


def check_stalls(ctx: JobContext) -> list[str]:
    found = _find(ctx)
    ctx.repo.set_setting(STALLS_KEY, [text for _, text in found])
    _notify_once_a_day(ctx, found)
    return [text for _, text in found]


def _find(ctx: JobContext) -> list[tuple[str, str]]:
    """(종류, 문장) 목록."""
    s = ctx.config.settings
    if not ctx.config.persona.enabled:  # 일부러 꺼 둔 것
        return []
    days = s.stall_alert_days
    now = ctx.clock.now()
    since = now - timedelta(days=days)
    found: list[tuple[str, str]] = []
    if (
        ctx.repo.runs_since("collect", since) >= MIN_COLLECT_RUNS
        and ctx.repo.relevant_since(since, s.relevance_threshold) == 0
    ):
        found.append(
            (
                "collect",
                f"자료 모으기 {days}일째 관련 자료 0개: 수집하는 사이트 화면이 바뀌었거나 "
                "관심 주제가 너무 좁을 수 있습니다",
            )
        )
    paused = set(ctx.repo.get_setting(PAUSED_KEY, []) or [])
    for platform in s.enabled_platforms:
        adapter = ctx.adapters.get(platform)
        if (
            adapter is None
            or platform in paused  # 로그인 멈춤은 따로 알린다
            or s.quotas.get(platform, 0) <= 0  # 일부러 안 올림
            or blocked_until(adapter, now)  # 요청 제한으로 쉬는 중(따로 알린다)
        ):
            continue
        last = ctx.repo.last_published_at(platform)
        if last is not None and last >= since:
            continue
        waiting = ctx.repo.waiting_publications(platform, since)
        if waiting:
            name = PLATFORM_NAMES.get(platform, platform)
            found.append(
                (
                    f"publish:{platform}",
                    f"{name} {days}일째 게시 0건(올릴 원고 {waiting}개 대기): "
                    "실행 기록에서 게시하기가 안 올린 이유를 확인하세요",
                )
            )
    return found


def _notify_once_a_day(ctx: JobContext, found: list[tuple[str, str]]) -> None:
    today = local(ctx.clock.now(), ctx.tz).date().isoformat()
    seen = dict(ctx.repo.get_setting(NOTIFIED_KEY, {}) or {})
    fresh = [(kind, text) for kind, text in found if seen.get(kind) != today]
    for kind, _ in fresh:
        seen[kind] = today
    # 풀린 종류는 지운다: 같은 날 다시 걸리면 다시 알린다
    kept = {kind: seen[kind] for kind, _ in found}
    if kept != (ctx.repo.get_setting(NOTIFIED_KEY, {}) or {}):
        ctx.repo.set_setting(NOTIFIED_KEY, kept)
    if fresh:
        ctx.notifier.notify("계속 0건", " / ".join(text for _, text in fresh)[:300], "warn")
