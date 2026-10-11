"""S-α 렌더러가 들어오기 전까지 쓰는 대체 렌더러. 슬라이드마다 단색 PNG를 만든다."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from nsm.domain.models import CardDeck

WIDTH, HEIGHT = 1080, 1350


def _solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    row = b"\x00" + bytes(rgb) * width
    raw = row * height

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


class PlaceholderRenderer:
    def render(self, deck: CardDeck, out_dir: Path) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        png = _solid_png(WIDTH, HEIGHT, _hex_to_rgb(deck.brand.primary_color))
        paths = []
        for i, _ in enumerate(deck.slides, start=1):
            p = out_dir / f"slide-{i:02d}.png"
            p.write_bytes(png)
            paths.append(p)
        return paths
