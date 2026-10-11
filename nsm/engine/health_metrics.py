"""health 잡(로그인 상태 점검)과 metrics 잡(지표 스냅샷)."""

from __future__ import annotations

import logging
from datetime import timedelta

from nsm.engine.context import JobContext, JobResult
from nsm.engine.publish import PAUSED_KEY

log = logging.getLogger(__name__)

METRICS_LOOKBACK_DAYS = 7


def run_health(ctx: JobContext) -> JobResult:
    """로그인이 풀린 플랫폼은 게시·답글을 멈추고 알린다. 다시 로그인되면 풀어 준다."""
    result = JobResult("health")
    paused = set(ctx.repo.get_setting(PAUSED_KEY, []))
    before = set(paused)
    for platform, adapter in ctx.adapters.items():
        try:
            h = adapter.health()
            ok, detail = h.logged_in, h.detail
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"상태 확인 실패: {e}"
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
    return result


def run_metrics(ctx: JobContext) -> JobResult:
    result = JobResult("metrics")
    now = ctx.clock.now()
    posts = accounts = 0
    for platform, adapter in ctx.adapters.items():
        caps = adapter.capabilities()
        if "post_metrics" in caps:
            for pub in ctx.repo.published_posts_since(
                platform, now - timedelta(days=METRICS_LOOKBACK_DAYS)
            ):
                if not pub.external_id:
                    continue
                try:
                    ctx.repo.insert_post_metrics(adapter.read_post_metrics(pub.external_id))
                    posts += 1
                except Exception as e:  # noqa: BLE001
                    result.errors.append(f"{platform} 게시물 지표 실패: {e}")
        if "account_metrics" in caps:
            try:
                ctx.repo.insert_account_metrics(adapter.read_account_metrics())
                accounts += 1
            except Exception as e:  # noqa: BLE001
                result.errors.append(f"{platform} 계정 지표 실패: {e}")
    result.counts.update(posts=posts, accounts=accounts)
    result.ok = not result.errors or posts + accounts > 0
    return result
