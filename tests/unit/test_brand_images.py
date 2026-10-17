"""로고·캐릭터 그림 처리(C17, ADR 0046): 바탕 지우기, 자르기, 거절, 지우기 범위."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from nsm.app.brand_images import (
    BrandImageError,
    drop_old,
    prepare_character,
    prepare_logo,
    remove_background,
    save_png,
)

PAPER = (251, 249, 244)  # 팀원 그림처럼 흰 종이 바탕


def _png(img: Image.Image, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def _drawing() -> Image.Image:
    """종이 바탕 + 검은 윤곽선 동그라미(안은 흰 배) + 초록 날개."""
    img = Image.new("RGB", (400, 400), PAPER)
    d = ImageDraw.Draw(img)
    d.ellipse((100, 100, 300, 300), fill=(250, 246, 236), outline=(30, 30, 30), width=12)
    d.ellipse((60, 180, 130, 240), fill=(160, 180, 140), outline=(30, 30, 30), width=8)
    return img


def _tiny() -> Image.Image:
    img = Image.new("RGB", (200, 200), PAPER)
    ImageDraw.Draw(img).ellipse((90, 90, 110, 110), fill=(30, 30, 30))
    return img


def test_background_outside_outline_becomes_transparent() -> None:
    out = remove_background(_drawing())
    a = out.getchannel("A")
    assert a.getpixel((5, 5)) == 0 and a.getpixel((395, 390)) == 0  # 바깥 종이
    assert a.getpixel((200, 200)) == 255  # 윤곽선 안의 흰 배는 남는다
    assert a.getpixel((200, 106)) == 255  # 윤곽선
    assert a.getpixel((90, 210)) == 255  # 날개


def test_transparent_image_is_kept() -> None:
    img = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((40, 40, 160, 160), fill=(255, 255, 255, 255))
    out = remove_background(img)
    assert out.getchannel("A").getpixel((100, 100)) == 255  # 흰 그림이 바탕으로 지워지지 않음
    assert out.getchannel("A").getpixel((5, 5)) == 0


def test_prepare_character_crops_and_limits_size() -> None:
    big = _drawing().resize((2000, 2000))
    out = prepare_character(_png(big, "JPEG"))
    assert out.mode == "RGBA" and max(out.size) <= 900
    # 바탕을 잘라 그림만 남는다(정사각 2000이 아니라 그림 크기 비율)
    assert out.size[0] < 900 or out.size[1] < 900
    assert out.getchannel("A").getpixel((0, 0)) < 255


def test_keep_background_when_asked() -> None:
    out = prepare_character(_png(_drawing()), clear_background=False)
    assert out.getchannel("A").getpixel((0, 0)) == 255


@pytest.mark.parametrize(
    ("data", "msg"),
    [
        (b"not an image", "PNG·JPEG·WebP"),
        (_png(Image.new("RGB", (300, 300), PAPER)), "그림이 보이지 않습니다"),
        (_png(_tiny()), "너무 작습니다"),
        (b"\x89PNG" + b"\x00" * (10 * 1024 * 1024), "10MB"),
    ],
    ids=["not-image", "only-paper", "tiny", "too-big"],
)
def test_bad_character_images_are_rejected(data: bytes, msg: str) -> None:
    with pytest.raises(BrandImageError, match=msg):
        prepare_character(data)


def test_gif_is_rejected() -> None:
    with pytest.raises(BrandImageError):
        prepare_logo(_png(Image.new("RGB", (100, 100), (255, 0, 0)), "GIF"))


def test_logo_keeps_background_and_shrinks() -> None:
    out = prepare_logo(_png(Image.new("RGB", (1200, 600), (20, 40, 200)), "JPEG"))
    assert out.size == (400, 200) and out.getchannel("A").getpixel((0, 0)) == 255


def test_save_and_drop_only_inside_folder(tmp_path: Path) -> None:
    folder = tmp_path / "brand"
    p = save_png(prepare_logo(_png(Image.new("RGB", (100, 100), (1, 2, 3)))), folder, "logo")
    assert p.parent == folder and p.suffix == ".png"
    outside = tmp_path / "mine.png"
    outside.write_bytes(b"x")
    drop_old(folder, str(outside))  # 설정에 직접 쓴 다른 곳의 파일은 지우지 않는다
    assert outside.exists()
    drop_old(folder, str(p))
    assert not p.exists()
    drop_old(folder, None)


def test_phone_photo_is_rotated_upright() -> None:
    img = Image.new("RGB", (400, 200), PAPER)
    ImageDraw.Draw(img).ellipse((150, 50, 250, 150), outline=(30, 30, 30), width=8)
    exif = Image.Exif()
    exif[0x0112] = 6  # 오른쪽으로 90도 돌려 보라는 회전 정보
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif.tobytes())
    out = prepare_logo(buf.getvalue())
    assert out.size[1] > out.size[0]  # 가로 사진이 세로로 바로 섬


def test_big_drawing_is_shrunk_before_background_removal() -> None:
    big = _drawing().resize((4000, 4000))
    out = prepare_character(_png(big, "JPEG"))
    assert max(out.size) <= 900 and out.getchannel("A").getpixel((0, 0)) < 255
