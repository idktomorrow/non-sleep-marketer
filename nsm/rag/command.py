"""`nsm index` 명령의 몸통(R1). 개발·점검용이다. 색인 로직 자체는 `indexer.index_documents`에 있다."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nsm.rag.indexer import chunk_source, chunk_stats, index_documents, stored_stats
from nsm.rag.sources import DocsFolderSource, default_docs_root

LOCK_HELP = (
    "DB 파일이 잠겨 있습니다. 다른 프로그램(대개 실행 중인 nsm run)이 이 파일을 쓰고 있기 때문입니다.\n"
    "해결: (1) nsm run을 잠시 끄고 다시 실행하거나, (2) --db로 다른 파일(예: 임시 파일)을 지정하세요.\n"
    "R3부터는 nsm run 안에서 색인할 예정이라 끌 필요가 없어집니다."
)


class DbLockedError(Exception):
    pass


def is_lock_error(e: BaseException) -> bool:
    """다른 프로세스가 DuckDB 파일을 쥐고 있을 때. Windows는 "File is already open in …(PID …)",
    리눅스·맥은 "Could not set lock on file … Conflicting lock is held"(2026-10-30 Windows에서 재현해 확인)."""
    text = str(e).lower()
    return any(k in text for k in ("already open in", "could not set lock", "conflicting lock"))


def open_db(path: Path):
    from nsm.store.db import Database

    try:
        return Database(path)
    except Exception as e:  # duckdb.IOException 등
        if is_lock_error(e):
            raise DbLockedError(LOCK_HELP) from e
        raise


class IndexInputError(Exception):
    """사람이 고칠 수 있는 입력 문제(조각 크기·겹침, 문서 폴더). CLI가 한국어로만 보여 준다."""


def run_index(
    cfg: Any,
    db_path: Path,
    *,
    docs: Path | None = None,
    dry_run: bool = False,
    rebuild: bool = False,
    stats: bool = False,
    size: int | None = None,
    overlap: int | None = None,
    secrets: Any = None,
    embedder_factory: Callable[..., Any] | None = None,
    echo: Callable[[str], None] = print,
) -> int:
    r = cfg.settings.rag
    size = r.chunk_chars if size is None else size
    overlap = r.chunk_overlap if overlap is None else overlap
    if size < 100 or overlap < 0 or overlap >= size:
        raise IndexInputError(
            f"조각 크기는 100자 이상, 겹침은 0 이상이고 크기보다 작아야 합니다(지금 {size}자·{overlap}자)"
        )
    root = docs or default_docs_root()
    if not root.is_dir():
        raise IndexInputError(f"문서 폴더가 없습니다: {root} (--docs로 지정하세요)")
    source = DocsFolderSource(root, r.include, r.exclude)
    if dry_run:  # API도 DB도 쓰지 않는다
        s = chunk_stats(chunk_source(source, size, overlap))
        echo(f"[dry-run] 조각 크기 {size}자, 겹침 {overlap}자")
        echo(
            f"파일 {s.files}개 · 조각 {s.chunks}개 · 평균 {s.avg_chars:.0f}자 · 최대 {s.max_chars}자 · "
            f"제목 단위 {s.sections}개 중 쪼개진 것 {s.split_sections}개({s.split_ratio:.0%})"
        )
        return 0
    db = open_db(db_path)
    try:  # 끝나면 DB 연결과 HTTP 연결을 닫는다(R3에서 nsm run 안에서 부를 때 새지 않게)
        if stats:
            _print_stats(db, db_path, echo)
            return 0
        return _index(
            cfg, db, db_path, source, size, overlap, rebuild, secrets, embedder_factory, echo
        )
    finally:
        db.close()


def _print_stats(db: Any, db_path: Path, echo: Callable[[str], None]) -> None:
    st = stored_stats(db)
    echo(f"DB: {db_path}")
    echo(
        f"조각 {st['chunks']}개 · 파일 {st['files']}개 · 벡터 없는 조각 {st['without_vector']}개 · "
        f"글자 {st['chars']:,}자"
    )
    for m, n in st["models"].items():
        echo(f"  임베딩 모델 {m}: {n}개")


def _index(
    cfg, db, db_path, source, size, overlap, rebuild, secrets, embedder_factory, echo
) -> int:
    from nsm.domain.clock import SystemClock
    from nsm.rag.budget import add_used, close_embedder, plan_embedding
    from nsm.rag.indexer import EmptySourceError
    from nsm.store.repo import Repository

    r = cfg.settings.rag
    if secrets is None:
        from nsm.config.secrets import KeyringSecretStore

        secrets = KeyringSecretStore()
    if embedder_factory is None:
        from nsm.rag.gemini import make_embedder

        embedder_factory = make_embedder
    repo = Repository(db, SystemClock())
    made = embedder_factory(cfg, secrets, repo)  # 비용은 costs 표에(일일 상한에 포함)
    now = datetime.now(UTC)
    # 키·비용 상한·오늘 남은 무료 한도(색인 잡과 같은 판단, nsm/rag/budget.py)
    plan = plan_embedding(cfg, repo, made, now, r.embed_max_per_run)
    try:
        t0 = time.monotonic()
        try:
            rep = index_documents(
                db,
                source,
                plan.embedder,
                size=size,
                overlap=overlap,
                now=now,
                rebuild=rebuild,
                max_embed=plan.max_embed,
            )
        except EmptySourceError as e:
            raise IndexInputError(str(e)) from e
        took = time.monotonic() - t0
        add_used(repo, now, rep.embedded)
    finally:
        close_embedder(made)  # 상한으로 안 써도 만든 HTTP 연결은 닫는다
    echo(f"DB: {db_path}")
    echo(
        f"파일 {rep.files}개 · 조각 {rep.chunks}개 · 벡터 그대로 {rep.reused}개 · 새로 임베딩 {rep.embedded}개 · "
        f"벡터 없음 {rep.without_vector}개 · {took:.1f}초"
    )
    if rep.deleted_files:
        echo(f"지운 파일 {len(rep.deleted_files)}개: {', '.join(rep.deleted_files[:5])}")
    if plan.embedder is not None:
        tokens = getattr(plan.embedder, "tokens", 0)
        usd = tokens / 1_000_000 * r.embedding_usd_per_mtok
        echo(
            f"임베딩 {plan.embedder.model} {plan.embedder.dim}차원 · 입력 {tokens:,}토큰 · 비용 ${usd:.4f}"
        )
    if made is None:
        echo(
            "Gemini 키가 없어 조각만 저장하고 벡터는 만들지 않았습니다. "
            "설정 화면 API 키 칸(또는 nsm init)에서 Gemini 키를 넣고 다시 실행하면 벡터 없는 조각만 이어서 임베딩합니다."
        )
    elif plan.note:
        echo(f"{plan.note}. 내일 다시 실행하세요.")
    if rep.deferred:
        echo(
            f"오늘 상한({plan.max_embed}조각, 무료 구간 하루 한도 {r.embed_daily_limit}건 안)에 닿아 {rep.deferred}조각은 미뤘습니다. "
            "내일 다시 실행하면 이어서 합니다."
        )
    if rep.error:
        echo(
            f"임베딩이 도중에 멈췄습니다({rep.error_kind or '오류'}): {rep.error}\n"
            "저장된 조각은 그대로이고, 다시 실행하면 벡터 없는 조각부터 이어서 합니다."
        )
        return 1
    return 0


def run_ask(
    cfg: Any,
    db_path: Path,
    question: str,
    *,
    k: int | None = None,
    secrets: Any = None,
    embedder_factory: Callable[..., Any] | None = None,
    echo: Callable[[str], None] = print,
) -> int:
    """`nsm ask`: 상위 조각을 점수·경로와 함께 출력한다. 답은 만들지 않는다(R3).

    기준값을 넘는 조각이 없으면 "근거 없음"과 함께 기준값 아래 후보도 보여 준다(기준값을 맞출 때 보려고).
    """
    from nsm.domain.clock import SystemClock
    from nsm.rag.budget import SearchUnavailable, budgeted_search, close_embedder
    from nsm.rag.search import NO_GROUND
    from nsm.store.repo import Repository

    r = cfg.settings.rag
    db = open_db(db_path)
    try:
        if secrets is None:
            from nsm.config.secrets import KeyringSecretStore

            secrets = KeyringSecretStore()
        if embedder_factory is None:
            from nsm.rag.gemini import make_embedder

            embedder_factory = make_embedder
        repo = Repository(db, SystemClock())
        made = embedder_factory(cfg, secrets, repo)  # 질문 비용 기록
        try:
            if made is None:
                echo(
                    "Gemini 키가 없어 질문을 임베딩할 수 없습니다. 설정 화면 API 키 칸에서 Gemini 키를 넣으세요."
                )
                return 1
            try:  # 화면과 같은 길: 예산 판단·질문 임베딩·장부(실패한 시도도 셈)
                res = budgeted_search(
                    cfg, repo, made, question, k or r.search_k, r.min_score, datetime.now(UTC)
                )
            except SearchUnavailable as e:
                echo(f"질문을 검색하지 못했습니다: {e}")
                return 1
        finally:
            close_embedder(made)
        top = f"{res.top_score:.3f}" if res.top_score is not None else "없음"
        echo(f"질문: {res.question}")
        echo(f"기준값 {r.min_score:.2f} · 1등 점수 {top}")
        shown = res.hits
        if not res.grounded:
            echo(f"→ {NO_GROUND}(기준값을 넘는 조각이 없습니다). 기준값 아래 후보:")
            shown = res.candidates
        for i, h in enumerate(shown, 1):
            first = " ".join(h.text.split("\n", 1)[-1].split())[:160]
            echo(f"{i}. [{h.score:.3f}] {h.heading_path}")
            echo(f"   {first}")
        return 0
    finally:
        db.close()
