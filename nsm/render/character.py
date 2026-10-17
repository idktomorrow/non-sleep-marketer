"""일러스트형 카드의 페르소나 캐릭터(배치 C17, ADR 0046).

설정 `brand.character`가 무엇을 그릴지 정한다.
- none(기본): 캐릭터 없음. 넣고 싶으면 설정 화면에서 그림을 올린다(C20: 동봉하던 예시 캐릭터는 뺐다)
- custom: 사용자가 설정 화면에서 올린 그림(`brand.character_cover`, 마지막 장은 `brand.character_outro`, 없으면 표지 그림)
- drawn: 코드로 그린 도형 캐릭터(S8의 SVG)

캐릭터도 가상인물의 모습이라 배지가 있는 표지·마지막 장에만 둔다(ADR 0043). 카드 덱 JSON(동결 계약)은 바꾸지 않고,
렌더러가 스타일처럼 설정에서 읽는다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CharacterArt:
    """표지·마지막 장에 넣을 그림. `drawn`이면 코드 캐릭터(SVG)."""

    cover: Path | None = None
    outro: Path | None = None
    drawn: bool = False
    ai_made: bool = False  # AI로 만든 그림이면 그 장에 "AI 생성 이미지" 표기

    def for_kind(self, kind: str) -> Path | None:
        return self.cover if kind == "cover" else self.outro if kind == "outro" else None

    @property
    def fingerprint(self) -> str:
        """렌더 캐시 지문: 그림 파일 내용이나 표기가 바뀌면 카드를 다시 그린다."""
        h = hashlib.sha256(f"{self.drawn}|{self.ai_made}".encode())
        for p in (self.cover, self.outro):
            h.update(b"|")
            if p is not None and p.is_file():
                h.update(p.read_bytes())
        return h.hexdigest()[:12]


DRAWN = CharacterArt(drawn=True)


def _existing(path: str | Path | None) -> Path | None:
    if not path:
        return None
    p = Path(path)
    return p if p.is_file() else None


def character_art(brand: Any) -> CharacterArt:
    """설정의 브랜드(BrandSettings) → 그릴 캐릭터. 올린 그림 파일이 사라졌으면 코드 캐릭터로(빈자리 대신)."""
    choice = getattr(brand, "character", "none")
    ai_made = bool(getattr(brand, "character_ai_made", False))
    if choice == "drawn":
        return DRAWN
    if choice == "custom":
        cover = _existing(getattr(brand, "character_cover", None))
        outro = _existing(getattr(brand, "character_outro", None)) or cover
        if cover is None and outro is None:
            return DRAWN
        return CharacterArt(cover=cover or outro, outro=outro, ai_made=ai_made)
    return CharacterArt()  # none
