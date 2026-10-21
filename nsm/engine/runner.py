"""잡 이름 → 실행 함수. 모든 실행은 run_logs에 기록된다."""

from __future__ import annotations

import logging
from collections.abc import Callable

from nsm.engine.collect import run_collect
from nsm.engine.context import JobContext, JobResult
from nsm.engine.generate import run_generate
from nsm.engine.health_metrics import run_health, run_metrics
from nsm.engine.keyword import run_keyword_draft
from nsm.engine.maintenance import run_maintenance
from nsm.engine.picks import run_picks, run_slots_retry
from nsm.engine.publish import run_publish
from nsm.engine.reply import run_reply
from nsm.engine.weekly_report import run_weekly_report

log = logging.getLogger(__name__)

JOBS: dict[str, Callable[[JobContext], JobResult]] = {
    "collect": run_collect,
    "generate": run_generate,
    "publish": run_publish,
    "reply": run_reply,
    "health": run_health,
    "metrics": run_metrics,
    "weekly_report": run_weekly_report,
    "maintenance": run_maintenance,
    "picks": run_picks,  # 오늘의 추천(S11 제안)
    "slots_retry": run_slots_retry,  # 재시도 시각이 된 추천 슬롯(S14)
}


# 사람이 "지금 실행"으로 부를 때 다르게 동작하는 잡(예: 원고 만들기는 하루 목표와 상관없이 1건 더)
MANUAL_JOBS: dict[str, Callable[[JobContext], JobResult]] = {
    "generate": lambda ctx: run_generate(ctx, manual=True),
    "publish": lambda ctx: run_publish(ctx, manual=True),  # 게시 간격 무시(하루 한도는 지킴)
    # 댓글 확인: 플랫폼별 최소 간격 무시(요청 제한으로 쉬는 중이면 그대로 쉼)
    "reply": lambda ctx: run_reply(ctx, manual=True),
    # 오늘의 추천: 모드가 꺼져 있어도 누르면 만든다(오늘 이미 만들었어도 추천 수만큼 더)
    "picks": lambda ctx: run_picks(ctx, manual=True),
}


# 사람이 값을 넘겨 부르는 잡(예약하지 않음). 키워드 원고는 키워드를 받는다(C15)
ARG_JOBS: dict[str, Callable[[JobContext, str], JobResult]] = {
    "keyword": run_keyword_draft,
}


def run_job(name: str, ctx: JobContext, manual: bool = False, arg: str | None = None) -> JobResult:
    if name in ARG_JOBS:
        if arg is None:
            raise ValueError(f"{name} 잡에는 값이 필요합니다(예: --keyword)")
        fn: Callable[[JobContext], JobResult] = lambda c: ARG_JOBS[name](c, arg)  # noqa: E731
    elif name not in JOBS:
        raise KeyError(f"알 수 없는 잡: {name} (가능: {', '.join([*JOBS, *ARG_JOBS])})")
    else:
        fn = MANUAL_JOBS.get(name, JOBS[name]) if manual else JOBS[name]
    ctx = ctx.snapshot()  # 이 실행은 지금 설정·어댑터 묶음을 끝까지 쓴다(S13)
    run_id = ctx.repo.start_run(name)
    try:
        result = fn(ctx)
    except Exception as e:
        log.exception("잡 %s 실패", name)
        ctx.repo.finish_run(run_id, False, f"{type(e).__name__}: {e}")
        ctx.notifier.notify(f"잡 실패: {name}", str(e)[:200], "error")
        return JobResult(name, ok=False, errors=[str(e)])
    ctx.repo.finish_run(run_id, result.ok, result.summary())
    return result
