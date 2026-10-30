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
    from nsm.domain.policy import local_day_bounds, under_cost_cap
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
    embedder = embedder_factory(cfg, secrets, repo)  # 비용은 costs 표에(일일 상한에 포함)
    made = embedder  # 비용 상한으로 안 써도 만든 HTTP 연결은 닫는다
    now = datetime.now(UTC)
    over_cap = False
    if embedder is not None:
        start, end = local_day_bounds(now, cfg.settings.timezone)
        if not under_cost_cap(repo.cost_between(start, end), cfg.settings.daily_cost_cap_usd):
            over_cap, embedder = (
                True,
                None,
            )  # 다른 유료 경로처럼 오늘 비용 상한이면 임베딩하지 않는다
    try:
        t0 = time.monotonic()
        try:
            rep = index_documents(
                db,
                source,
                embedder,
                size=size,
                overlap=overlap,
                now=now,
                rebuild=rebuild,
                max_embed=r.embed_max_per_run,
            )
        except EmptySourceError as e:
            raise IndexInputError(str(e)) from e
        took = time.monotonic() - t0
    finally:
        close = getattr(getattr(made, "client", None), "close", None)
        if close:
            close()
    echo(f"DB: {db_path}")
    echo(
        f"파일 {rep.files}개 · 조각 {rep.chunks}개 · 벡터 그대로 {rep.reused}개 · 새로 임베딩 {rep.embedded}개 · "
        f"벡터 없음 {rep.without_vector}개 · {took:.1f}초"
    )
    if rep.deleted_files:
        echo(f"지운 파일 {len(rep.deleted_files)}개: {', '.join(rep.deleted_files[:5])}")
    if embedder is not None:
        tokens = getattr(embedder, "tokens", 0)
        usd = tokens / 1_000_000 * r.embedding_usd_per_mtok
        echo(f"임베딩 {embedder.model} {embedder.dim}차원 · 입력 {tokens:,}토큰 · 비용 ${usd:.4f}")
    if over_cap:
        echo(
            "오늘 비용 상한(daily_cost_cap_usd)에 닿아 임베딩하지 않고 조각만 저장했습니다. 내일 다시 실행하세요."
        )
    if rep.deferred:
        echo(
            f"실행당 상한({r.embed_max_per_run}조각, 무료 구간 하루 한도 1,000조각 안)에 닿아 {rep.deferred}조각은 미뤘습니다. "
            "내일 다시 실행하면 이어서 합니다."
        )
    if rep.no_key and not over_cap:
        echo(
            "Gemini 키가 없어 조각만 저장하고 벡터는 만들지 않았습니다. "
            "설정 화면 API 키 칸(또는 nsm init)에서 Gemini 키를 넣고 다시 실행하면 벡터 없는 조각만 이어서 임베딩합니다."
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
    """`nsm ask`: 상위 조각을 점수·경로와 함께 출력한다. 답은 만들지 않는다(R3)."""
    from nsm.domain.clock import SystemClock
    from nsm.rag.search import NO_GROUND, search
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
        embedder = embedder_factory(cfg, secrets, Repository(db, SystemClock()))  # 질문 비용 기록
        if embedder is None:
            echo(
                "Gemini 키가 없어 질문을 임베딩할 수 없습니다. 설정 화면 API 키 칸에서 Gemini 키를 넣으세요."
            )
            return 1
        try:
            res = search(db, embedder, question, k=k or r.search_k, min_score=r.min_score)
        finally:
            close = getattr(getattr(embedder, "client", None), "close", None)
            if close:
                close()
        top = f"{res.top_score:.3f}" if res.top_score is not None else "없음"
        echo(f"질문: {res.question}")
        echo(f"기준값 {r.min_score:.2f} · 1등 점수 {top}")
        if not res.grounded:
            echo(f"→ {NO_GROUND}(기준값을 넘는 조각이 없습니다)")
        for i, h in enumerate(res.hits, 1):
            first = " ".join(h.text.split("\n", 1)[-1].split())[:160]
            echo(f"{i}. [{h.score:.3f}] {h.heading_path}")
            echo(f"   {first}")
        return 0
    finally:
        db.close()
