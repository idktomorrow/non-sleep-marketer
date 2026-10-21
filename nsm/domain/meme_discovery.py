"""밈 탐색 결과의 순수 규칙(S16 제안, ADR 0060): 사례 묶기와 분류(A·B·C), 근거 기록.

- A 최근 확산 확인: 서로 다른 작성자 3명 이상의 변주 + 가장 최근 사례가 7일 안
- B 지속 형식: 서로 다른 작성자 3명 이상이지만 사례가 30일 넘게 퍼져 있음(계속 쓰이는 형식)
- C 근거 부족 발견: 그 밖(작성자 1~2명, 날짜 미상 등). D 창작은 사례 없이 만든 밈
- 반응 수치를 못 가져오면 None(미확인). 플랫폼이 다른 수치는 더하지 않는다. 시간차 관측이 없으면 "급상승"이라 하지 않는다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from nsm.domain.picks_policy import jaccard, normalize_tokens

RECENT = timedelta(days=7)
LASTING = timedelta(days=30)
VARIANT_JACCARD = 0.34
MIN_AUTHORS = 3

PLATFORMS = {
    "blog.naver.com": "네이버 블로그",
    "m.blog.naver.com": "네이버 블로그",
    "cafe.naver.com": "네이버 카페",
    "x.com": "X",
    "twitter.com": "X",
    "www.instagram.com": "인스타그램",
    "www.threads.net": "스레드",
    "www.youtube.com": "유튜브",
}
CATEGORY_LABELS = {
    "A": "최근 확산 밈",
    "B": "계속 쓰이는 형식",
    "C": "발견 후보(근거 부족)",
    "D": "창작 밈",
}


def platform_of(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return PLATFORMS.get(host, host or "알 수 없음")


def author_of(url: str) -> str:
    """같은 사람의 글을 한 번만 세려고: 블로그·카페·X는 경로 첫 칸이 작성자(또는 카페) id."""
    parts = urlsplit(url)
    first = parts.path.strip("/").split("/")[0] if parts.path.strip("/") else ""
    # 모바일·www 주소도 같은 사람(m.blog.naver.com/u1 == blog.naver.com/u1, S16 리뷰)
    host = parts.netloc.lower()
    for prefix in ("m.", "mobile.", "www."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
    if host == "twitter.com":
        host = "x.com"
    return f"{host}/{first.lower()}"


@dataclass
class Sighting:
    url: str
    title: str
    snippet: str
    platform: str
    posted_at: datetime | None
    observed_at: datetime
    author: str
    metrics: dict | None = None  # 반응 수치. 못 가져오면 None(미확인) — 추정하지 않는다
    body_checked: bool = False  # 원문을 열어 봤는지(검색 결과 요약만 봤으면 False)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["posted_at"] = self.posted_at.isoformat() if self.posted_at else None
        d["observed_at"] = self.observed_at.isoformat()
        return d


@dataclass
class MemeCandidate:
    key: str  # 대표 제목
    category: str
    sightings: list[Sighting] = field(default_factory=list)
    reason: str = ""

    @property
    def authors(self) -> int:
        return len({s.author for s in self.sightings})

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "category": self.category,
            "label": CATEGORY_LABELS[self.category],
            "authors": self.authors,
            "reason": self.reason,
            "sightings": [s.to_dict() for s in self.sightings[:8]],
        }


def group_sightings(sightings: list[Sighting]) -> list[list[Sighting]]:
    groups: list[tuple[set[str], list[Sighting]]] = []
    for s in sightings:
        toks = normalize_tokens(f"{s.title} {s.snippet[:80]}")
        for gt, members in groups:
            if jaccard(toks, gt) >= VARIANT_JACCARD:
                members.append(s)
                gt |= toks
                break
        else:
            groups.append((set(toks), [s]))
    return [m for _, m in groups]


def classify(group: list[Sighting], now: datetime) -> MemeCandidate:
    authors = len({s.author for s in group})
    dated = [s.posted_at for s in group if s.posted_at is not None]
    newest = max(dated) if dated else None
    oldest = min(dated) if dated else None
    key = group[0].title
    if authors >= MIN_AUTHORS and newest is not None and now - newest <= RECENT:
        if oldest is not None and newest - oldest > LASTING:
            cat, why = (
                "B",
                f"작성자 {authors}명이 {(newest - oldest).days}일에 걸쳐 쓴 형식(최근에도 쓰임)",
            )
        else:
            cat, why = "A", f"서로 다른 작성자 {authors}명의 변주, 가장 최근 {newest:%m/%d}"
    elif authors >= MIN_AUTHORS:
        cat, why = "B", f"작성자 {authors}명이 쓴 형식(최근 7일 사례는 없음)"
    else:
        cat, why = (
            "C",
            f"작성자 {authors}명뿐이라 확산 근거 부족" + ("" if dated else ", 날짜 미상"),
        )
    return MemeCandidate(key=key, category=cat, sightings=group, reason=why)


def candidates(sightings: list[Sighting], now: datetime) -> list[MemeCandidate]:
    out = [classify(g, now) for g in group_sightings(sightings)]
    order = {"A": 0, "B": 1, "C": 2}
    out.sort(key=lambda c: (order[c.category], -c.authors))
    return out
