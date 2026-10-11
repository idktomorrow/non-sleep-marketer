"""구글 트렌드 한국 급상승 검색어(공개 RSS). 브라우저 없이 HTTP 한 번으로 읽는다."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from collections.abc import Callable
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from nsm.adapters.sources.common import url_hash
from nsm.domain.clock import new_id
from nsm.domain.models import SourceItem
from nsm.ports import Clock

log = logging.getLogger(__name__)

RSS_URL = "https://trends.google.com/trending/rss?geo=KR"
HT = "{https://trends.google.com/trending/rss}"


def parse_trends_rss(
    xml_text: str, clock: Clock, limit: int, tz: str = "Asia/Seoul"
) -> list[SourceItem]:
    root = ET.fromstring(xml_text)
    out: list[SourceItem] = []
    for item in root.iter("item"):
        if len(out) >= limit:
            break
        keyword = (item.findtext("title") or "").strip()
        if not keyword:
            continue
        traffic = (item.findtext(f"{HT}approx_traffic") or "").strip()
        news = []
        first_url = None
        for n in item.findall(f"{HT}news_item"):
            title = (n.findtext(f"{HT}news_item_title") or "").strip()
            src = (n.findtext(f"{HT}news_item_source") or "").strip()
            url = (n.findtext(f"{HT}news_item_url") or "").strip()
            if title:
                news.append(f"{title} ({src})" if src else title)
            first_url = first_url or url or None
        text = f"급상승 검색어: {keyword} (검색량 {traffic or '알 수 없음'})."
        if news:
            text += " 관련 뉴스: " + "; ".join(news[:3])
        # 같은 날 같은 키워드는 한 번만: 날짜를 해시에 넣는다
        day = clock.now().astimezone(ZoneInfo(tz)).date().isoformat()
        out.append(
            SourceItem(
                id=new_id(),
                source="google_trends",
                url=first_url or f"{RSS_URL}#{quote(keyword)}",
                title=keyword,
                text=text,
                collected_at=clock.now(),
                url_hash=url_hash(f"{RSS_URL}#{keyword}@{day}"),
            )
        )
    return out


class GoogleTrendsSource:
    name = "google_trends"

    def __init__(
        self,
        clock: Clock,
        fetch: Callable[[str], str] | None = None,
        tz: str = "Asia/Seoul",
    ) -> None:
        self.clock = clock
        self.tz = tz
        self._fetch = fetch or self._http_get

    @staticmethod
    def _http_get(url: str) -> str:
        r = httpx.get(url, timeout=20.0, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return r.text

    def collect(self, topics: list[str], limit: int) -> list[SourceItem]:
        try:
            return parse_trends_rss(self._fetch(RSS_URL), self.clock, limit, self.tz)
        except Exception as e:  # noqa: BLE001
            log.warning("구글 트렌드 수집 실패: %s", e)
            return []
