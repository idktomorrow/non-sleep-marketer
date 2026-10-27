"""health 잡(로그인 상태 점검)과 metrics 잡(지표 스냅샷)."""

from __future__ import annotations

import logging
from datetime import timedelta

from nsm.domain.errors import KindedError, kind_of, policy_for
from nsm.engine.context import JobContext, JobResult
from nsm.engine.incidents import record_incident, with_diagnosis
from nsm.engine.pause import (
    HEALTH_STREAK_KEY,
    PAUSE_INFO_KEY,
    pause_info,
    pause_notice,
    short_reason,
)
from nsm.engine.publish import PAUSED_KEY
from nsm.engine.rate_limit import NAMES, blocked_until, rest_until, resting, retry_at
from nsm.engine.watchdog import check_stalls

log = logging.getLogger(__name__)

METRICS_LOOKBACK_DAYS = 7


def run_health(ctx: JobContext) -> JobResult:
    """로그인이 풀린 플랫폼은 게시·답글을 멈추고 알린다. 다시 로그인되면 풀어 준다.

    - 요청 제한(HTTP 429)은 로그인 문제가 아니다: 멈추지 않고, 쉬는 동안은 확인도 하지 않는다(ADR 0038).
    - 끄는 중(Ctrl+C)에 끊긴 점검은 멈춤·알림·기록 없이 "중단"으로만 남긴다(E4, incidents/0008).
    - 일시적 실패는 `health_pause_after_failures`번 연속일 때만 멈춘다. 로그인 풀림은 바로 멈춘다(ADR 0054 개정).
    - 멈출 때 이유(종류)와 시각을 저장해 화면·알림이 그대로 쓴다.
    """
    result = JobResult("health")
    need = ctx.config.settings.health_pause_after_failures
    paused = set(ctx.repo.get_setting(PAUSED_KEY, []))
    info = pause_info(ctx.repo)
    streak: dict[str, int] = dict(ctx.repo.get_setting(HEALTH_STREAK_KEY, {}) or {})
    before = (set(paused), {k: dict(v) for k, v in info.items()}, dict(streak))
    limited: list[str] = []
    retrying: list[str] = []
    now = ctx.clock.now()
    for platform, adapter in ctx.adapters.items():
        if until := blocked_until(adapter, now):
            limited.append(resting(ctx, platform, until))
            continue
        err: BaseException | None = None
        try:
            h = adapter.health()
            ok, detail = h.logged_in, h.detail
        except Exception as e:  # noqa: BLE001
            if ctx.shutting_down() or getattr(e, "code", None) == "interrupted":
                _interrupted(result, platform, e)
                continue
            if until := rest_until(e):  # 오류 종류 표(ADR 0054): 요청 제한은 쉬기만
                record_incident(ctx, platform, "health", "health", e, "쉬기")
                limited.append(resting(ctx, platform, until))
                continue
            err = e
            ok, detail = False, f"상태 확인 실패({policy_for(e).label}): {e}"
        if ctx.shutting_down():  # 끄는 중에 본 "로그인 안 됨"도 믿지 않는다
            _interrupted(result, platform, None)
            continue
        if ok:
            streak.pop(platform, None)
            if platform in paused:
                paused.discard(platform)
                info.pop(platform, None)
                ctx.notifier.notify(
                    f"{platform} 다시 연결됨", f"{platform} 자동 활동을 다시 시작합니다", "info"
                )
            continue
        if platform in paused:
            continue  # 이미 멈춤: 같은 실패를 30분마다 다시 쌓지 않는다
        kind = kind_of(err) if err is not None else "auth"
        if kind == "transient":
            n = streak.get(platform, 0) + 1
            streak[platform] = n
            if n < need:
                record_incident(
                    ctx,
                    platform,
                    "health",
                    "health",
                    err,  # type: ignore[arg-type]
                    f"상태 확인 실패(일시적), 다음 점검에서 다시 확인({n}/{need}회)",
                )
                retrying.append(f"{NAMES.get(platform, platform)} {n}/{need}회")
                continue
        diag = record_incident(
            ctx,
            platform,
            "health",
            "health",
            err or KindedError(detail, "auth"),
            "자동 게시·답글 멈춤",
        )
        paused.add(platform)
        info[platform] = {
            "reason": kind,
            "since": now.isoformat(),
            "detail": detail[:200],
            "failures": streak.get(platform, 1),
        }
        streak.pop(platform, None)
        title, body = pause_notice(platform, info[platform], detail)
        ctx.notifier.notify(title, with_diagnosis(body, diag), "error")
    if paused != before[0]:
        ctx.repo.set_setting(PAUSED_KEY, sorted(paused))
    info = {k: v for k, v in info.items() if k in paused}
    if info != before[1]:
        ctx.repo.set_setting(PAUSE_INFO_KEY, info)
    if streak != before[2]:
        ctx.repo.set_setting(HEALTH_STREAK_KEY, streak)
    result.counts.update(paused=sorted(paused))
    if paused:
        result.counts["paused_why"] = {p: short_reason(info.get(p)) for p in sorted(paused)}
    if retrying:
        result.counts["health_retry"] = retrying
    if limited:
        result.counts["rate_limited"] = limited
    try:
        stalls = check_stalls(ctx)  # 조용히 멈춘 수집·게시(C12)
    except Exception as e:  # noqa: BLE001  로그인 점검 결과를 가리지 않게
        log.warning("계속 0건 확인 실패: %s", e)
        result.errors.append(f"계속 0건 확인 실패: {e}")
        stalls = []
    if stalls:
        result.counts["stalls"] = stalls
    return result


def _interrupted(result: JobResult, platform: str, e: BaseException | None) -> None:
    """끄는 중 끊긴 점검: 멈춤·알림·실패 기록 없이 실행 기록에 "중단"만(C33과 같은 기준)."""
    result.ok = False
    result.interrupted = True
    result.errors.append(f"{platform} 상태 확인 중단(프로그램 종료 중){f': {e}' if e else ''}")


def run_metrics(ctx: JobContext) -> JobResult:
    result = JobResult("metrics")
    now = ctx.clock.now()
    tally = {"posts": 0, "accounts": 0}
    limited: list[str] = []
    for platform, adapter in ctx.adapters.items():
        if until := blocked_until(adapter, now):
            limited.append(resting(ctx, platform, until))
            continue
        try:
            _platform_metrics(ctx, platform, adapter, result, now, tally)
        except Exception as e:  # noqa: BLE001
            until = retry_at(e)
            record_incident(
                ctx,
                platform,
                "metrics",
                "metrics",
                e,
                "쉬기" if until else "지표 수집 중단(다음 실행에 다시)",
            )
            if until is None:
                raise
            limited.append(resting(ctx, platform, until))  # 남은 글도 막힌다: 이 플랫폼은 그만
    if limited:
        result.counts["rate_limited"] = limited
    posts, accounts = tally["posts"], tally["accounts"]
    result.counts.update(posts=posts, accounts=accounts)
    result.ok = not result.errors or posts + accounts > 0
    return result


def _platform_metrics(
    ctx: JobContext, platform: str, adapter, result: JobResult, now, tally: dict[str, int]
) -> None:
    """한 플랫폼의 지표를 tally에 센다. 요청 제한 예외만 밖으로 던지고 나머지는 오류로 적는다."""
    caps = adapter.capabilities()
    if "post_metrics" in caps:
        since = now - timedelta(days=METRICS_LOOKBACK_DAYS)
        for pub in ctx.repo.published_posts_since(platform, since):
            if not pub.external_id:
                continue
            try:
                ctx.repo.insert_post_metrics(adapter.read_post_metrics(pub.external_id))
                tally["posts"] += 1
            except Exception as e:  # noqa: BLE001
                if retry_at(e):
                    raise
                result.errors.append(f"{platform} 게시물 지표 실패: {e}")
    if "account_metrics" in caps:
        try:
            ctx.repo.insert_account_metrics(adapter.read_account_metrics())
            tally["accounts"] += 1
        except Exception as e:  # noqa: BLE001
            if retry_at(e):
                raise
            result.errors.append(f"{platform} 계정 지표 실패: {e}")
