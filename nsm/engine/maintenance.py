"""maintenance 잡: 하루 한 번 데이터 백업과 오래된 실행 기록 정리 (배치 C12).

- 백업: `<NSM_HOME>/backups/data-YYYY-MM-DD.duckdb`. 같은 날 다시 돌면 덮어쓴다. 최근 `backup_keep`개만 남긴다.
- 실행 기록: `run_log_keep_days`일보다 오래된 것만 지운다. 원고·댓글·성과는 건드리지 않는다.
- 백업이 실패하면 잡 실패로 기록되고 알림이 간다(runner).

되살리기: `nsm run`을 끄고, 백업 파일을 `<NSM_HOME>/data.duckdb`로 복사한다(사용설명서 7절).
"""

from __future__ import annotations

import logging

from nsm.domain.policy import local
from nsm.engine.context import JobContext, JobResult

log = logging.getLogger(__name__)

PATTERN = "data-*.duckdb"


def run_maintenance(ctx: JobContext) -> JobResult:
    result = JobResult("maintenance")
    s = ctx.config.settings
    # 정리를 먼저: 백업이 실패해도(파일이 열려 있음, 디스크 가득) 실행 기록 정리는 된다
    pruned = ctx.repo.prune_runs(s.run_log_keep_days)
    if ctx.backups_dir is not None and ctx.repo.db.path != ":memory:":
        day = local(ctx.clock.now(), ctx.tz).date().isoformat()
        path = ctx.repo.db.backup_to(ctx.backups_dir / f"data-{day}.duckdb")
        removed = 0
        # 방금 만든 것은 지우지 않는다(날짜가 뒤로 가도 오늘 백업이 남게)
        others = [p for p in ctx.backups_dir.glob(PATTERN) if p != path]
        for old in sorted(others, reverse=True)[s.backup_keep - 1 :]:
            old.unlink(missing_ok=True)
            removed += 1
        result.counts.update(
            backup=path.name,
            backup_mb=round(path.stat().st_size / 1_000_000, 1),
            backups_removed=removed,
        )
        log.info("데이터 백업: %s", path)
    result.counts["runs_pruned"] = pruned
    return result
