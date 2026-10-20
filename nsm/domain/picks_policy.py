"""오늘의 추천 순수 규칙(S11 제안, ADR 0050): 자료를 사건 단위로 묶고, 최신성과 다양성으로 순서를 매긴다.

외부 호출·DB 접근이 없다. 한계: 번역 없이 토큰 겹침으로만 묶으므로, 별칭을 대표 주제로 바꾼 뒤에도
공통 토큰(브랜드명·영문·숫자)이 없는 한국어·일본어 제목은 다른 사건으로 남을 수 있다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from nsm.domain.models import SourceItem

Freshness = Literal["fresh", "stale", "unknown"]

SAME_EVENT_JACCARD = 0.4
# 숫자·영문·한글·가나·한자를 따로 자른다("10월23일" → 10, 23 / "10月23日" → 10, 23 이 서로 겹치게)
_TOKEN = re.compile(r"\d+|[A-Za-z]+|[가-힣]+|[ぁ-んァ-ヶー]+|[一-龥]+")
# 굿즈 소식에 자주 나오는 일본어 낱말 → 한국어(같은 사건을 한·일 기사가 같이 보도할 때 묶이게). 번역기는 아니다
JA_KO: dict[str, str] = {
    "コラボ": "콜라보",
    "コラボレーション": "콜라보",
    "グッズ": "굿즈",
    "イベント": "행사",
    "ポップアップ": "팝업",
    "キャンペーン": "캠페인",
    "限定": "한정",
    "新作": "신작",
    "新商品": "신상",
    "発売": "출시",
    "発表": "발표",
    "公開": "공개",
    "開催": "개최",
    "店舗": "매장",
    "予約": "예약",
    "映画": "극장판",
    "劇場版": "극장판",
}
_DATE_PATTERNS = (
    re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일"),
    re.compile(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})"),
    re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)"),
)
_QUOTED = re.compile(r"[『「“\"'‘]([^』」”\"'’]{2,40})[』」”\"'’]")
# 사건 구분에 도움이 안 되는 낱말(조사·일반어). 짧게만 둔다
_STOP = frozenset(
    [
        "그리고", "그래서", "하지만", "또는", "및", "등", "더", "가장", "관련", "소식",
        "정리", "총정리", "공개", "출시", "발표", "이번", "오늘", "내일", "올해",
    ]
)  # fmt: skip


def normalize_tokens(text: str, aliases: dict[str, list[str]] | None = None) -> set[str]:
    """제목을 비교용 토큰으로. 별칭(ちいかわ, Chiikawa)은 대표 주제(치이카와)로 바꾼 뒤 자른다."""
    low = text.lower()
    for ja, ko in JA_KO.items():
        low = low.replace(ja.lower(), f" {ko} ")
    for canonical, names in (aliases or {}).items():
        for name in sorted(names, key=len, reverse=True):
            low = low.replace(name.lower(), f" {canonical.lower()} ")
        low = low.replace(canonical.lower(), f" {canonical.lower()} ")
    out: set[str] = set()
    for tok in _TOKEN.findall(low):
        if len(tok) < 2 and not tok.isdigit():
            continue
        if tok in _STOP:
            continue
        # 대표 주제가 여러 낱말이면 낱말마다 토큰으로 들어간다(치이카와 굿즈 → 치이카와, 굿즈)
        out.add(tok)
    return out


def date_tokens(text: str) -> set[str]:
    """제목에 적힌 날짜(월-일). 날짜가 다르면 같은 캐릭터라도 다른 사건이다."""
    out: set[str] = set()
    for pat in _DATE_PATTERNS:
        for m in pat.finditer(text):
            g = m.groups()
            month, day = (g[-2], g[-1])
            out.add(f"{int(month)}-{int(day)}")
    return out


def quoted_names(text: str) -> set[str]:
    """따옴표로 감싼 고유명(제품명·행사명). 둘 다 있는데 겹치지 않으면 다른 사건이다."""
    return {m.group(1).strip().lower() for m in _QUOTED.finditer(text)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def same_event(a_title: str, b_title: str, aliases: dict[str, list[str]] | None = None) -> bool:
    ta, tb = normalize_tokens(a_title, aliases), normalize_tokens(b_title, aliases)
    if jaccard(ta, tb) < SAME_EVENT_JACCARD:
        return False
    da, db = date_tokens(a_title), date_tokens(b_title)
    if da and db and not (da & db):
        return False  # 날짜가 명시돼 있고 서로 다르다
    qa, qb = quoted_names(a_title), quoted_names(b_title)
    # 제품명·행사명이 둘 다 적혀 있는데 서로 다르면 다른 사건
    return not (qa and qb and not (qa & qb))


def fingerprint(tokens: set[str]) -> str:
    return " ".join(sorted(tokens))


@dataclass
class Cluster:
    """같은 사건으로 묶인 자료. topic은 대표 주제(persona_topic)."""

    topic: str
    items: list[tuple[SourceItem, float]] = field(default_factory=list)
    tokens: set[str] = field(default_factory=set)

    @property
    def best_relevance(self) -> float:
        return max((r for _, r in self.items), default=0.0)

    @property
    def published_at(self) -> datetime | None:
        dates = [i.published_at for i, _ in self.items if i.published_at is not None]
        return max(dates) if dates else None

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.tokens)

    def sorted_items(self) -> list[tuple[SourceItem, float]]:
        return sorted(self.items, key=lambda x: -x[1])


def cluster_candidates(
    candidates: list[tuple[SourceItem, float, str]], aliases: dict[str, list[str]] | None = None
) -> list[Cluster]:
    """(자료, 관련성, 대표 주제) → 사건 묶음. 같은 주제 안에서 제목이 같은 사건이면 합친다."""
    clusters: list[Cluster] = []
    for item, rel, topic in candidates:
        tokens = normalize_tokens(item.title, aliases)
        home = next(
            (
                c
                for c in clusters
                if c.topic == topic and same_event(c.items[0][0].title, item.title, aliases)
            ),
            None,
        )
        if home is None:
            clusters.append(Cluster(topic=topic, items=[(item, rel)], tokens=set(tokens)))
        else:
            home.items.append((item, rel))
            home.tokens |= tokens
    return clusters


def freshness(published_at: datetime | None, now: datetime, fresh_hours: int) -> Freshness:
    """공개 시각 기준. 수집 시각으로 대신하지 않는다: 모르면 unknown(날짜 확인 필요)."""
    if published_at is None:
        return "unknown"
    return "fresh" if now - published_at <= timedelta(hours=fresh_hours) else "stale"


_FRESH_SCORE = {"fresh": 2, "unknown": 1, "stale": 0}


def rank_clusters(
    clusters: list[Cluster],
    exclude: list[str],
    now: datetime,
    fresh_hours: int,
    aliases: dict[str, list[str]] | None = None,
) -> list[Cluster]:
    """추천 순서. 최근 소식 > 날짜 미상 > 오래된 것, 그다음 관련성·자료 수. 주제가 겹치지 않게 돌아가며 뽑는다.

    exclude: 최근에 이미 다룬 사건(원고 제목)과 '관심 없음'으로 버린 사건의 지문. 겹치면 뺀다.
    """
    ex_tokens = [normalize_tokens(x, aliases) for x in exclude]

    def dup(c: Cluster) -> bool:
        # 대표 주제 낱말(치이카와, 굿즈)은 같은 주제의 모든 제목에 있어 겹침을 부풀린다 → 빼고 비교(S11 리뷰)
        topic_tokens = normalize_tokens(c.topic, aliases)
        mine = c.tokens - topic_tokens
        return any(jaccard(mine, e - topic_tokens) >= SAME_EVENT_JACCARD for e in ex_tokens)

    kept = [c for c in clusters if not dup(c)]
    kept.sort(
        key=lambda c: (
            -_FRESH_SCORE[freshness(c.published_at, now, fresh_hours)],
            -c.best_relevance,
            -len(c.items),
        )
    )
    # 주제 다양성: 1라운드는 주제마다 하나씩, 그다음 남은 것
    ordered: list[Cluster] = []
    seen_topics: set[str] = set()
    rest: list[Cluster] = []
    for c in kept:
        if c.topic in seen_topics:
            rest.append(c)
        else:
            seen_topics.add(c.topic)
            ordered.append(c)
    return ordered + rest
