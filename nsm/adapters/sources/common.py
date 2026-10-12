"""수집 소스 공통 도구: URL 정규화·해시, 예의 있는 지연, 텍스트 정리."""

from __future__ import annotations

import hashlib
import random
import re
import time
from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_EXACT = {"fbclid", "gclid", "ref", "from", "igshid", "mc_cid", "mc_eid"}

MAX_TEXT = 20_000


def normalize_url(url: str) -> str:
    """같은 글을 같은 키로: 스킴·호스트 소문자, 추적 파라미터 제거, 끝 슬래시 제거."""
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not (k.lower().startswith("utm_") or k.lower() in _TRACKING_EXACT)
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


# 화면 낭독기용 안내 문구(링크·본문에 섞여 들어온다)
A11Y_NOISE = re.compile(
    r"새\s*창\s*열림|새\s*창으로\s*열기|본문\s*바로\s*가기|메뉴\s*바로\s*가기|"
    r"주메뉴\s*바로\s*가기|이미지\s*확대\s*보기|동영상\s*재생|접근성\s*안내"
)
# 보이지 않는 글자(네이버 블로그 본문에 많다)
_ZERO_WIDTH = re.compile("[​‌‍⁠﻿]")
# 그 줄 전체가 이것뿐이면 버린다(메뉴·버튼·공유 도구 같은 화면 잡음)
NAV_LINES = frozenset(
    [
        "로그인",
        "로그아웃",
        "회원가입",
        "메뉴",
        "전체메뉴",
        "검색",
        "홈",
        "이전",
        "다음",
        "이전글",
        "다음글",
        "목록",
        "맨위로",
        "맨",
        "위로",
        "top",
        "공유",
        "공유하기",
        "url복사",
        "url",
        "복사",
        "링크복사",
        "링크",
        "복사",
        "신고",
        "신고하기",
        "구독",
        "구독하기",
        "이웃추가",
        "서로이웃",
        "추가",
        "댓글",
        "댓글쓰기",
        "공감",
        "좋아요",
        "스크랩",
        "인쇄",
        "글자크기",
        "본문",
        "기타",
        "기능",
        "번역",
        "번역하기",
        "more",
        "더보기",
        "접기",
        "펼치기",
        "카테고리",
        "프롤로그",
        "태그",
    ]
)
_NAV_WORDS = set(NAV_LINES)
_NAV_JOINED = {w.replace(" ", "") for w in _NAV_WORDS}


def _is_nav(line: str) -> bool:
    s = line.strip().lower()
    if not s:
        return True
    if s in _NAV_WORDS or s.replace(" ", "") in _NAV_JOINED:
        return True
    words = s.split()
    # "로그인 회원가입 메뉴" 같은 메뉴 나열: 모든 낱말이 메뉴 말이면 잡음
    return len(words) <= 6 and all(w in _NAV_WORDS for w in words)


def clean_text(text: str, n: int = MAX_TEXT) -> str:
    """화면 낭독기 문구와 메뉴·버튼 줄을 걷어내고 공백을 정리한다."""
    text = _ZERO_WIDTH.sub("", A11Y_NOISE.sub(" ", text or ""))
    lines = [ln.strip() for ln in text.splitlines()]
    kept = [ln for ln in lines if not _is_nav(ln)]
    return clip(" ".join(kept), n)


def clip(text: str, n: int = MAX_TEXT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n]
