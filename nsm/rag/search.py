"""문서 검색(R2, ADR 0057). 질문을 같은 임베딩 모델로 벡터화해 `doc_chunks`에서 코사인 유사도 상위 k개를 찾는다.

- 벡터가 없는 조각, 다른 모델·차원으로 만든 조각은 빼고 비교한다(섞이면 점수가 뜻이 없다).
- 기준값(`rag.min_score`)보다 낮은 조각은 버린다. 남는 것이 없으면 "근거 없음"이다. R3에서 Claude가 "모른다"고 답하게 하는
  근거이므로, 애매하면 빈손으로 돌아오는 쪽을 택한다.
- 조각은 천여 개라 DuckDB `list_cosine_similarity`로 전수 비교한다(FLOAT[], ADR 0056).
- 질문 임베딩 비용은 임베더가 `classifier`·ref "embedding-query"로 적는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from nsm.rag.indexer import vector_literal
from nsm.rag.ports import Embedder
from nsm.store.db import Database

NO_GROUND = "근거 없음"


@dataclass
class Hit:
    path: str
    heading_path: str
    text: str
    score: float


@dataclass
class SearchResult:
    question: str
    hits: list[Hit] = field(default_factory=list)  # 기준값을 넘은 것만, 점수 높은 순
    top_score: float | None = None  # 기준값과 상관없는 1등 점수(측정·디버깅용)
    candidates: list[Hit] = field(default_factory=list)  # 기준값을 보기 전 상위 k(측정용)

    @property
    def grounded(self) -> bool:
        return bool(self.hits)

    @property
    def reason(self) -> str:
        return "" if self.hits else NO_GROUND


def search(
    db: Database,
    embedder: Embedder,
    question: str,
    *,
    k: int = 5,
    min_score: float,
    source_kind: str | None = None,
) -> SearchResult:
    question = question.strip()
    if not question:
        return SearchResult(question)
    qv = embedder.embed_query(question)
    where = "embedding IS NOT NULL AND embed_model = ? AND len(embedding) = ?"
    params: list = [vector_literal(qv), embedder.model, len(qv)]
    if source_kind:
        where += " AND source_kind = ?"
        params.append(source_kind)
    rows = db.execute(
        "SELECT path, heading_path, text, list_cosine_similarity(embedding, ?::FLOAT[]) AS score "
        f"FROM doc_chunks WHERE {where} ORDER BY score DESC NULLS LAST, id LIMIT ?",
        [*params, max(1, k)],
    )
    cands = [Hit(p, h, t, float(s)) for p, h, t, s in rows if s is not None]
    return SearchResult(
        question=question,
        hits=[c for c in cands if c.score >= min_score],
        top_score=cands[0].score if cands else None,
        candidates=cands,
    )
