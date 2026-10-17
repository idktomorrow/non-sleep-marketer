"""브랜드 이미지(배치 C17, ADR 0046): 로고와 페르소나 캐릭터 그림 올리기.

- 캐릭터는 일러스트형 카드의 표지·마지막 장에 들어간다. 배경이 있는 그림(흰 종이 같은 바탕)은
  테두리에서 이어진 바탕색만 지워 투명하게 만든다. 윤곽선 안의 흰 부분(배, 얼굴)은 남는다.
- 로고는 세 스타일 모두 이름 옆에 작게 들어간다. 배경은 지우지 않는다(로고 바탕은 브랜드의 일부).
- 저장은 `<NSM_HOME>/brand/` 아래 우리가 정한 이름으로만 한다(사용자 파일 이름을 경로에 쓰지 않음).
"""

from __future__ import annotations

import io
import logging
import secrets
from pathlib import Path

MAX_BYTES = 10 * 1024 * 1024
MAX_SIDE = 900  # 카드에서 캐릭터는 300~400px라 이보다 크게 둘 필요가 없다
MAX_PIXELS = 40_000_000  # 이보다 큰 그림은 받지 않는다(압축 폭탄·처리 시간)
BG_TOLERANCE = 38  # 바탕색과 이만큼(0~255 채널 차이) 가까우면 바탕
EDGE_SOFT = 110  # 바탕 가장자리에서 이 차이까지는 반투명으로(윤곽선 둘레 흰 테두리 줄이기)


class BrandImageError(ValueError):
    """올린 이미지를 쓸 수 없음. 화면에 그대로 보여 준다."""


def _open(data: bytes):
    from PIL import Image, ImageOps, UnidentifiedImageError

    if len(data) > MAX_BYTES:
        raise BrandImageError("이미지는 10MB 이하만 올릴 수 있습니다")
    try:
        img = Image.open(io.BytesIO(data))
        fmt = img.format
        if img.width * img.height > MAX_PIXELS:
            raise BrandImageError("그림이 너무 큽니다(가로×세로 4천만 픽셀 이하)")
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        raise BrandImageError("PNG·JPEG·WebP 이미지만 올릴 수 있습니다") from e
    if fmt not in ("PNG", "JPEG", "WEBP"):
        raise BrandImageError("PNG·JPEG·WebP 이미지만 올릴 수 있습니다")
    # 휴대폰 사진은 회전 정보(EXIF)대로 돌려 둔다(옆으로 누운 그림 방지)
    return ImageOps.exif_transpose(img)


def has_transparency(img) -> bool:
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        alpha = img.convert("RGBA").getchannel("A")
        return alpha.getextrema()[0] < 250
    return False


def remove_background(img):
    """테두리에서 이어진 바탕색 영역을 투명하게. 이미 투명한 그림은 그대로."""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

    rgba = img.convert("RGBA")
    if has_transparency(img):
        return rgba
    rgb = rgba.convert("RGB")
    w, h = rgb.size
    border = (
        [rgb.getpixel((x, 0)) for x in range(0, w, 4)]
        + [rgb.getpixel((x, h - 1)) for x in range(0, w, 4)]
        + [rgb.getpixel((0, y)) for y in range(0, h, 4)]
        + [rgb.getpixel((w - 1, y)) for y in range(0, h, 4)]
    )
    bg = tuple(sorted(c[i] for c in border)[len(border) // 2] for i in range(3))
    # 바탕색과의 차이(채널 중 가장 큰 차이)
    diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, bg))
    r, g, b = diff.split()
    dist = ImageChops.lighter(ImageChops.lighter(r, g), b)
    near = dist.point(lambda v: 255 if v <= BG_TOLERANCE else 0)
    # 테두리에서 이어진 "가까운" 영역만 바탕(윤곽선 안의 흰 배·얼굴은 남긴다)
    filled = near.copy()
    for x in range(0, w, 8):
        for y in (0, h - 1):
            if filled.getpixel((x, y)) == 255:
                ImageDraw.floodfill(filled, (x, y), 128)
    for y in range(0, h, 8):
        for x in (0, w - 1):
            if filled.getpixel((x, y)) == 255:
                ImageDraw.floodfill(filled, (x, y), 128)
    background = filled.point(lambda v: 255 if v == 128 else 0)
    # 바탕 바로 옆(몇 px)은 바탕색과 가까운 만큼 반투명: 흰 테두리(헤일로)를 줄인다
    around = background.filter(ImageFilter.MaxFilter(5))
    soft = dist.point(
        lambda v: (
            0
            if v <= BG_TOLERANCE
            else min(255, (v - BG_TOLERANCE) * 255 // (EDGE_SOFT - BG_TOLERANCE))
        )
    )
    alpha = ImageOps.invert(background)
    edge_alpha = ImageChops.darker(alpha, soft)
    alpha = Image.composite(edge_alpha, alpha, around)
    rgba.putalpha(alpha)
    return rgba


def prepare_character(data: bytes, clear_background: bool = True):
    """캐릭터 그림: (필요하면) 바탕 지우기 → 그림만 남게 자르기 → 크기 줄이기. PIL 이미지(RGBA)."""
    img = _open(data)
    # 바탕 지우기는 픽셀마다 도는 일이라, 먼저 넉넉히(최종 크기의 2배) 줄여 둔다
    img.thumbnail((MAX_SIDE * 2, MAX_SIDE * 2))
    out = remove_background(img) if clear_background else img.convert("RGBA")
    box = out.getchannel("A").point(lambda v: 255 if v > 16 else 0).getbbox()
    if box is None:
        raise BrandImageError("그림이 보이지 않습니다(전부 투명하거나 바탕만 있는 이미지)")
    out = out.crop(box)
    w, h = out.size
    if min(w, h) < 48:
        raise BrandImageError("그림이 너무 작습니다(가로·세로 48px 이상)")
    out.thumbnail((MAX_SIDE, MAX_SIDE))
    return out


def prepare_logo(data: bytes):
    img = _open(data)
    img = img.convert("RGBA")
    img.thumbnail((400, 400))
    return img


def save_png(img, folder: Path, prefix: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{prefix}-{secrets.token_hex(4)}.png"
    img.save(path, "PNG", optimize=True)
    return path


def drop_old(folder: Path, old: str | None) -> None:
    """바뀐 이미지가 우리 폴더에 저장한 것이면 지운다. 다른 곳의 파일(설정에 직접 쓴 경로)은 건드리지 않는다."""
    if not old:
        return
    p = Path(old).resolve()
    if p.parent == folder.resolve() and p.is_file():
        try:
            p.unlink()
        except OSError as e:  # 다른 곳에서 열고 있으면(Windows) 남겨 둔다. 설정은 이미 바뀌었다
            logging.getLogger(__name__).warning("예전 그림을 지우지 못했습니다 %s: %s", p, e)
