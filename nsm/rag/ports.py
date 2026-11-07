"""RAG 전용 Protocol(ADR 0056). 동결된 `nsm/ports/`와 따로 둔다.

- `DocumentSource`: 색인할 문서 목록과 본문을 준다. 첫 구현은 docs 폴더(`DocsFolderSource`), R4에서 수집 자료·우리 글
  (`sources_db.py`)이 같은 모양으로 붙었다. 0건이 정상인 출처는 `allow_empty = True`를 둔다. `kind`가 `doc_chunks.source_kind`에 들어가 출처마다 따로 증분·삭제한다.
- `Embedder`: 문자열 목록 → 벡터 목록. 첫 구현은 Gemini(`GeminiEmbedder`), 테스트는 `tests.fakes.FakeEmbedder`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Document:
    path: (
        str  # 출처 안에서의 경로(예: "adr/0054-error-taxonomy-and-evidence.md"). 같은 출처에서 유일
    )
    text: str
    label: str | None = None  # 제목 경로 맨 앞에 보일 이름(R4: "수집 자료"). 없으면 path


class DocumentSource(Protocol):
    kind: str  # "docs", "collected", "posts"(R4)

    def documents(self) -> list[Document]: ...


class Embedder(Protocol):
    model: str  # 저장할 때 함께 적는다. 모델이 바뀌면 다시 임베딩한다
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """문서 조각 임베딩. 길이와 순서는 입력과 같다. 실패하면 예외(요청 제한은 kind="rate_limited")."""
        ...

    def embed_query(self, question: str) -> list[float]:
        """검색 질문 임베딩(R2). 같은 모델·차원이어야 문서 조각과 비교된다."""
        ...
