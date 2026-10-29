"""색인할 문서 출처. 첫 구현은 저장소의 docs 폴더(R1)."""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

from nsm.rag.ports import Document


def default_docs_root() -> Path:
    """저장소의 docs 폴더(이 패키지 기준 ../../docs)."""
    return Path(__file__).resolve().parents[2] / "docs"


def excluded(rel: str, patterns: list[str]) -> bool:
    """제외 목록: "presentation/"처럼 /로 끝나면 그 폴더 아래 전부, 아니면 경로 패턴(fnmatch)."""
    for p in patterns:
        if p.endswith("/"):
            if rel.startswith(p) or f"/{p}" in f"/{rel}":
                return True
        elif fnmatch(rel, p):
            return True
    return False


class DocsFolderSource:
    """docs 아래 포함 목록에 맞는 .md만. 제외 목록이 이긴다(빈 양식, 발표·학습 자료, 샘플)."""

    kind = "docs"

    def __init__(self, root: Path, include: list[str], exclude: list[str]) -> None:
        self.root = root
        self.include = include
        self.exclude = exclude

    def _wanted(self, rel: str) -> bool:
        if excluded(rel, self.exclude):
            return False
        return any(rel.startswith(p) if p.endswith("/") else fnmatch(rel, p) for p in self.include)

    def documents(self) -> list[Document]:
        out = []
        for f in sorted(self.root.rglob("*.md")):
            rel = f.relative_to(self.root).as_posix()
            if self._wanted(rel):
                out.append(Document(rel, f.read_text(encoding="utf-8")))
        return out
