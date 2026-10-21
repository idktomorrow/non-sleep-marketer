"""캐릭터 프로필(S15 제안, ADR 0058): 작업 공간(프리셋)마다 캐릭터 이름·외형·성격·참조 이미지를 저장하고 재사용한다.

폴더: <NSM_HOME>/presets/<slug>/characters/<id>/profile.yaml + ref-*.png. 상품 사진(uploads·picks)과 섞지 않는다.
프로필을 고치면 revision이 오른다. 원고에는 **제작 당시 참조 복사본**과 revision이 남아(Cast) 과거 원고가 바뀌지 않는다.
"""

from __future__ import annotations

import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from nsm.domain.clock import new_id

MAX_REFS = 6
_ID = re.compile(r"^[0-9A-Za-z]{6,40}$")


class CharacterError(ValueError):
    pass


@dataclass
class CharacterProfile:
    id: str
    name: str
    appearance: str = ""
    personality: str = ""
    references: list[str] = field(default_factory=list)
    ai_made: bool = False
    revision: int = 1

    def to_yaml(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "appearance": self.appearance,
            "personality": self.personality,
            "references": [Path(r).name for r in self.references],
            "ai_made": self.ai_made,
            "revision": self.revision,
        }


class CharacterStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _dir(self, cid: str) -> Path:
        if not _ID.match(cid):
            raise CharacterError("캐릭터 id 형식이 아닙니다")
        return self.root / cid

    def list(self) -> list[CharacterProfile]:
        if not self.root.is_dir():
            return []
        out = []
        for d in sorted(self.root.iterdir()):
            if (d / "profile.yaml").is_file():
                try:
                    out.append(self.get(d.name))
                except CharacterError:
                    continue
        return out

    def get(self, cid: str) -> CharacterProfile:
        d = self._dir(cid)
        f = d / "profile.yaml"
        if not f.is_file():
            raise CharacterError("캐릭터가 없습니다")
        raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        refs = [str(d / name) for name in raw.get("references") or [] if (d / name).is_file()]
        return CharacterProfile(
            id=cid,
            name=str(raw.get("name", "")),
            appearance=str(raw.get("appearance", "")),
            personality=str(raw.get("personality", "")),
            references=refs,
            ai_made=bool(raw.get("ai_made", False)),
            revision=int(raw.get("revision", 1)),
        )

    def _write(self, p: CharacterProfile) -> None:
        d = self._dir(p.id)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "profile.yaml.tmp"
        tmp.write_text(
            yaml.safe_dump(p.to_yaml(), allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        tmp.replace(d / "profile.yaml")

    def create(self, name: str, appearance: str = "", personality: str = "") -> CharacterProfile:
        name = unicodedata.normalize("NFC", name.strip())
        if not name or len(name) > 30:
            raise CharacterError("캐릭터 이름은 1~30자")
        p = CharacterProfile(
            id=new_id(),
            name=name,
            appearance=appearance.strip()[:500],
            personality=personality.strip()[:300],
        )
        self._write(p)
        return p

    def update(
        self,
        cid: str,
        *,
        name: str | None = None,
        appearance: str | None = None,
        personality: str | None = None,
    ) -> CharacterProfile:
        p = self.get(cid)
        if name is not None:
            if not name.strip() or len(name.strip()) > 30:
                raise CharacterError("캐릭터 이름은 1~30자")
            p.name = name.strip()
        if appearance is not None:
            p.appearance = appearance.strip()[:500]
        if personality is not None:
            p.personality = personality.strip()[:300]
        p.revision += 1
        self._write(p)
        return p

    def add_reference(
        self, cid: str, data: bytes, ext: str, ai_made: bool = False
    ) -> CharacterProfile:
        p = self.get(cid)
        if len(p.references) >= MAX_REFS:
            raise CharacterError(f"참조 이미지는 {MAX_REFS}장까지입니다")
        d = self._dir(cid)
        name = f"ref-{new_id()[-10:]}{ext}"
        (d / name).write_bytes(data)
        p.references.append(str(d / name))
        p.ai_made = p.ai_made or ai_made
        p.revision += 1
        self._write(p)
        return p

    def remove_reference(self, cid: str, filename: str) -> CharacterProfile:
        p = self.get(cid)
        d = self._dir(cid)
        target = (d / Path(filename).name).resolve()
        if target.parent != d.resolve():
            raise CharacterError("잘못된 파일")
        p.references = [r for r in p.references if Path(r).resolve() != target]
        if target.is_file():
            target.unlink()
        p.revision += 1
        self._write(p)
        return p

    def delete(self, cid: str) -> None:
        d = self._dir(cid)
        if d.is_dir():
            shutil.rmtree(d)
