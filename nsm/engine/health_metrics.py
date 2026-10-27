"""health 잡(로그인 상태 점검)과 metrics 잡(지표 스냅샷)."""

from __future__ import annotations

import logging
from datetime import timedelta

from nsm.domain.errors import policy_for
from nsm.engine.context import JobContext, JobResult
from nsm.engine.publish import PAUSED_KEY
from nsm.engine.rate_limit import blocked_until, resting, retry_at
from nsm.engine.watchdog import check_stalls

log = logging.getLogger(__name__)

METRICS_LOOKBACK_DAYS = 7


def run_health(ctx: JobContext) -> JobResult:
    """로그인이 풀린 플랫폼은 게시·답글을 멈추고 알린다. 다시 로그인되면 풀어 준다.

    요청 제한(HTTP 429)은 로그인 문제가 아니다: 멈추지 않고, 쉬는 동안은 확인도 하지 않는다(ADR 0038).
    """
    result = JobResult("health")
    paused = set(ctx.repo.get_setting(PAUSED_KEY, []))
    before = set(paused)
    limited: list[str] = []
    now = ctx.clock.now()
    for platform, adapter in ctx.adapters.items():
        if until := blocked_until(adapter, now):
            limited.append(resting(ctx, platform, until))
            continue
        try:
            h = adapter.health()
            ok, detail = h.logged_in, h.detail
        except Exception as e:  # noqa: BLE001
            pol = policy_for(e)  # 오류 종류 표(ADR 0054)
            if pol.retry == "until" and (until := retry_at(e)):
                limited.append(resting(ctx, platform, until))
                continue
            if not pol.pause_platform:
                result.errors.append(f"{platform} 상태 확인 실패({pol.label}): {e}")
                continue
            ok, detail = False, f"상태 확인 실패({pol.label}): {e}"
        if not ok and platform not in paused:
            paused.add(platform)
            ctx.notifier.notify(
                f"{platform} 로그인 필요",
                f"{platform} 자동 게시·답글을 멈췄습니다. 브라우저에서 다시 로그인하세요. ({detail})",
                "error",
            )
        elif ok and platform in paused:
            paused.discard(platform)
            ctx.notifier.notify(
                f"{platform} 다시 연결됨", f"{platform} 자동 활동을 다시 시작합니다", "info"
            )
    if paused != before:
        ctx.repo.set_setting(PAUSED_KEY, sorted(paused))
    result.counts.update(paused=sorted(paused))
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
