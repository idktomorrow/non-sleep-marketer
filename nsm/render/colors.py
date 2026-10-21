"""소재 사진에서 카드 색을 뽑는다(S14 제안): 레퍼런스처럼 포인트 색이 사진을 따라간다.

Pillow로 축소·양자화해 가장 많은 색 중 채도가 있는 것을 주 색으로, 그다음 다른 색상을 보조 색으로.
흰·검정·회색은 건너뛴다. 결과는 덱 `brand.primary_color/accent_color`에 들어가며 팔레트가 대비를 보정한다.
"""

from __future__ import annotations

import colorsys
from pathlib import Path


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def dominant_colors(path: Path | str, n: int = 12) -> list[tuple[int, int, int]]:
    """자주 쓰인 색부터(회색 계열 제외). 못 읽으면 빈 목록."""
    try:
        from PIL import Image

        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((160, 160))
            q = im.quantize(colors=n, method=Image.Quantize.MEDIANCUT)
            palette = q.getpalette() or []
            counts = sorted(q.getcolors() or [], reverse=True)
    except Exception:  # noqa: BLE001  깨진 파일·지원 안 하는 형식
        return []
    out = []
    for _count, idx in counts:
        r, g, b = palette[idx * 3 : idx * 3 + 3]
        _h, lum, sat = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        if sat < 0.18 or lum < 0.12 or lum > 0.92:
            continue  # 회색·검정·흰색은 포인트 색이 못 된다
        out.append((r, g, b))
    return out


def colors_from_photo(path: Path | str) -> tuple[str, str] | None:
    """(주 색, 보조 색) 16진수. 쓸 만한 색이 없으면 None(기존 브랜드 색 유지)."""
    cands = dominant_colors(path)
    if not cands:
        return None
    primary = cands[0]
    h0 = colorsys.rgb_to_hls(*(c / 255 for c in primary))[0]
    accent = next(
        (c for c in cands[1:] if abs(colorsys.rgb_to_hls(*(x / 255 for x in c))[0] - h0) > 0.08),
        None,
    )
    if accent is None:
        # 다른 색상이 없으면 주 색의 밝은 변형
        h, lum, sat = colorsys.rgb_to_hls(*(c / 255 for c in primary))
        r, g, b = colorsys.hls_to_rgb(h, min(0.82, lum + 0.3), sat)
        accent = (int(r * 255), int(g * 255), int(b * 255))
    return _hex(primary), _hex(accent)
