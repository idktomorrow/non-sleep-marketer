"""문서 색인(R1, ADR 0056). CLI(`nsm index`)와 분리된 함수라 R3에서 maintenance 잡·대시보드 버튼이 그대로 부른다.

증분 규칙:
- 파일마다 조각을 다시 만들고, 같은 출처·경로에서 **내용 해시가 같고 벡터가 있고 임베딩 모델·차원이 같은** 예전
  조각은 벡터를 그대로 쓴다(임베딩을 다시 부르지 않는다). 차원 설정을 바꾸면 다시 임베딩한다(차원이 섞이지 않게).
- 파일 내용이 그대로면 행을 다시 쓰지 않고, 벡터가 없는 조각만 그 자리에서 채운다.
- 출처에서 문서를 하나도 못 찾았는데 저장된 조각이 있으면 지우지 않고 멈춘다(경로 실수로 색인이 날아가지 않게).
- 나머지(새 조각, 벡터가 NULL이던 조각, 모델이 바뀐 조각)만 임베딩한다. 키가 없으면 조각만 저장하고 벡터는 NULL.
- 출처에서 사라진 파일의 조각은 지운다.
- 임베딩이 도중에 실패하면(요청 제한 등) 거기까지 저장하고 남은 조각은 NULL로 둔다. 다음 색인이 이어서 한다.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from nsm.rag.chunker import Chunk, chunk_markdown
from nsm.rag.ports import DocumentSource, Embedder
from nsm.store.db import Database, to_db

log = logging.getLogger(__name__)


def vector_literal(v: list[float] | None) -> str | None:
    """벡터를 "[0.1,0.2,…]" 글자로. DuckDB에 파이썬 리스트를 매개변수로 넘기면 768개짜리 하나에 약 1초가 걸려
    (2026-10-30 실측, 1,179조각이면 20분), 글자로 넘기고 SQL에서 `::FLOAT[]`로 바꾼다(행당 수 ms)."""
    if v is None:
        return None
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class IndexReport:
    files: int = 0
    chunks: int = 0
    reused: int = 0  # 벡터를 그대로 쓴 조각
    embedded: int = 0  # 이번에 임베딩한 조각
    without_vector: int = 0  # 끝났을 때 벡터가 없는 조각(키 없음·실패)
    deleted_files: list[str] = field(default_factory=list)
    no_key: bool = False
    deferred: int = 0  # 실행당 상한(embed_max_per_run) 때문에 다음으로 미룬 조각
    error: str = ""  # 임베딩이 도중에 멈춘 이유(있으면)
    error_kind: str = ""


@dataclass
class ChunkStats:
    files: int
    chunks: int
    avg_chars: float
    max_chars: int
    sections: int
    split_sections: int  # 조각이 둘 이상 나온 절

    @property
    def split_ratio(self) -> float:
        return self.split_sections / self.sections if self.sections else 0.0


def chunk_source(source: DocumentSource, size: int, overlap: int) -> dict[str, list[Chunk]]:
    return {d.path: chunk_markdown(d.path, d.text, size, overlap) for d in source.documents()}


def chunk_stats(by_file: dict[str, list[Chunk]]) -> ChunkStats:
    all_chunks = [c for cs in by_file.values() for c in cs]
    per_section: dict[tuple[str, int], int] = {}
    for f, cs in by_file.items():
        for c in cs:
            per_section[(f, c.section)] = per_section.get((f, c.section), 0) + 1
    lens = [len(c.text) for c in all_chunks]
    return ChunkStats(
        files=len(by_file),
        chunks=len(all_chunks),
        avg_chars=sum(lens) / len(lens) if lens else 0.0,
        max_chars=max(lens) if lens else 0,
        sections=len(per_section),
        split_sections=sum(1 for n in per_section.values() if n > 1),
    )


class EmptySourceError(Exception):
    """출처에서 문서를 하나도 못 찾았는데 저장된 조각은 있다: 경로가 틀렸을 가능성. 지우지 않고 멈춘다(리뷰)."""


def index_documents(
    db: Database,
    source: DocumentSource,
    embedder: Embedder | None,
    *,
    size: int,
    overlap: int,
    now: datetime,
    rebuild: bool = False,
    max_embed: int | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> IndexReport:
    kind = source.kind
    report = IndexReport(no_key=embedder is None)
    by_file = chunk_source(source, size, overlap)
    report.files = len(by_file)
    stored = {
        r[0]
        for r in db.execute("SELECT DISTINCT path FROM doc_chunks WHERE source_kind = ?", [kind])
    }
    if not by_file and stored:
        # 문서 폴더 경로를 잘못 주면 "전부 사라짐"으로 보여 색인을 통째로 지운다. 그러지 않는다
        raise EmptySourceError(
            f"문서를 하나도 찾지 못했습니다(저장된 파일 {len(stored)}개는 그대로 둡니다). 문서 폴더 경로를 확인하세요"
        )
    if rebuild:
        db.execute("DELETE FROM doc_chunks WHERE source_kind = ?", [kind])
        stored = set()
    for gone in sorted(stored - set(by_file)):  # 사라진 파일
        db.execute("DELETE FROM doc_chunks WHERE source_kind = ? AND path = ?", [kind, gone])
        report.deleted_files.append(gone)
    model = embedder.model if embedder else None
    dim = embedder.dim if embedder else None

    def usable(vec_len: int | None, vec_model: str | None) -> bool:
        """예전 벡터를 그대로 써도 되나: 있고, 모델·차원이 지금 임베더와 같다(키가 없으면 있는 것은 둔다)."""
        if vec_len is None:
            return False
        return embedder is None or (vec_model == model and vec_len == dim)

    pending: list[str] = []  # 임베딩할 조각 id
    texts: dict[str, str] = {}
    created = to_db(now)
    for path, chunks in by_file.items():
        report.chunks += len(chunks)
        rows = db.execute(
            "SELECT id, seq, content_hash, len(embedding), embed_model FROM doc_chunks "
            "WHERE source_kind = ? AND path = ? ORDER BY seq",
            [kind, path],
        )
        if [(r[1], r[2]) for r in rows] == [(c.seq, content_hash(c.text)) for c in chunks]:
            # 파일 내용이 그대로: 다시 쓰지 않고, 벡터가 없거나 모델·차원이 다른 조각만 임베딩 대기에
            for (cid, _seq, _h, vlen, vmodel), c in zip(rows, chunks, strict=True):
                if usable(vlen, vmodel):
                    report.reused += 1
                else:
                    pending.append(cid)
                    texts[cid] = c.text
            continue
        old = {
            h: (m, v)
            for h, m, v in db.execute(
                "SELECT content_hash, embed_model, embedding FROM doc_chunks WHERE source_kind = ? AND path = ?",
                [kind, path],
            )
        }
        with db.transaction() as conn:
            conn.execute("DELETE FROM doc_chunks WHERE source_kind = ? AND path = ?", [kind, path])
            for c in chunks:
                text = c.text
                h = content_hash(text)
                cid = f"{kind}:{path}#{c.seq}"
                old_model, old_vec = old.get(h, (None, None))
                keep = usable(len(old_vec) if old_vec is not None else None, old_model)
                conn.execute(
                    "INSERT INTO doc_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?::FLOAT[], ?)",
                    [
                        cid,
                        kind,
                        path,
                        c.heading_path,
                        c.seq,
                        text,
                        len(text),
                        h,
                        old_model if keep else None,
                        vector_literal(old_vec) if keep else None,
                        created,
                    ],
                )
                if keep:
                    report.reused += 1
                else:
                    pending.append(cid)
                    texts[cid] = text
    if embedder is not None and pending:
        if max_embed is not None and len(pending) > max_embed:
            report.deferred = len(pending) - max_embed  # 하루 한도 안에서 멈춤: 다음 실행이 이어서
            pending = pending[:max_embed]
        _embed_pending(db, embedder, pending, texts, report, should_stop or (lambda: False))
    report.without_vector = int(
        db.execute(
            "SELECT count(*) FROM doc_chunks WHERE source_kind = ? AND embedding IS NULL", [kind]
        )[0][0]
    )
    return report


def _embed_pending(
    db: Database,
    embedder: Embedder,
    ids: list[str],
    texts: dict[str, str],
    report: IndexReport,
    should_stop: Callable[[], bool],
) -> None:
    step = max(1, getattr(embedder, "batch_size", 100))
    if hasattr(embedder, "should_stop"):
        embedder.should_stop = should_stop  # 묶음 사이 기다림(61초)도 끄는 중이면 끊는다
    for i in range(0, len(ids), step):
        if should_stop():
            report.error, report.error_kind = "프로그램 종료 중이라 멈춤", "interrupted"
            return
        part = ids[i : i + step]
        try:
            vectors = embedder.embed([texts[c] for c in part])
        except Exception as e:  # noqa: BLE001  거기까지 저장하고 남은 조각은 다음 색인에서
            report.error = str(e)[:200]
            report.error_kind = (
                "interrupted"
                if getattr(e, "code", "") == "interrupted"
                else getattr(e, "kind", "") or ""
            )
            log.warning("임베딩 중단(%s): %s", report.error_kind or type(e).__name__, report.error)
            return
        with db.transaction() as conn:
            for cid, vec in zip(part, vectors, strict=True):
                conn.execute(
                    "UPDATE doc_chunks SET embedding = ?::FLOAT[], embed_model = ? WHERE id = ?",
                    [vector_literal(vec), embedder.model, cid],
                )
        report.embedded += len(part)


def stored_stats(db: Database, kind: str | None = None) -> dict[str, Any]:
    where, params = ("WHERE source_kind = ?", [kind]) if kind else ("", [])
    row = db.execute(
        f"SELECT count(*), count(DISTINCT path), count(*) FILTER (WHERE embedding IS NULL), "
        f"coalesce(sum(chars), 0) FROM doc_chunks {where}",
        params,
    )[0]
    models = db.execute(
        f"SELECT embed_model, count(*) FROM doc_chunks {where} GROUP BY 1 ORDER BY 2 DESC", params
    )
    return {
        "chunks": int(row[0]),
        "files": int(row[1]),
        "without_vector": int(row[2]),
        "chars": int(row[3]),
        "models": {str(m): int(n) for m, n in models},
    }
