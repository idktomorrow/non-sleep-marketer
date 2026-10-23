"""일러스트형 카드 캐릭터(C17, ADR 0046): 설정 → 그림, 표지·마지막 장에만, AI 표기, 캐시 지문."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nsm.config.schema import BrandSettings
from nsm.domain.models import CardDeck
from nsm.render.character import DRAWN, CharacterArt, character_art
from nsm.render.html_renderer import build_html

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "decks"


def _deck() -> CardDeck:
    return CardDeck.model_validate(json.loads((FIXTURES / "list.json").read_text("utf-8")))


def _brand(**kw) -> BrandSettings:
    return BrandSettings(name="b", industry="i", **kw)


def _slides(html: str) -> list[str]:
    return html.split('<section class="slide')[1:]


@pytest.fixture
def art_png(tmp_path: Path) -> Path:
    """올린 캐릭터처럼 투명 바탕 PNG 한 장."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse(
        (40, 40, 260, 260), fill=(250, 240, 220, 255), outline=(30, 30, 30, 255), width=10
    )
    p = tmp_path / "character.png"
    img.save(p)
    return p


def test_character_art_choices(tmp_path: Path) -> None:
    # C20: 동봉 캐릭터는 없다. 기본은 캐릭터 없음
    none = character_art(_brand())
    assert none.cover is None and none.outro is None and not none.drawn
    assert character_art(_brand(character="drawn")) == DRAWN
    assert _brand(character="parrot").character == "none"  # 예전 설정 파일은 없음으로
    assert _brand(character="hamster").character == "none"
    cover = tmp_path / "c.png"
    cover.write_bytes(b"x")
    custom = character_art(_brand(character="custom", character_cover=str(cover)))
    assert custom.cover == custom.outro == cover  # 마지막 장 그림이 없으면 표지 그림
    gone = character_art(_brand(character="custom", character_cover=str(tmp_path / "x.png")))
    assert gone == DRAWN  # 올린 파일이 사라지면 코드 캐릭터로
    # C19: 기본은 "AI 생성 이미지"를 붙이지 않는다
    assert character_art(_brand(character="custom", character_cover=str(cover))).ai_made is False
    on = _brand(character="custom", character_cover=str(cover), character_ai_made=True)
    assert character_art(on).ai_made is True


def test_no_character_by_default_in_cards() -> None:
    html = build_html(_deck(), "illust", character_art(_brand()))
    assert "mascot-img" not in html and '<svg class="mascot' not in html


def test_image_character_on_cover_and_outro_only(art_png: Path) -> None:
    p = art_png
    html = build_html(_deck(), "illust", CharacterArt(cover=p, outro=p, ai_made=True))
    slides = _slides(html)
    assert "mascot-img" in slides[0] and "mascot-img" in slides[-1]
    assert all("mascot" not in s for s in slides[1:-1])
    assert '<svg class="mascot' not in html  # 코드 캐릭터 대신 그림
    # AI 표기는 카드가 아니라 게시 글에(S18, 사용자 결정 2026-10-23)
    assert not any('data-label="ai-generated"' in s for s in slides)


def test_hand_drawn_character_has_no_ai_caption(art_png: Path) -> None:
    p = art_png
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
def test_other_styles_ignore_character(style: str, art_png: Path) -> None:
    p = art_png
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
