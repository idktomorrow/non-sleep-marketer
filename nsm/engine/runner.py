"""잡 이름 → 실행 함수. 모든 실행은 run_logs에 기록된다."""

from __future__ import annotations

import logging
from collections.abc import Callable

from nsm.engine.collect import run_collect
from nsm.engine.context import JobContext, JobResult
from nsm.engine.generate import run_generate

log = logging.getLogger(__name__)

JOBS: dict[str, Callable[[JobContext], JobResult]] = {
    "collect": run_collect,
    "generate": run_generate,
}


def run_job(name: str, ctx: JobContext) -> JobResult:
    if name not in JOBS:
        raise KeyError(f"알 수 없는 잡: {name} (가능: {', '.join(JOBS)})")
    run_id = ctx.repo.start_run(name)
    try:
        result = JOBS[name](ctx)
    except Exception as e:
        log.exception("잡 %s 실패", name)
        ctx.repo.finish_run(run_id, False, f"{type(e).__name__}: {e}")
        ctx.notifier.notify(f"잡 실패: {name}", str(e)[:200], "error")
        return JobResult(name, ok=False, errors=[str(e)])
    ctx.repo.finish_run(run_id, result.ok, result.summary())
    return result
