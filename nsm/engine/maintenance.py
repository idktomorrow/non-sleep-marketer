"""maintenance 잡: 하루 한 번 데이터 백업과 오래된 실행 기록 정리 (배치 C12).

- 백업: `<NSM_HOME>/backups/data-YYYY-MM-DD.duckdb`. 같은 날 다시 돌면 덮어쓴다. 최근 `backup_keep`개만 남긴다.
- 실행 기록: `run_log_keep_days`일보다 오래된 것만 지운다. 원고·댓글·성과는 건드리지 않는다.
- 이미지(C48): 백업 뒤 폐기 원고의 카드·미리보기·올린 배경 폴더, 게시 원고의 카드와 겹치는 미리보기를 지운다(ADR 0073).
- 백업 복사(C49): `backup_copy_dir`이 있으면 본 백업 뒤 같은 이름으로 한 부 더(같은 보관 개수). 실패는 WARNING·알림만.
- 백업이 실패하면 잡 실패로 기록되고 알림이 간다(runner).

되살리기: `nsm run`을 끄고, 백업 파일을 `<NSM_HOME>/data.duckdb`로 복사한다(사용설명서 7절).
"""

from __future__ import annotations

import logging

from nsm.domain.policy import local
from nsm.engine.backup_copy import CopyError, clean_path, copy_backup, prune_copies
from nsm.engine.context import JobContext, JobResult

log = logging.getLogger(__name__)

PATTERN = "data-*.duckdb"


def backup_prefix(preset: str) -> str:
    """백업 파일 앞부분. 기본 프리셋은 예전 그대로 `data-날짜`, 다른 프리셋은 `data-<프리셋>-날짜`(C29).
    프리셋 백업이 기본 백업을 덮어쓰거나 서로의 보관 개수에 섞이지 않게."""
    from nsm.config.presets import DEFAULT_PRESET, same, slugify

    return "data" if same(preset, DEFAULT_PRESET) else f"data-{slugify(preset)}"


def _copy(ctx: JobContext, result: JobResult, path, prefix: str) -> None:
    """본 백업을 복사 폴더에 한 부 더(C49, ADR 0074). 실패해도 본 백업은 성공: WARNING과 알림 한 번."""
    s = ctx.config.settings
    folder = clean_path(s.backup_copy_dir)
    result.counts["copy_dir"] = folder
    try:
        copied = copy_backup(path, folder, ctx.backups_dir)
    except CopyError as e:
        result.counts.update(copy_ok=False, copy_error=str(e), copies_removed=0)
        log.warning("백업 복사 실패: %s (%s)", e, folder)
        # 같은 제목은 데스크톱 알림이 짧은 시간 안에 한 번만 띄운다(WindowsNotifier 규칙)
        ctx.notifier.notify("백업 복사 실패", f"{e}: {folder}. 본 백업은 됐습니다", "warn")
        return
    try:
        removed = prune_copies(copied.parent, prefix, s.backup_keep, copied)
    except OSError as e:  # 복사는 됐다: 옛 복사본을 못 지운 것만 남긴다
        log.warning("옛 백업 복사본 정리 실패: %s", e)
        removed = 0
    result.counts.update(copy_ok=True, copies_removed=removed)
    log.info("백업 복사: %s", copied)


def run_maintenance(ctx: JobContext) -> JobResult:
    result = JobResult("maintenance")
    s = ctx.config.settings
    # 정리를 먼저: 백업이 실패해도(파일이 열려 있음, 디스크 가득) 실행 기록 정리는 된다
    pruned = ctx.repo.prune_runs(s.run_log_keep_days)
    result.counts["failures_pruned"] = ctx.repo.prune_failures(s.run_log_keep_days)  # E2
    # 문서 질문 기록(R3c): 실행 기록과 같은 보관 일수. 측정 기록(doc_eval)은 전후 비교용이라 지우지 않는다
    from nsm.rag.answer import prune_qa

    result.counts["qa_pruned"] = prune_qa(ctx.repo.db, ctx.clock.now(), s.run_log_keep_days)
    if ctx.backups_dir is not None and ctx.repo.db.path != ":memory:":
        day = local(ctx.clock.now(), ctx.tz).date().isoformat()
        prefix = backup_prefix(s.preset)
        path = ctx.repo.db.backup_to(ctx.backups_dir / f"{prefix}-{day}.duckdb")
        # 방금 만든 것은 지우지 않는다(날짜가 뒤로 가도 오늘 백업이 남게). 같은 프리셋 것만 센다
        removed = prune_copies(ctx.backups_dir, prefix, s.backup_keep, path)
        result.counts.update(
            backup=path.name,
            backup_mb=round(path.stat().st_size / 1_000_000, 1),
            backups_removed=removed,
        )
        log.info("데이터 백업: %s", path)
        if s.backup_copy_dir.strip():
            _copy(ctx, result, path, prefix)
    result.counts["runs_pruned"] = pruned
    from nsm.adapters.evidence import prune_evidence

    try:  # 실패 증거 폴더 정리(E1). 실패해도 다른 정리는 계속
        result.counts["evidence_pruned"] = prune_evidence(ctx.clock.now(), s.evidence_keep_days)
    except Exception as e:  # noqa: BLE001
        log.warning("실패 증거 정리 실패: %s", e)
    # 자료 정리(C33)는 백업 뒤에: 오늘 백업에 정리 전 상태가 한 번 더 남게. 관련 없음 자료는 본문을 비우고
    # (제목·지문은 남겨 같은 글을 다시 모으지 않게), 더 오래되면 판정 기록을 지운다. 원고에 쓰인 자료는 영구
    result.counts["sources_blanked"] = ctx.repo.blank_stale_source_bodies(
        s.relevance_threshold, s.source_body_keep_days
    )
    result.counts["sources_pruned"] = ctx.repo.prune_stale_source_records(
        s.relevance_threshold, s.source_keep_days
    )
    # 이미지 정리(C48)도 백업 뒤에. 폐기 원고는 폴더 통째로, 게시 원고는 카드와 겹치는 미리보기만
    from nsm.engine.media_cleanup import sweep_media

    try:
        folders, size = sweep_media(
            ctx.media_dir, ctx.repo.content_ids("discarded"), ctx.repo.content_ids("published")
        )
        result.counts.update(media_removed=folders, media_mb=round(size / 1_000_000, 1))
        if folders:
            log.info("이미지 폴더 %d개 정리(%.1fMB)", folders, size / 1_000_000)
    except Exception as e:  # noqa: BLE001
        log.warning("이미지 정리 실패: %s", e)
    # 폐기된 원고에 남은 대기·재시도 게시 줄(C41 전에 생긴 것): 포기로 바꿔 게시 감시가 세지 않게
    abandoned = ctx.repo.abandon_discarded_leftovers()
    if abandoned:
        log.info("폐기된 원고의 게시 대기 줄 %d개를 포기로 정리", abandoned)
        result.counts["leftover_pubs"] = abandoned
    # "게시는 됐지만 주소 못 찾음"으로 성공 처리된 C43 이전 줄: 실제로는 안 올라간 경우였다(incidents/0015).
    # 한 번만 포기로 바꾼다. 원고 상태는 그대로(옛 소식을 다시 올리지 않음)
    unverified = ctx.repo.abandon_unverified_published_once()
    if unverified:
        log.info("확인 안 된 게시 줄 %d개를 포기로 정리(C43 이전)", unverified)
        result.counts["unverified_pubs"] = unverified
    return result
