"""밈 탐색 수집기(S16 제안, ADR 0060): 관심 주제의 실제 밈 사례를 공개 검색 결과에서 찾는다.

- 쓰는 것: 네이버 블로그·카페 검색(로그인 없이 보이는 결과 목록), 설정의 커뮤니티 검색 URL(`community_urls`).
- X(트위터)는 로그인·API 없이 검색할 수 없어 **직접 수집하지 않는다**(우회 금지). 위 결과에 노출된 x.com 링크는
  "발견 경로"로만 기록한다(원문 미확인).
- 검색 결과 목록의 제목·요약만 본 것은 `body_checked=False`로 남긴다(원문 전체 확인으로 표시하지 않는다).
- 반응 수치(좋아요·조회)는 공개 결과에 안정적으로 나오지 않아 수집하지 않는다 → None(미확인).
- 출처 하나가 막혀도 다른 출처와 창작 경로는 계속 돈다. 출처별 상태를 돌려준다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

from nsm.adapters.sources.common import A11Y_NOISE, Pacer, clean_text, normalize_url
from nsm.adapters.sources.web import parse_naver_date
from nsm.domain.meme_discovery import Sighting, author_of, platform_of
from nsm.ports import Clock

log = logging.getLogger(__name__)

MEME_QUERIES = ("{topic} 밈", "{topic} 짤")
NAVER = {
    "네이버 블로그": (
        "https://search.naver.com/search.naver?where=blog&query={q}&sm=tab_opt&nso=so%3Add%2Cp%3A1m",
        re.compile(r"^https?://(m\.)?blog\.naver\.com/[\w-]+/\d+"),
    ),
    "네이버 카페": (
        "https://search.naver.com/search.naver?where=article&query={q}&sm=tab_opt&nso=so%3Add%2Cp%3A1m",
        re.compile(r"^https?://(m\.)?cafe\.naver\.com/[\w-]+/\d+"),
    ),
}
X_LINK = re.compile(r"^https?://(?:www\.)?(?:x|twitter)\.com/[\w]+/status/\d+")


@dataclass
class DiscoveryResult:
    sightings: list[Sighting] = field(default_factory=list)
    access: dict[str, str] = field(
        default_factory=dict
    )  # 출처 → "ok n건" | "실패: …" | "수집 안 함: …"


class MemeDiscovery:
    def __init__(
        self, driver, clock: Clock, pacer: Pacer, community_urls: tuple[str, ...] = ()
    ) -> None:
        self.driver = driver
        self.clock = clock
        self.pacer = pacer
        self.community_urls = community_urls

    def discover(self, topic: str, limit: int = 30) -> DiscoveryResult:
        res = DiscoveryResult()
        res.access["X"] = (
            "수집 안 함: 로그인·API 없이 검색할 수 없음(검색 결과에 보인 X 링크만 발견 경로로 기록)"
        )
        seen: set[str] = set()
        now = self.clock.now()
        targets = [(name, url, pat) for name, (url, pat) in NAVER.items()]
        for u in self.community_urls:
            host = urlsplit(u.replace("{query}", "x")).netloc
            targets.append(
                (
                    f"커뮤니티 {host}",
                    u.replace("{query}", "{q}"),
                    re.compile(rf"^https?://{re.escape(host)}/\S*\d{{3,}}"),
                )
            )
        # 출처마다 몫을 나눠 첫 출처가 상한을 다 쓰지 않게, 상한에 닿으면 남은 요청은 하지 않는다(S16 리뷰)
        quota = max(5, limit // max(1, len(targets)))
        for name, url_t, pattern in targets:
            got = 0
            err = ""
            if len(res.sightings) >= limit:
                res.access[name] = "건너뜀: 사례 상한에 닿음"
                continue
            for qt in MEME_QUERIES:
                if got >= quota or len(res.sightings) >= limit:
                    break
                q = qt.format(topic=topic)
                self.pacer.wait()
                try:
                    self.driver.goto(url_t.format(q=quote(q)))
                    links = self.driver.links()
                except Exception as e:  # noqa: BLE001  출처 하나가 막혀도 계속
                    err = f"{type(e).__name__}: {str(e)[:80]}"
                    log.warning("밈 탐색 %s 실패(%s): %s", name, q, e)
                    continue
                for link in links:
                    if got >= quota or len(res.sightings) >= limit:
                        break
                    href = link.href
                    is_x = bool(X_LINK.match(href))
                    if not (pattern.match(href) or is_x):
                        continue
                    key = normalize_url(href)
                    if key in seen:
                        continue
                    title = " ".join(A11Y_NOISE.sub(" ", link.text).split())
                    if len(title) < 4 and not is_x:
                        continue
                    seen.add(key)
                    res.sightings.append(
                        Sighting(
                            url=href,
                            title=title[:120] or href,
                            snippet=clean_text(link.context, 240),
                            platform=platform_of(href),
                            posted_at=parse_naver_date(link.context, now),
                            observed_at=now,
                            author=author_of(href),
                        )
                    )
                    got += 1
            res.access[name] = f"ok {got}건" if got or not err else f"실패: {err}"
        return res
