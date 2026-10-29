"""RAG 전용 Protocol(ADR 0056). 동결된 `nsm/ports/`와 따로 둔다.

- `DocumentSource`: 색인할 문서 목록과 본문을 준다. 첫 구현은 docs 폴더(`DocsFolderSource`), 나중에 수집 자료(DuckDB)도
  같은 모양으로 붙인다. `kind`가 `doc_chunks.source_kind`에 들어가 출처마다 따로 증분·삭제한다.
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


class DocumentSource(Protocol):
    kind: str  # "docs", 나중에 "sources" 등

    def documents(self) -> list[Document]: ...


class Embedder(Protocol):
    model: str  # 저장할 때 함께 적는다. 모델이 바뀌면 다시 임베딩한다
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """문서 조각 임베딩. 길이와 순서는 입력과 같다. 실패하면 예외(요청 제한은 kind="rate_limited")."""
        ...
