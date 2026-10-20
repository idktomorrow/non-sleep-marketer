"""브라우저로 읽는 웹 수집 소스: 네이버 검색, 커뮤니티 검색.

검색 결과 화면에서는 링크와 요약문만 읽는다. 관련 자료로 판정된 글만 `fetch_body`로 원문을 열어
본문을 상한(BODY_MAX)까지 가져온다(ADR 0022). 본문은 생성 재료로만 쓰고 그대로 옮기지 않는다
(생성 규칙: 20자 넘게 그대로 옮기지 않음). 요청 사이에 무작위 지연을 두고, 소스당 개수 상한을 지킨다.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from urllib.parse import quote, urlsplit

from nsm.adapters.drivers.base import BrowserDriver, Link
from nsm.adapters.sources.common import A11Y_NOISE, Pacer, clean_text, normalize_url, url_hash
from nsm.domain.clock import new_id
from nsm.domain.models import SourceItem
from nsm.ports import Clock

log = logging.getLogger(__name__)

BODY_MAX = 3000  # 원문 본문 상한(글자). 생성 프롬프트도 자료당 3000자까지만 쓴다.
BODY_MIN = 200  # 이보다 짧으면 본문을 못 찾은 것으로 보고 요약문을 그대로 둔다

NAVER_TABS = {
    "blog": (
        "https://search.naver.com/search.naver?where=blog&query={q}{recent}",
        re.compile(r"^https?://(m\.)?blog\.naver\.com/[\w-]+/\d+"),
    ),
    # 2026-10 네이버 뉴스 탭: 기사 제목 링크는 언론사 주소이고, n.news.naver.com 링크의 글자는
    # "네이버뉴스"뿐이라(8자 미만) 걸러졌다. 그래서 네이버가 아닌 주소의 제목 링크도 받는다.
    "news": (
        "https://search.naver.com/search.naver?where=news&query={q}{recent}",
        re.compile(
            r"^https?://(?:n\.news\.naver\.com/.+/article/"
            r"|(?![\w.-]*naver(?:corp)?\.(?:com|me)\b)[\w.-]+\.[a-z]{2,}(?::\d+)?/.+)"
        ),
    ),
}


# 최신순(so:dd) + 최근 기간(p:1d/1w/1m). 기본 정확도순은 몇 주 전 글(예: 개봉 전 "출시 예정" 글)이
# 위에 와서 이미 지난 일을 앞으로의 일처럼 쓰는 원고가 나왔다(2026-10-13, ADR 0032).
RECENT_PARAMS = {
    "1d": "&sm=tab_opt&sort=1&nso=so%3Add%2Cp%3A1d",
    "1w": "&sm=tab_opt&sort=1&nso=so%3Add%2Cp%3A1w",
    "1m": "&sm=tab_opt&sort=1&nso=so%3Add%2Cp%3A1m",
    "all": "",
}


# 네이버 검색 결과의 날짜 표기("3시간 전", "어제", "2026.10.19."). 공개 시각으로 쓴다(S11: 최신성 판단).
_REL_DATE = re.compile(r"(\d{1,3})\s*(분|시간|일|주)\s*전")
_ABS_DATE = re.compile(r"(20\d{2})\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{1,2})")


def parse_naver_date(text: str, now: datetime) -> datetime | None:
    """요약문에 섞인 날짜 표기 → 공개 시각(aware, now와 같은 시간대). 못 읽으면 None(수집 시각으로 대신하지 않는다)."""
    if not text:
        return None
    m = _REL_DATE.search(text)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {
            "분": timedelta(minutes=n),
            "시간": timedelta(hours=n),
            "일": timedelta(days=n),
            "주": timedelta(weeks=n),
        }
        return now - delta[unit]
    if "어제" in text:
        return now - timedelta(days=1)
    m = _ABS_DATE.search(text)
    if m:
        try:
            return now.replace(
                year=int(m.group(1)),
                month=int(m.group(2)),
                day=int(m.group(3)),
                hour=12,
                minute=0,
                second=0,
                microsecond=0,
            )
        except ValueError:
            return None
    return None


def _items_from_links(
    links: list[Link],
    pattern: re.Pattern[str],
    source: str,
    clock: Clock,
    limit: int,
    seen: set[str],
) -> list[SourceItem]:
    out: list[SourceItem] = []
    now = clock.now()
    for link in links:
        if len(out) >= limit:
            break
        title = " ".join(A11Y_NOISE.sub(" ", link.text).split())
        if len(title) < 8 or not pattern.match(link.href):
            continue
        key = normalize_url(link.href)
        if key in seen:
            continue
        seen.add(key)
        context = clean_text(link.context, 400)
        out.append(
            SourceItem(
                id=new_id(),
                source=source,
                url=link.href,
                title=title[:200],
                text=context if title in context else f"{title}. {context}",
                published_at=parse_naver_date(link.context, now),
                collected_at=now,
                url_hash=url_hash(link.href),
            )
        )
    return out


def body_url(url: str) -> str:
    """네이버 블로그 PC 주소는 본문이 iframe 안에 있다. 모바일 주소는 본문이 바로 보인다."""
    parts = urlsplit(url)
    if parts.netloc.lower() == "blog.naver.com":
        return url.replace("://blog.naver.com", "://m.blog.naver.com", 1)
    return url


def fetch_article(driver: BrowserDriver, pacer: Pacer, url: str) -> str | None:
    """원문 본문(정리 + 상한). 못 찾거나 너무 짧으면 None."""
    pacer.wait()
    try:
        driver.goto(body_url(url))
        reader = getattr(driver, "article_text", None)
        raw = reader() if callable(reader) else driver.page_text()
    except Exception as e:  # noqa: BLE001  본문 실패는 요약문으로 대신한다
        log.info("본문 가져오기 실패(%s): %s", url, e)
        return None
    text = clean_text(raw, BODY_MAX)
    return text if len(text) >= BODY_MIN else None


class NaverSearchSource:
    name = "naver"

    def __init__(
        self,
        driver: BrowserDriver,
        clock: Clock,
        pacer: Pacer,
        tabs: tuple[str, ...] = ("blog", "news"),
        recent: str = "1w",
    ) -> None:
        self.driver = driver
        self.clock = clock
        self.pacer = pacer
        self.tabs = tabs
        self.recent = RECENT_PARAMS.get(recent, RECENT_PARAMS["1w"])

    def collect(self, topics: list[str], limit: int) -> list[SourceItem]:
        items: list[SourceItem] = []
        seen: set[str] = set()
        per_query = max(1, limit // max(1, len(topics) * len(self.tabs)))
        for topic in topics:
            for tab in self.tabs:
                if len(items) >= limit:
                    return items
                url_t, pattern = NAVER_TABS[tab]
                self.pacer.wait()
                try:
                    self.driver.goto(url_t.format(q=quote(topic), recent=self.recent))
                    links = self.driver.links()
                except Exception as e:  # noqa: BLE001
                    log.warning("네이버 %s 검색 실패(%s): %s", tab, topic, e)
                    continue
                items += _items_from_links(
                    links, pattern, self.name, self.clock, min(per_query, limit - len(items)), seen
                )
        return items

    def fetch_body(self, url: str) -> str | None:
        return fetch_article(self.driver, self.pacer, url)


class CommunitySource:
    """설정의 검색 URL 템플릿({query} 자리표시자)으로 커뮤니티 검색 결과를 읽는다."""

    def __init__(self, search_url: str, driver: BrowserDriver, clock: Clock, pacer: Pacer) -> None:
        if "{query}" not in search_url:
            raise ValueError("커뮤니티 검색 URL에는 {query} 자리표시자가 필요합니다")
        self.search_url = search_url
        host = urlsplit(search_url.replace("{query}", "x")).netloc
        self.name = f"community:{host}"
        # 게시글 주소에는 보통 숫자 ID가 있다. 메뉴·게시판 목록·로그인 링크를 거른다.
        self._pattern = re.compile(rf"^https?://{re.escape(host)}/\S*\d{{3,}}")
        self.driver = driver
        self.clock = clock
        self.pacer = pacer

    def collect(self, topics: list[str], limit: int) -> list[SourceItem]:
        items: list[SourceItem] = []
        seen: set[str] = set()
        per_topic = max(1, limit // max(1, len(topics)))
        for topic in topics:
            if len(items) >= limit:
                break
            self.pacer.wait()
            try:
                self.driver.goto(self.search_url.replace("{query}", quote(topic)))
                links = self.driver.links()
            except Exception as e:  # noqa: BLE001
                log.warning("%s 검색 실패(%s): %s", self.name, topic, e)
                continue
            items += _items_from_links(
                links,
                self._pattern,
                self.name,
                self.clock,
                min(per_topic, limit - len(items)),
                seen,
            )
        return items

    def fetch_body(self, url: str) -> str | None:
        return fetch_article(self.driver, self.pacer, url)
