"""사진 후보 수집(S11): og:image를 받아 두되 사설 주소·큰 파일·가짜 이미지는 거절한다."""

from __future__ import annotations

import io
from pathlib import Path

import httpx
from PIL import Image

from nsm.adapters.sources.og_image import (
    extract_og_image,
    fetch_photo_candidate,
    is_public_url,
)

PUBLIC = {"news.example.com": ["93.184.216.34"], "img.example.com": ["93.184.216.35"]}


def _resolver(table):
    def resolve(host: str) -> list[str]:
        if host not in table:
            raise OSError("no such host")
        return table[host]

    return resolve


def _png(w: int = 800, h: int = 1000) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 120, 80)).save(buf, "PNG")
    return buf.getvalue()


def _client(routes: dict[str, httpx.Response]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(str(request.url), httpx.Response(404))

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


HTML = '<html><head><meta property="og:title" content="x"><meta property="og:image" content="/photos/1.png"></head></html>'


def test_extract_og_image_resolves_relative_and_twitter_fallback() -> None:
    assert (
        extract_og_image(HTML, "https://news.example.com/a/1")
        == "https://news.example.com/photos/1.png"
    )
    tw = '<meta name="twitter:image" content="https://img.example.com/t.jpg">'
    assert extract_og_image(tw, "https://news.example.com/") == "https://img.example.com/t.jpg"
    assert extract_og_image("<html></html>", "https://news.example.com/") is None


def test_public_url_rejects_private_loopback_and_non_http() -> None:
    r = _resolver(
        {**PUBLIC, "intra": ["10.0.0.5"], "home": ["127.0.0.1"], "meta": ["169.254.169.254"]}
    )
    assert is_public_url("https://news.example.com/a", r)
    assert not is_public_url("http://intra/a", r)
    assert not is_public_url("http://home/a", r)
    assert not is_public_url("http://meta/latest", r)
    assert not is_public_url("http://localhost/a", r)
    assert not is_public_url("file:///etc/passwd", r)
    assert not is_public_url("ftp://news.example.com/a", r)


def test_fetches_and_saves_candidate(tmp_path: Path) -> None:
    client = _client(
        {
            "https://news.example.com/a/1": httpx.Response(200, text=HTML),
            "https://news.example.com/photos/1.png": httpx.Response(200, content=_png()),
        }
    )
    c = fetch_photo_candidate(
        "https://news.example.com/a/1", tmp_path, client=client, resolver=_resolver(PUBLIC)
    )
    assert c is not None and c.path.name == "candidate.png" and (c.width, c.height) == (800, 1000)
    assert (
        c.image_url == "https://news.example.com/photos/1.png"
        and c.source_url == "https://news.example.com/a/1"
    )


def test_redirect_to_private_address_is_blocked(tmp_path: Path) -> None:
    client = _client(
        {
            "https://news.example.com/a/1": httpx.Response(
                302, headers={"location": "http://intra/secret"}
            ),
            "http://intra/secret": httpx.Response(200, text=HTML),
        }
    )
    r = _resolver({**PUBLIC, "intra": ["192.168.1.9"]})
    assert (
        fetch_photo_candidate("https://news.example.com/a/1", tmp_path, client=client, resolver=r)
        is None
    )
    assert not list(tmp_path.iterdir())


def test_rejects_fake_small_or_huge_images(tmp_path: Path) -> None:
    base = {"https://news.example.com/a/1": httpx.Response(200, text=HTML)}
    fake = _client(
        {
            **base,
            "https://news.example.com/photos/1.png": httpx.Response(
                200, content=b"GIF89a not really"
            ),
        }
    )
    assert (
        fetch_photo_candidate(
            "https://news.example.com/a/1", tmp_path, client=fake, resolver=_resolver(PUBLIC)
        )
        is None
    )
    small = _client(
        {
            **base,
            "https://news.example.com/photos/1.png": httpx.Response(200, content=_png(300, 300)),
        }
    )
    assert (
        fetch_photo_candidate(
            "https://news.example.com/a/1", tmp_path, client=small, resolver=_resolver(PUBLIC)
        )
        is None
    )
    huge = _client(
        {
            **base,
            "https://news.example.com/photos/1.png": httpx.Response(
                200, content=b"\x89PNG" + b"0" * (10 * 1024 * 1024 + 1)
            ),
        }
    )
    assert (
        fetch_photo_candidate(
            "https://news.example.com/a/1", tmp_path, client=huge, resolver=_resolver(PUBLIC)
        )
        is None
    )


def test_too_many_redirects_and_missing_page(tmp_path: Path) -> None:
    routes = {
        f"https://news.example.com/r{i}": httpx.Response(
            301, headers={"location": f"https://news.example.com/r{i + 1}"}
        )
        for i in range(6)
    }
    client = _client(routes)
    assert (
        fetch_photo_candidate(
            "https://news.example.com/r0", tmp_path, client=client, resolver=_resolver(PUBLIC)
        )
        is None
    )
    assert (
        fetch_photo_candidate(
            "https://news.example.com/missing",
            tmp_path,
            client=_client({}),
            resolver=_resolver(PUBLIC),
        )
        is None
    )
