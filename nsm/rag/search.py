"""문서 검색(R2, ADR 0057). 질문을 같은 임베딩 모델로 벡터화해 `doc_chunks`에서 코사인 유사도 상위 k개를 찾는다.

- 벡터가 없는 조각, 다른 모델·차원으로 만든 조각은 빼고 비교한다(섞이면 점수가 뜻이 없다).
- 기준값(`rag.min_score`)보다 낮은 조각은 버린다. 남는 것이 없으면 "근거 없음"이다. R3에서 Claude가 "모른다"고 답하게 하는
  근거이므로, 애매하면 빈손으로 돌아오는 쪽을 택한다.
- 조각은 천여 개라 DuckDB `list_cosine_similarity`로 전수 비교한다(FLOAT[], ADR 0056).
- 질문 임베딩 비용은 임베더가 `classifier`·ref "embedding-query"로 적는다.
- 문서 단위 묶기(R4d, ADR 0067): 같은 원고(`posts/<content_id>/…`)·같은 수집 기사·같은 문서 파일은 상위 k 안에
  최대 `per_doc`개까지만 둔다. SQL은 후보를 넉넉히(k의 4배, 최소 20개) 가져오고 파이썬에서 점수 순으로 훑어 채운다.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field

from nsm.rag.indexer import vector_literal
from nsm.rag.ports import Embedder
from nsm.store.db import Database

NO_GROUND = "근거 없음"
POOL_FACTOR = 4  # 묶을 때 SQL로 가져오는 후보: k의 이 배수
POOL_MIN = 20  # 그리고 최소 이만큼


@dataclass
class Hit:
    path: str
    heading_path: str
    text: str
    score: float
    source_kind: str = "docs"  # R4: "docs"·"collected"·"posts"


@dataclass
class SearchResult:
    question: str
    hits: list[Hit] = field(default_factory=list)  # 묶은 뒤, 기준값을 넘은 것만, 점수 높은 순
    top_score: float | None = None  # 묶기·기준값과 상관없는 1등 점수(측정·디버깅용)
    candidates: list[Hit] = field(default_factory=list)  # 묶은 뒤, 기준값을 보기 전 상위 k(측정용)

    @property
    def grounded(self) -> bool:
        return bool(self.hits)

    @property
    def reason(self) -> str:
        return "" if self.hits else NO_GROUND


def post_content_id(path: str) -> str | None:
    """우리 글 경로 `posts/<content_id>/<platform>`·`posts/<content_id>/cards`(R4c)의 원고 id."""
    parts = path.split("/")
    return parts[1] if len(parts) >= 3 and parts[0] == "posts" and parts[1] else None


def group_key(path: str, source_kind: str) -> str:
    """묶음 키(R4d): 우리 글은 원고 하나(`posts/<content_id>`, 플랫폼 문서·카드 문서가 한 묶음),
    수집 자료(`source_items/<id>`)와 문서 파일은 경로 그대로(파일 하나가 한 묶음)."""
    if source_kind == "posts":
        cid = post_content_id(path)
        if cid:
            return f"posts/{cid}"
    return path


def group_hits(cands: list[Hit], k: int, per_doc: int) -> list[Hit]:
    """점수 순 후보에서 묶음마다 최대 per_doc개까지 받아 k개를 채운다. 모자라면 있는 만큼. 순서는 그대로."""
    taken: list[Hit] = []
    seen: dict[str, int] = {}
    for h in cands:
        g = group_key(h.path, h.source_kind)
        if seen.get(g, 0) >= per_doc:
            continue
        seen[g] = seen.get(g, 0) + 1
        taken.append(h)
        if len(taken) >= k:
            break
    return taken


def search(
    db: Database,
    embedder: Embedder,
    question: str,
    *,
    k: int = 5,
    min_score: float,
    source_kind: str | None = None,
    exclude_ids: Collection[str] | None = None,
    per_doc: int | None = None,
) -> SearchResult:
    """exclude_ids: 비교에서 뺄 조각 id(검색 측정의 유출 조각, R3d). 실제 질문은 쓰지 않는다.

    per_doc: 묶음(`group_key`)마다 상위 k에 둘 최대 조각 수(R4d, `rag.per_doc`). None이면 묶지 않는다(조각 단위 상위 k).
    """
    question = question.strip()
    if not question:
        return SearchResult(question)
    k = max(1, k)
    qv = embedder.embed_query(question)
    where = "embedding IS NOT NULL AND embed_model = ? AND len(embedding) = ?"
    params: list = [vector_literal(qv), embedder.model, len(qv)]
    if source_kind:
        where += " AND source_kind = ?"
        params.append(source_kind)
    if exclude_ids:
        where += " AND NOT list_contains(?::VARCHAR[], id)"
        params.append(sorted(exclude_ids))
    limit = k if per_doc is None else max(k * POOL_FACTOR, POOL_MIN)
    rows = db.execute(
        "SELECT path, heading_path, text, list_cosine_similarity(embedding, ?::FLOAT[]) AS score, source_kind "
        f"FROM doc_chunks WHERE {where} ORDER BY score DESC NULLS LAST, id LIMIT ?",
        [*params, limit],
    )
    pool = [Hit(p, h, t, float(s), kind) for p, h, t, s, kind in rows if s is not None]
    cands = pool[:k] if per_doc is None else group_hits(pool, k, max(1, per_doc))
    return SearchResult(
        question=question,
        hits=[c for c in cands if c.score >= min_score],
        top_score=pool[0].score if pool else None,
        candidates=cands,
    )
