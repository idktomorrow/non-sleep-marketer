"""문서 색인 잡(R2, ADR 0057): R1의 `index_documents`를 nsm run 안에서 하루 한 번 돌린다.

- 증분이고, 실행당 상한(`rag.embed_max_per_run`)과 오늘 남은 무료 한도(`nsm/rag/budget.py`, 태평양 날짜 기준 장부) 중
  작은 만큼만 보낸다. "지금 실행"을 또 눌러도 같은 날 한도를 두 번 쓰지 않는다.
- Gemini 키가 없거나, 하루 비용 상한이거나, 한도(429)에 걸리면 **조용히** 끝낸다(알림 없음, 실패 아님). 조각은 저장하고
  벡터 없는 조각은 다음 날 이어서 한다. 429는 오류 종류 표의 `rate_limited`로 다룬다(ADR 0054).
- 끄는 중이면 묶음 사이(61초 기다림 포함)에서 멈추고 실행 기록엔 "중단"으로 남는다.
- nsm run을 켤 때는 돌리지 않는다(기동이 느려지지 않게).
"""

from __future__ import annotations

import time

from nsm.engine.context import JobContext, JobResult
from nsm.rag.budget import add_used, plan_embedding
from nsm.rag.indexer import EmptySourceError, index_documents
from nsm.rag.sources import DocsFolderSource, default_docs_root


def run_index_job(ctx: JobContext) -> JobResult:
    result = JobResult("index")
    r = ctx.config.settings.rag
    now = ctx.clock.now()
    plan = plan_embedding(ctx.config, ctx.repo, ctx.embedder, now, r.embed_max_per_run)
    if plan.embedder is not None and hasattr(plan.embedder, "reset"):
        plan.embedder.reset()
    quiet = plan.note
    source = DocsFolderSource(ctx.docs_root or default_docs_root(), r.include, r.exclude)
    t0 = time.monotonic()
    try:
        rep = index_documents(
            ctx.repo.db,
            source,
            plan.embedder,
            size=r.chunk_chars,
            overlap=r.chunk_overlap,
            now=now,
            max_embed=plan.max_embed,
            should_stop=ctx.shutting_down,
        )
    except EmptySourceError as e:
        result.ok = False
        result.errors.append(str(e))
        return result
    add_used(ctx.repo, now, rep.embedded)
    if rep.error_kind == "interrupted":
        result.interrupted = True
        result.errors.append(rep.error)
    elif rep.error_kind == "rate_limited":
        quiet = "Gemini 한도(429)로 멈춤, 내일 이어서"
    elif rep.error:
        result.errors.append(
            f"임베딩 중단: {rep.error}"
        )  # 한도가 아닌 실패(형식·연결)는 실행 기록에 남긴다
    if rep.deferred:
        quiet = quiet or f"오늘 상한({plan.max_embed}조각)으로 {rep.deferred}조각은 내일"
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
