"""소재 사진 색 추출(S14 제안): 채도 있는 주 색·보조 색을 뽑고, 회색·흰색뿐이면 None(브랜드 색 유지)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from nsm.app.card_editor import apply_photo_colors
from nsm.render.colors import colors_from_photo
from tests.fakes import FakeTextGenerator, load_source_items


def _photo(path: Path, colors: list[tuple[int, int, int]]) -> Path:
    im = Image.new("RGB", (120, 120), colors[0])
    px = im.load()
    for y in range(120):
        for x in range(120):
            px[x, y] = colors[(x // 40) % len(colors)]
    im.save(path)
    return path


def test_extracts_saturated_primary_and_distinct_accent(tmp_path: Path) -> None:
    p = _photo(tmp_path / "a.png", [(30, 60, 150), (30, 60, 150), (240, 160, 40)])
    primary, accent = colors_from_photo(p)
    assert primary.startswith("#") and accent.startswith("#") and primary != accent
    r, b = int(primary[1:3], 16), int(primary[5:7], 16)
    assert b > r  # 파란 계열이 주 색


def test_grey_photo_keeps_brand_colors(tmp_path: Path, app_config) -> None:
    p = _photo(tmp_path / "g.png", [(128, 128, 128), (250, 250, 250), (20, 20, 20)])
    assert colors_from_photo(p) is None
    items = load_source_items()[:1]
    from nsm.domain.models import ContentBrief

    gen = FakeTextGenerator().generate_content(
        ContentBrief(topic="t", template="info", sources=items),
        app_config.persona,
        app_config.brand,
    )
    assert apply_photo_colors(gen, p).deck.brand.primary_color == gen.deck.brand.primary_color
    colored = _photo(tmp_path / "c.png", [(200, 40, 60)])
    assert apply_photo_colors(gen, colored).deck.brand.primary_color != gen.deck.brand.primary_color
