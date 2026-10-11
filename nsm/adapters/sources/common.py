"""수집 소스 공통 도구: URL 정규화·해시, 예의 있는 지연."""

from __future__ import annotations

import hashlib
import random
import time
from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING = ("utm_", "fbclid", "gclid", "ref", "from")

MAX_TEXT = 20_000


def normalize_url(url: str) -> str:
    """같은 글을 같은 키로: 스킴·호스트 소문자, 추적 파라미터 제거, 끝 슬래시 제거."""
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not any(k.lower().startswith(t) for t in _TRACKING)
    ]
    path = parts.path.rstrip("/") or "/"
    host = parts.netloc.lower()
    if host.startswith("m.blog.naver.com"):
        host = host[2:]  # 모바일·PC 블로그 주소를 같은 글로
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(sorted(query)), parts.fragment))


def url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()[:32]


class Pacer:
    """요청 사이에 무작위 지연을 둔다. 테스트에서는 sleep을 바꿔 끼운다."""

    def __init__(
        self,
        min_s: float,
        max_s: float,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.min_s, self.max_s = min_s, max_s
        self.sleep = sleep
        self.rng = rng or random.Random()
        self._first = True

    def wait(self) -> None:
        if self._first:
            self._first = False
            return
        self.sleep(self.rng.uniform(self.min_s, self.max_s))


def clip(text: str, n: int = MAX_TEXT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n]
