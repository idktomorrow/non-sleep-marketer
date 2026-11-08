"""조용히 멈춘 것 알아보기 (배치 C12).

화면 구조가 바뀌면 수집·게시는 오류 없이 "0건"으로 계속 돈다(2026-10-13 네이버 뉴스, 2026-10-14 인스타그램 429).
실패로 잡히지 않으니 알림도 없었다. health 잡(30분마다)이 여기를 불러 확인한다.

- 자료 모으기: 최근 `stall_alert_days`일 동안 자료 모으기가 2번 넘게 성공했는데 관련 자료가 0개
  (실패한 실행은 세지 않는다: 그건 "잡 실패" 알림이 따로 간다)
- 게시: 그 플랫폼에 올릴 원고가 그 기간보다 오래 기다리는데 그동안 한 건도 못 올림
  (하루 한도 0, 로그인 멈춤, 요청 제한으로 쉬는 중은 이유가 이미 보이므로 빼고 본다)
  - "올릴 원고"는 승인된 원고의 대기·재시도 게시 줄만 센다(C41: 폐기·검토 대기 원고의 줄을 세어 오보했다)
  - 올릴 원고가 0개면 왜 없는지 따로 알린다(C41, incidents/0014): 원고 만들기가 그 기간 안 돌았음(마지막 실행 시각),
    검토 대기 원고가 쌓임, 만들기는 돌았는데 새 원고 없음. 플랫폼마다 같은 문장을 겹쳐 알리지 않게 이유별로 한 줄.
    새 설치(게시도 원고 만들기도 한 번도 없음)는 알리지 않는다

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
    idle: list[str] = []  # 게시 0건인데 올릴 원고도 0개인 플랫폼(이유는 아래에서 한 번에)
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
        elif ctx.repo.waiting_publications(platform, now + timedelta(days=1)):
            continue  # 최근에 승인된 원고가 기다린다: 게시 시간·하루 한도로 곧 올라간다(C41 리뷰)
        elif last is not None or ctx.repo.has_any_run("generate"):  # 새 설치는 아직 알리지 않는다
            idle.append(platform)
    if idle:
        found.append(_no_drafts(ctx, idle, days, since))
    return found


def _no_drafts(ctx: JobContext, platforms: list[str], days: int, since) -> tuple[str, str]:
    """게시 0건인데 올릴 원고가 없을 때: 왜 없는지 한 줄(플랫폼이 여럿이어도 한 번)."""
    names = "·".join(PLATFORM_NAMES.get(p, p) for p in platforms)
    from nsm.engine.catch_up import finished_since

    last = ctx.repo.last_run_at("generate")
    # 그 기간에 끝난 실행(성공·건너뜀·실패, 종료 중단 제외)이 없으면 "안 돎". 건너뛰거나 실패만 했으면 아래 "새 원고 없음"
    if ctx.repo.runs_since("generate", since) == 0 and not finished_since(
        ctx.repo, "generate", since
    ):
        when = local(last, ctx.tz).strftime("%m-%d %H:%M") if last else "기록 없음"
        return (
            "generate",
            f"원고 만들기 {days}일째 안 돎(마지막 실행 {when}), 그래서 {names} 게시 0건: "
            '그 시각에 PC가 꺼져 있었거나 절전이었을 수 있습니다. 대시보드 "지금 실행"의 원고 만들기를 누르세요',
        )
    pending = ctx.repo.count_contents_in_status("pending_review")
    if pending:
        return (
            "review",
            f"{names} {days}일째 게시 0건: 승인된 원고가 없고 검토 대기 원고 {pending}개가 있습니다. "
            "검토 화면에서 승인하세요",
        )
    return (
        "no_drafts",
        f"{names} {days}일째 게시 0건: 원고 만들기는 돌았지만 올릴 새 원고가 없습니다. "
        "실행 기록에서 원고 만들기가 건너뛰거나 실패한 이유(쓸 자료 없음·비용 상한 등)를 확인하세요",
    )


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
