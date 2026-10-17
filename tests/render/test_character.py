"""일러스트형 카드 캐릭터(C17, ADR 0046): 설정 → 그림, 표지·마지막 장에만, AI 표기, 캐시 지문."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nsm.config.schema import BrandSettings
from nsm.domain.models import CardDeck
from nsm.render.character import CHARACTERS_DIR, DRAWN, CharacterArt, character_art
from nsm.render.html_renderer import build_html

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "decks"


def _deck() -> CardDeck:
    return CardDeck.model_validate(json.loads((FIXTURES / "list.json").read_text("utf-8")))


def _brand(**kw) -> BrandSettings:
    return BrandSettings(name="b", industry="i", **kw)


def _slides(html: str) -> list[str]:
    return html.split('<section class="slide')[1:]


def test_bundled_characters_exist_and_are_transparent() -> None:
    from PIL import Image

    for name in ("parrot.png", "hamster.png"):
        img = Image.open(CHARACTERS_DIR / name)
        assert img.mode == "RGBA"
        assert img.getchannel("A").getextrema()[0] == 0  # 바탕이 지워져 있다


def test_character_art_choices(tmp_path: Path) -> None:
    assert character_art(_brand()).cover == CHARACTERS_DIR / "parrot.png"  # 기본은 앵무새
    assert character_art(_brand(character="hamster")).outro == CHARACTERS_DIR / "hamster.png"
    assert character_art(_brand(character="drawn")) == DRAWN
    none = character_art(_brand(character="none"))
    assert none.cover is None and none.outro is None and not none.drawn
    cover = tmp_path / "c.png"
    cover.write_bytes(b"x")
    custom = character_art(_brand(character="custom", character_cover=str(cover)))
    assert custom.cover == custom.outro == cover  # 마지막 장 그림이 없으면 표지 그림
    gone = character_art(_brand(character="custom", character_cover=str(tmp_path / "x.png")))
    assert gone == DRAWN  # 올린 파일이 사라지면 코드 캐릭터로
    assert character_art(_brand(character_ai_made=False)).ai_made is False
    # C19: 기본 캐릭터(AI로 만든 만화 그림)는 기본으로 "AI 생성 이미지"를 붙이지 않는다
    assert character_art(_brand()).ai_made is False
    assert character_art(_brand(character_ai_made=True)).ai_made is True


def test_image_character_on_cover_and_outro_only() -> None:
    p = CHARACTERS_DIR / "hamster.png"
    html = build_html(_deck(), "illust", CharacterArt(cover=p, outro=p, ai_made=True))
    slides = _slides(html)
    assert "mascot-img" in slides[0] and "mascot-img" in slides[-1]
    assert all("mascot" not in s for s in slides[1:-1])
    assert '<svg class="mascot' not in html  # 코드 캐릭터 대신 그림
    # AI로 만든 그림이 있는 장에만 AI 표기
    assert ['data-label="ai-generated"' in s for s in slides] == [
        True,
        *([False] * (len(slides) - 2)),
        True,
    ]


def test_hand_drawn_character_has_no_ai_caption() -> None:
    p = CHARACTERS_DIR / "parrot.png"
    html = build_html(_deck(), "illust", CharacterArt(cover=p, outro=p, ai_made=False))
    assert "mascot-img" in html and 'data-label="ai-generated"' not in html


@pytest.mark.parametrize(
    ("art", "img", "svg"), [(DRAWN, False, True), (CharacterArt(), False, False)]
)
def test_drawn_and_none(art: CharacterArt, img: bool, svg: bool) -> None:
    html = build_html(_deck(), "illust", art)
    assert ("mascot-img" in html) is img
    assert ('<svg class="mascot' in html) is svg


@pytest.mark.parametrize("style", ["clean", "photo"])
def test_other_styles_ignore_character(style: str) -> None:
    p = CHARACTERS_DIR / "parrot.png"
    html = build_html(_deck(), style, CharacterArt(cover=p, outro=p, ai_made=True))
    assert "mascot-img" not in html and '<svg class="mascot' not in html
    assert 'data-label="ai-generated"' not in html


def test_fingerprint_follows_file_and_label(tmp_path: Path) -> None:
    p = tmp_path / "c.png"
    p.write_bytes(b"one")
    a = CharacterArt(cover=p, outro=p).fingerprint
    assert CharacterArt(cover=p, outro=p, ai_made=True).fingerprint != a
    p.write_bytes(b"two")
    assert CharacterArt(cover=p, outro=p).fingerprint != a
