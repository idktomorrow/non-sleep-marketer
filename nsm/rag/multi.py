"""여러 출처를 차례로 색인한다(R4, ADR 0065). 색인 잡과 `nsm index`가 같이 쓴다.

- 출처 순서는 설정 `rag.sources`(기본 docs → collected → posts). 하루 임베딩 상한은 앞 출처가 먼저 쓰고, 남은 만큼만
  다음 출처로 넘긴다. 상한을 다 쓰면 뒤 출처는 조각만 저장(벡터 NULL)하고 다음 날 이어서 한다.
- 한 출처가 실패해도(문서 폴더가 비어 `EmptySourceError` 등) 다른 출처는 계속한다. 수집 자료 0건인 새 설치에서
  docs 색인이 멈추면 안 되기 때문이다.
- 요청 제한(429)이나 임베딩 실패가 나면 뒤 출처는 보내지 않고 조각만 저장한다. 끄는 중이면 거기서 멈춘다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from nsm.rag.indexer import IndexReport, index_documents
from nsm.rag.ports import DocumentSource, Embedder
from nsm.rag.sources import DocsFolderSource
from nsm.rag.sources_db import CollectedSource, PostsSource
from nsm.store.db import Database

KINDS = ("docs", "collected", "posts")
KIND_BADGE = {"docs": "문서", "collected": "수집", "posts": "우리 글"}  # 출처 배지
KIND_NAME = {"docs": "프로젝트 문서", "collected": "수집 자료", "posts": "우리가 올린 글"}  # 고르기


def make_source(
    kind: str, cfg: Any, db: Database, now: datetime, docs_root: Path
) -> DocumentSource:
    r = cfg.settings.rag
    if kind == "docs":
        return DocsFolderSource(docs_root, r.include, r.exclude)
    if kind == "collected":
        return CollectedSource(
            db,
            min_relevance=r.collected_min_relevance,
            days=r.collected_days,
            now=now,
            tz=cfg.settings.timezone,
        )
    if kind == "posts":
        return PostsSource(db, tz=cfg.settings.timezone)
    raise ValueError(f"모르는 출처: {kind}")


@dataclass
class SourceRun:
    kind: str
    report: IndexReport | None  # None이면 이 출처는 실패(error)
    error: str = ""


def index_sources(
    db: Database,
    sources: list[DocumentSource],
    embedder: Embedder | None,
    *,
    size: int,
    overlap: int,
    now: datetime,
    max_embed: int,
    rebuild: bool = False,
    should_stop: Callable[[], bool] | None = None,
) -> list[SourceRun]:
    left = max_embed
    runs: list[SourceRun] = []
    for src in sources:
        try:
            rep = index_documents(
                db,
                src,
                embedder,
                size=size,
                overlap=overlap,
                now=now,
                rebuild=rebuild,
                max_embed=left,
                should_stop=should_stop,
            )
        except Exception as e:  # noqa: BLE001  EmptySourceError·DB 읽기 실패: 이 출처만 건너뛴다
            runs.append(SourceRun(src.kind, None, str(e)[:300]))
            continue
        runs.append(SourceRun(src.kind, rep))
        left = max(0, left - rep.embedded)
        if rep.error_kind == "interrupted":
            break
        if rep.error:
            left = 0  # 429·연결 실패: 뒤 출처는 조각만 저장
    return runs
