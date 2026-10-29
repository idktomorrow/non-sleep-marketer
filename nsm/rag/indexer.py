"""문서 색인(R1, ADR 0056). CLI(`nsm index`)와 분리된 함수라 R3에서 maintenance 잡·대시보드 버튼이 그대로 부른다.

증분 규칙:
- 파일마다 조각을 다시 만들고, 같은 출처·경로에서 **내용 해시가 같고 벡터가 있고 임베딩 모델이 같은** 예전 조각은
  벡터를 그대로 쓴다(임베딩을 다시 부르지 않는다).
- 나머지(새 조각, 벡터가 NULL이던 조각, 모델이 바뀐 조각)만 임베딩한다. 키가 없으면 조각만 저장하고 벡터는 NULL.
- 출처에서 사라진 파일의 조각은 지운다.
- 임베딩이 도중에 실패하면(요청 제한 등) 거기까지 저장하고 남은 조각은 NULL로 둔다. 다음 색인이 이어서 한다.
"""

from __future__ import annotations

import hashlib
import logging
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
) -> IndexReport:
    kind = source.kind
    report = IndexReport(no_key=embedder is None)
    by_file = chunk_source(source, size, overlap)
    report.files = len(by_file)
    if rebuild:
        db.execute("DELETE FROM doc_chunks WHERE source_kind = ?", [kind])
    # 사라진 파일
    stored = {
        r[0]
        for r in db.execute("SELECT DISTINCT path FROM doc_chunks WHERE source_kind = ?", [kind])
    }
    for gone in sorted(stored - set(by_file)):
        db.execute("DELETE FROM doc_chunks WHERE source_kind = ? AND path = ?", [kind, gone])
        report.deleted_files.append(gone)
    model = embedder.model if embedder else None
    pending: list[str] = []  # 임베딩할 조각 id
    texts: dict[str, str] = {}
    created = to_db(now)
    for path, chunks in by_file.items():
        # 파일이 그대로면(순번·해시가 같고 모두 같은 모델 벡터가 있으면) 건드리지 않는다: 두 번째 실행을 빠르게
        same = db.execute(
            "SELECT seq, content_hash, embedding IS NOT NULL, embed_model FROM doc_chunks "
            "WHERE source_kind = ? AND path = ? ORDER BY seq",
            [kind, path],
        )
        if [(r[0], r[1]) for r in same] == [(c.seq, content_hash(c.text)) for c in chunks] and all(
            r[2] and (model is None or r[3] == model) for r in same
        ):
            report.reused += len(chunks)
            report.chunks += len(chunks)
            continue
        old: dict[str, tuple[str | None, list[float] | None]] = {
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
                keep = old_vec is not None and (model is None or old_model == model)
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
        report.chunks += len(chunks)
    if embedder is not None and pending:
        if max_embed is not None and len(pending) > max_embed:
            report.deferred = len(pending) - max_embed  # 하루 한도 안에서 멈춤: 다음 실행이 이어서
            pending = pending[:max_embed]
        _embed_pending(db, embedder, pending, texts, report)
    report.without_vector = int(
        db.execute(
            "SELECT count(*) FROM doc_chunks WHERE source_kind = ? AND embedding IS NULL", [kind]
        )[0][0]
    )
    return report


def _embed_pending(
    db: Database, embedder: Embedder, ids: list[str], texts: dict[str, str], report: IndexReport
) -> None:
    step = max(1, getattr(embedder, "batch_size", 100))
    for i in range(0, len(ids), step):
        part = ids[i : i + step]
        try:
            vectors = embedder.embed([texts[c] for c in part])
        except Exception as e:  # noqa: BLE001  거기까지 저장하고 남은 조각은 다음 색인에서
            report.error = str(e)[:200]
            report.error_kind = getattr(e, "kind", "") or ""
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
