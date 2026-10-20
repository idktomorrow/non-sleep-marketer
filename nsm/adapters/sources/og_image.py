"""기사·공식 페이지의 대표 이미지(og:image)를 사진 **후보**로 받아 둔다(S11 제안, ADR 0050).

후보일 뿐이다. 카드에 들어가려면 사람이 검토 화면에서 "이 사진 쓰기"를 눌러야 한다(ADR 0049).
안전 규칙:
- http·https만, 원문·이미지·모든 리다이렉트 홉의 호스트가 공개 IP로만 풀려야 한다(localhost·사설·링크로컬·예약 대역 거절)
- 리다이렉트 3번까지, HTML 2MB, 이미지 10MB, 픽셀 2500만, 짧은 변 600px 이상, 실제 형식 검사(Pillow)
- 실패하면 그 후보만 비운다(예외를 밖으로 내지 않음)
"""

from __future__ import annotations

import io
import ipaddress
import logging
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

log = logging.getLogger(__name__)

MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 25_000_000
MIN_SIDE = 600
MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 10.0
USER_AGENT = "non-sleep-marketer/photo-candidate (+local tool; fetches og:image once)"

_META = re.compile(
    r"<meta\s+[^>]*?(?:property|name)\s*=\s*[\"'](?:og:image(?::secure_url)?|twitter:image(?::src)?)[\"'][^>]*?>",
    re.IGNORECASE,
)
_CONTENT = re.compile(r"content\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_EXT = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}

Resolver = Callable[[str], list[str]]


@dataclass(frozen=True)
class PhotoCandidate:
    image_url: str
    source_url: str
    path: Path
    width: int
    height: int


class UnsafeUrlError(ValueError):
    """공개 http(s) 주소가 아니거나 사설·루프백 대역으로 풀리는 주소."""


def _default_resolver(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def public_addresses(url: str, resolver: Resolver = _default_resolver) -> list[str]:
    """http(s)이고 호스트가 공개 IP로만 풀리면 그 IP 목록(한 번만 조회). 아니면 빈 목록.

    하나라도 사설·루프백·링크로컬·예약 대역이면 빈 목록. 접속은 이 목록의 IP로 해야 두 번째 조회가 다른 답을 주는
    DNS 리바인딩을 막는다.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return []
    host = parts.hostname
    if host in ("localhost",) or host.endswith(".localhost") or host.endswith(".local"):
        return []
    try:
        addrs = resolver(host)
    except OSError:
        return []
    if not addrs:
        return []
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            return []
        if not ip.is_global:
            return []
    return list(addrs)


def is_public_url(url: str, resolver: Resolver = _default_resolver) -> bool:
    return bool(public_addresses(url, resolver))


def _public_ip(url: str, resolver: Resolver) -> str:
    """호스트를 한 번만 풀어 공개 IP 하나를 돌려준다. 접속도 이 IP로 해 DNS 리바인딩(두 번째 조회가 사설 주소)을 막는다."""
    addrs = public_addresses(url, resolver)
    if not addrs:
        raise UnsafeUrlError(url)
    return addrs[0]


def _pinned(url: str, ip: str) -> tuple[str, dict[str, str], dict[str, str]]:
    """같은 URL을 IP로 바꾼 주소와 Host 헤더, TLS 이름(SNI). 인증서는 원래 호스트 이름으로 검증된다."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    netloc = f"[{ip}]" if ":" in ip else ip
    if parts.port:
        netloc += f":{parts.port}"
    pinned = parts._replace(netloc=netloc).geturl()
    headers = {"User-Agent": USER_AGENT, "Host": host + (f":{parts.port}" if parts.port else "")}
    return pinned, headers, {"sni_hostname": host}


def _get(
    client: httpx.Client, url: str, max_bytes: int, resolver: Resolver
) -> tuple[str, bytes, str]:
    """리다이렉트를 손으로 따라가며 홉마다 공개 주소인지 확인하고, 확인한 IP로 접속한다. (최종 URL, 본문, Content-Type)."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        ip = _public_ip(current, resolver)
        pinned, headers, ext = _pinned(current, ip)
        with client.stream("GET", pinned, headers=headers, extensions=ext) as r:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                current = urljoin(current, r.headers["location"])
                continue
            r.raise_for_status()
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise ValueError(f"too large: {current}")
            return current, bytes(buf), r.headers.get("content-type", "")
    raise ValueError(f"too many redirects: {url}")


def extract_og_image(html: str, base_url: str) -> str | None:
    for tag in _META.finditer(html):
        m = _CONTENT.search(tag.group(0))
        if m and m.group(1).strip():
            return urljoin(base_url, m.group(1).strip())
    return None


def fetch_photo_candidate(
    source_url: str,
    dest_dir: Path,
    *,
    client: httpx.Client | None = None,
    resolver: Resolver = _default_resolver,
) -> PhotoCandidate | None:
    """원문 페이지의 og:image를 받아 dest_dir/candidate.<ext>에 저장한다. 못 구하면 None."""
    from PIL import Image

    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False)
    try:
        try:
            final_url, html_bytes, _ = _get(client, source_url, MAX_HTML_BYTES, resolver)
        except Exception as e:  # noqa: BLE001  후보 하나의 실패는 조용히 비운다
            log.info("사진 후보: 원문을 못 읽음(%s): %s", source_url, e)
            return None
        image_url = extract_og_image(html_bytes.decode("utf-8", errors="replace"), final_url)
        if not image_url:
            return None
        try:
            _, data, _ = _get(client, image_url, MAX_IMAGE_BYTES, resolver)
        except Exception as e:  # noqa: BLE001
            log.info("사진 후보: 이미지를 못 받음(%s): %s", image_url, e)
            return None
        try:
            # 머리만 읽어 크기를 먼저 보고(픽셀 폭탄은 verify 전에 거절), 전역 Pillow 상한은 건드리지 않는다(S11 리뷰)
            with Image.open(io.BytesIO(data)) as im:
                fmt, width, height = im.format, im.width, im.height
            if fmt not in _EXT or width * height > MAX_PIXELS or min(width, height) < MIN_SIDE:
                return None
            with Image.open(io.BytesIO(data)) as im:
                im.verify()
        except Exception as e:  # noqa: BLE001  손상·형식 아님·픽셀 폭탄
            log.info("사진 후보: 이미지 검사 실패(%s): %s", image_url, e)
            return None
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"candidate{_EXT[fmt]}"
        path.write_bytes(data)
        return PhotoCandidate(
            image_url=image_url, source_url=source_url, path=path, width=width, height=height
        )
    finally:
        if own:
            client.close()
