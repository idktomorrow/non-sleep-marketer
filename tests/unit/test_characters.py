"""캐릭터 프로필(S15 제안): 만들기·참조 추가/삭제·판 번호·지우기·경로 조작 차단."""

from __future__ import annotations

from pathlib import Path

import pytest

from nsm.config.characters import MAX_REFS, CharacterError, CharacterStore


def test_profile_lifecycle(tmp_path: Path) -> None:
    s = CharacterStore(tmp_path / "chars")
    p = s.create("루나", "하얀 몸, 분홍 볼")
    assert p.revision == 1 and s.list()[0].name == "루나"
    p = s.add_reference(p.id, b"\x89PNG fake", ".png")
    assert p.revision == 2 and len(p.references) == 1 and Path(p.references[0]).is_file()
    p = s.update(p.id, appearance="하얀 몸, 노란 리본")
    assert p.revision == 3 and s.get(p.id).appearance == "하얀 몸, 노란 리본"
    name = Path(p.references[0]).name
    outside = tmp_path / "keep.txt"
    outside.write_text("x")
    p = s.remove_reference(p.id, "../../keep.txt")  # 이름만 쓰므로 다른 폴더 파일은 건드리지 않는다
    assert outside.is_file() and len(p.references) == 1
    p = s.remove_reference(p.id, name)
    assert p.references == []
    s.delete(p.id)
    assert s.list() == []


def test_limits_and_validation(tmp_path: Path) -> None:
    s = CharacterStore(tmp_path)
    with pytest.raises(CharacterError):
        s.create("")
    with pytest.raises(CharacterError):
        s.get("../x")
    p = s.create("하치")
    for _ in range(MAX_REFS):
        s.add_reference(p.id, b"x", ".png")
    with pytest.raises(CharacterError):
        s.add_reference(p.id, b"x", ".png")
