"""색인할 문서 출처. 첫 구현은 저장소의 docs 폴더(R1)."""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path

from nsm.rag.ports import Document


def default_docs_root() -> Path:
    """저장소의 docs 폴더. 소스에서 실행하면 패키지 기준 ../../docs, 아니면 지금 폴더의 docs.

    둘 다 없으면 없는 경로를 돌려주고, 부르는 쪽(`nsm index`)이 "문서 폴더가 없습니다"로 멈춘다.
    """
    here = Path(__file__).resolve().parents[2] / "docs"
    return here if here.is_dir() else Path.cwd() / "docs"


def matches(rel: str, pattern: str) -> bool:
    """포함·제외 목록의 한 항목. 둘 다 docs 기준 경로로 같은 규칙:
    "폴더/"는 그 폴더 아래 전부(맨 위 기준), 나머지는 경로 전체에 대한 패턴(`*`는 폴더 경계를 넘지 않음)."""
    if pattern.endswith("/"):
        return rel.startswith(pattern)
    if "/" not in pattern and "/" in rel:
        return False  # "*.md" 같은 패턴은 맨 위 파일에만(하위 폴더까지 통째로 빠지지 않게)
    return fnmatchcase(rel, pattern)


def excluded(rel: str, patterns: list[str]) -> bool:
    return any(matches(rel, p) for p in patterns)


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
        return any(matches(rel, p) for p in self.include)

    def documents(self) -> list[Document]:
        out = []
        for f in sorted(self.root.rglob("*.md")):
            rel = f.relative_to(self.root).as_posix()
            if self._wanted(rel):
                out.append(Document(rel, f.read_text(encoding="utf-8")))
        return out
