"""문서 색인 잡(R2, ADR 0057): R1의 `index_documents`를 nsm run 안에서 하루 한 번 돌린다.

- 증분이고 하루 전송 상한(`rag.embed_max_per_run`, 무료 구간 하루 1,000조각 안)을 지킨다.
- Gemini 키가 없거나, 하루 비용 상한이거나, 한도(429)에 걸리면 **조용히** 끝낸다(알림 없음, 실패 아님). 조각은 저장하고
  벡터 없는 조각은 다음 날 이어서 한다. 429는 오류 종류 표의 `rate_limited`로 다룬다(ADR 0054).
- nsm run을 켤 때는 돌리지 않는다(기동이 느려지지 않게). 처음 채우기는 묶음 사이 61초라 9분 남짓 작업 줄을 쓴다.
"""

from __future__ import annotations

import time

from nsm.domain.policy import local_day_bounds, under_cost_cap
from nsm.engine.context import JobContext, JobResult
from nsm.rag.indexer import EmptySourceError, index_documents
from nsm.rag.sources import DocsFolderSource, default_docs_root


def run_index_job(ctx: JobContext) -> JobResult:
    result = JobResult("index")
    r = ctx.config.settings.rag
    now = ctx.clock.now()
    embedder = ctx.embedder
    quiet = ""
    if embedder is None:
        quiet = "Gemini 키 없음(조각만 저장)"
    else:
        start, end = local_day_bounds(now, ctx.tz)
        if not under_cost_cap(
            ctx.repo.cost_between(start, end), ctx.config.settings.daily_cost_cap_usd
        ):
            embedder, quiet = None, "오늘 비용 상한(조각만 저장)"
    source = DocsFolderSource(ctx.docs_root or default_docs_root(), r.include, r.exclude)
    t0 = time.monotonic()
    try:
        rep = index_documents(
            ctx.repo.db,
            source,
            embedder,
            size=r.chunk_chars,
            overlap=r.chunk_overlap,
            now=now,
            max_embed=r.embed_max_per_run,
        )
    except EmptySourceError as e:
        result.ok = False
        result.errors.append(str(e))
        return result
    if rep.error_kind == "rate_limited":
        quiet = "Gemini 한도(429)로 멈춤, 내일 이어서"
    elif rep.error:
        result.errors.append(
            f"임베딩 중단: {rep.error}"
        )  # 한도가 아닌 실패(형식·연결)는 실행 기록에 남긴다
    if rep.deferred:
        quiet = quiet or f"하루 상한({r.embed_max_per_run}조각)으로 {rep.deferred}조각은 내일"
    result.counts.update(
        chunks=rep.chunks,
        without_vector=rep.without_vector,
        sent=rep.embedded,
        seconds=round(time.monotonic() - t0, 1),
    )
    if rep.deleted_files:
        result.counts["deleted_files"] = len(rep.deleted_files)
    if quiet:
        result.counts["note"] = quiet
    result.ok = not result.errors
    return result
