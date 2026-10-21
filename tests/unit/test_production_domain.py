"""그림 제작 순수 규칙(S15 제안, ADR 0058): spec 지문은 장면·캐릭터·참조·크기로만(대사는 아님), 2번째 컷은 1번째 결과 참조."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nsm.domain.models import CardDeck
from nsm.domain.production import (
    Cast,
    CharacterRef,
    MissingReference,
    build_spec,
    compose_prompt,
    image_slots,
)
from tests.render.test_html_renderer import FIXTURES

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8ffff3f0005fe02fea7d6a4b00000000049454e44ae426082"
)


def _ref(tmp_path: Path, name: str = "ref.png", **kw) -> CharacterRef:
    p = tmp_path / name
    p.write_bytes(PNG + name.encode())
    return CharacterRef(
        kind=kw.pop("kind", "account"),
        name="루나",
        appearance="하얀 몸, 분홍 볼",
        references=(str(p),),
        **kw,
    )


def _meme() -> CardDeck:
    return CardDeck.model_validate(json.loads((FIXTURES / "meme.json").read_text("utf-8")))


def test_slots_for_meme_and_sticker(tmp_path: Path) -> None:
    cast = Cast(hero=_ref(tmp_path))
    slots = image_slots(_meme(), cast)
    assert [s.target for s in slots] == ["s0.panel0", "s0.panel1"]
    assert slots[1].chain_from == "s0.panel0" and slots[0].width == 1536
    ed = CardDeck.model_validate(
        json.loads((FIXTURES / "editorial-sticker.json").read_text("utf-8"))
    )
    with_side = image_slots(ed, Cast(hero=cast.hero, sidekick=_ref(tmp_path, "s.png")))
    stickers = [s for s in with_side if s.role == "sticker"]
    assert len(stickers) == 1 and stickers[0].transparent  # 보조 스티커는 첫 메모 한 곳
    assert not [
        s for s in image_slots(ed, cast) if s.role == "sticker"
    ]  # 보조 없음이면 스티커 자리 없음


def test_spec_hash_ignores_dialogue_but_tracks_scene_and_refs(tmp_path: Path) -> None:
    cast = Cast(hero=_ref(tmp_path))
    deck = _meme()
    a = build_spec(image_slots(deck, cast)[0], cast, "medium")
    raw = deck.model_dump(mode="json")
    raw["slides"][0]["panels"][0]["bubble"] = "완전히 다른 대사"
    raw["slides"][0]["panels"][0]["label"] = "다른 라벨"
    b = build_spec(image_slots(CardDeck.model_validate(raw), cast)[0], cast, "medium")
    assert a.hash == b.hash  # 대사·라벨은 그림 spec이 아니다
    raw["slides"][0]["panels"][0]["scene"] = "바닷가, 들뜬 표정"
    c = build_spec(image_slots(CardDeck.model_validate(raw), cast)[0], cast, "medium")
    assert c.hash != a.hash
    other = Cast(hero=_ref(tmp_path, "other.png"))
    assert (
        build_spec(image_slots(deck, other)[0], other, "medium").hash != a.hash
    )  # 참조 내용이 다르면
    assert build_spec(image_slots(deck, cast)[0], cast, "high").hash != a.hash


def test_second_panel_includes_first_result(tmp_path: Path) -> None:
    cast = Cast(hero=_ref(tmp_path))
    second = image_slots(_meme(), cast)[1]
    first_img = tmp_path / "first.png"
    first_img.write_bytes(PNG + b"1")
    s1 = build_spec(second, cast, "medium", chain_image=str(first_img))
    assert s1.references[-1] == str(first_img) and "DIFFERENT scene" in s1.prompt
    first_img.write_bytes(PNG + b"2")  # 1번째 컷이 바뀌면 2번째 spec도 바뀐다
    assert build_spec(second, cast, "medium", chain_image=str(first_img)).hash != s1.hash


def test_missing_reference_and_real_only_do_not_fall_back(tmp_path: Path) -> None:
    slot = image_slots(_meme(), Cast(hero=_ref(tmp_path)))[0]
    with pytest.raises(MissingReference, match="참조"):
        build_spec(slot, Cast(hero=CharacterRef(kind="content", name="치이카와")), "medium")
    with pytest.raises(MissingReference, match="실제 자료만"):
        build_spec(slot, Cast(hero=_ref(tmp_path, use="real_only", kind="content")), "medium")
    # 콘텐츠 캐릭터도 참조가 있으면 생성 주인공이 될 수 있다(정체성 출처 ≠ 생성 여부)
    assert (
        build_spec(slot, Cast(hero=_ref(tmp_path, kind="content")), "medium").detail["character"][
            "kind"
        ]
        == "content"
    )


def test_prompt_is_deterministic_and_has_no_text(tmp_path: Path) -> None:
    ref = _ref(tmp_path)
    slot = image_slots(_meme(), Cast(hero=ref))[0]
    assert compose_prompt(slot, ref, False) == compose_prompt(slot, ref, False)
    spec = build_spec(slot, Cast(hero=ref), "medium")
    assert "speech bubbles" in spec.negative and "top-left corner" in spec.prompt
