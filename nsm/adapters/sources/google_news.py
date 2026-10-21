"""구글 뉴스 RSS 검색(C30, ADR 0050): 관심 주제로 최신 기사 목록을 받는다. 브라우저 없이 HTTP로.

- `news.google.com/rss/search?q=<주제>`는 제목·매체·발행 시각만 준다(요약 없음). 링크는 구글을 거치는
  리다이렉트 주소(`/rss/articles/<id>`)라, 원문은 구글 내부 호출(batchexecute)로 실제 주소를 푼 뒤 읽는다.
- 주소 풀기는 요청 2번이 들어 **관련 자료로 판정된 것만** `fetch_body` 때 푼다(ADR 0022의 지연 읽기 그대로).
  판정은 제목·매체 기준으로 한다(한국 기사 제목은 내용을 충분히 담는다).
- 같은 보도자료를 여러 매체가 받아쓴 기사는 제목 낱말이 거의 같다. 한 번 수집 안에서 제목 낱말 겹침이
  큰 것은 하나만 남긴다(원고 자료가 한쪽으로 쏠리지 않게).
"""

from __future__ import annotations

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx

from nsm.adapters.sources.common import clean_text, url_hash
from nsm.domain.clock import new_id
from nsm.domain.models import SourceItem
from nsm.ports import Clock

log = logging.getLogger(__name__)

RSS_URL = "https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"
ARTICLE_URL = "https://news.google.com/articles/{gid}"
BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
UA = {"User-Agent": "Mozilla/5.0"}
TITLE_OVERLAP = 0.6  # 제목 낱말 자카드 겹침이 이 이상이면 같은 기사로 본다
_GID = re.compile(r"/articles/([^/?#]+)")
_TOKEN = re.compile(r"[가-힣a-zA-Z0-9]{2,}")
_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)


def _tokens(title: str) -> set[str]:
    return set(_TOKEN.findall(title.lower()))


def _similar(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= TITLE_OVERLAP


def _strip_source(title: str, source: str) -> str:
    """구글은 제목 끝에 " - 매체"를 붙인다. 매체 이름이 맞으면 뗀다."""
    suffix = f" - {source}"
    return title[: -len(suffix)] if source and title.endswith(suffix) else title


def _pub(item: ET.Element) -> datetime | None:
    raw = (item.findtext("pubDate") or "").strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_news_rss(
    xml_text: str, clock: Clock, limit: int, seen_titles: list[set[str]] | None = None
) -> list[SourceItem]:
    """RSS → SourceItem. seen_titles는 같은 수집 안에서 주제를 건너 중복을 거르기 위해 공유한다."""
    root = ET.fromstring(xml_text)
    seen = seen_titles if seen_titles is not None else []
    out: list[SourceItem] = []
    for item in root.iter("item"):
        if len(out) >= limit:
            break
        link = (item.findtext("link") or "").strip()
        m = _GID.search(link)
        source = (item.findtext("source") or "").strip()
        title = _strip_source((item.findtext("title") or "").strip(), source)
        if not (m and title):
            continue
        toks = _tokens(title)
        if any(_similar(toks, s) for s in seen):
            continue
        seen.append(toks)
        text = f"{title}" + (f" ({source})" if source else "")
        out.append(
            SourceItem(
                id=new_id(),
                source="google_news",
                url=link,
                title=title,
                text=text,
                published_at=_pub(item),
                collected_at=clock.now(),
                url_hash=url_hash(f"gnews:{m.group(1)}"),
            )
        )
    return out


def resolve_article_url(gnews_url: str, client: httpx.Client) -> str | None:
    """구글 뉴스 리다이렉트 주소 → 원문 주소. 기사 페이지의 서명(data-n-a-sg·ts)으로 batchexecute를 부른다.
    2024년부터 주소가 암호화돼 base64 풀기로는 안 된다. 실패하면 None(본문 없이 제목만 쓴다)."""
    m = _GID.search(gnews_url)
    if not m:
        return None
    gid = m.group(1)
    page = client.get(ARTICLE_URL.format(gid=gid), headers=UA)
    sig = re.search(r'data-n-a-sg="([^"]+)"', page.text)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page.text)
    if not (sig and ts):
        return None
    inner = (
        '["garturlreq",[["ko-KR","KR",["FINANCE_TOP_INDICES","WEB_TEST_1_0_0"],null,null,1,1,'
        '"KR:ko",null,180,null,null,null,null,null,0,null,null,[1608992183,723341000]],'
        '"ko-KR","KR",1,[2,3,4,8],1,0,"655000234",0,0,null,0],'
        f'"{gid}",{ts.group(1)},"{sig.group(1)}"]'
    )
    req = [[["Fbv4je", inner, None, "generic"]]]
    resp = client.post(
        BATCH_URL,
        data={"f.req": json.dumps(req)},
        headers={**UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
    )
    try:
        body = resp.text.split("\n\n", 1)[1]
        url = json.loads(json.loads(body)[0][2])[1]
    except (IndexError, ValueError, TypeError, KeyError):
        return None
    return url if isinstance(url, str) and url.startswith("http") else None


def html_to_text(html: str) -> str:
    """브라우저 없이 읽을 때의 거친 본문 추출(스크립트·태그 제거 → 정리)."""
    return clean_text(_TAG.sub("\n", _SCRIPT.sub(" ", html)))


class GoogleNewsSource:
    name = "google_news"

    def __init__(
        self,
        clock: Clock,
        fetch: Callable[[str], str] | None = None,
        body_fetcher: Callable[[str], str | None] | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.clock = clock
        self.client = client or httpx.Client(timeout=20.0, follow_redirects=True)
        self._fetch = fetch or self._http_get
        self._body = body_fetcher  # 브라우저 읽기(web.fetch_article)가 있으면 그것을, 없으면 HTTP
        self._sleep = sleep

    def _http_get(self, url: str) -> str:
        r = self.client.get(url, headers=UA)
        r.raise_for_status()
        return r.text

    def collect(self, topics: list[str], limit: int) -> list[SourceItem]:
        items: list[SourceItem] = []
        seen: list[set[str]] = []
        per_topic = max(1, limit // max(1, len(topics)))
        for topic in topics:
            if len(items) >= limit:
                break
            try:
                xml = self._fetch(RSS_URL.format(query=quote(topic)))
                items += parse_news_rss(xml, self.clock, min(per_topic, limit - len(items)), seen)
            except Exception as e:  # noqa: BLE001  한 주제 실패가 다른 주제를 막지 않게
                log.warning("구글 뉴스 수집 실패(%s): %s", topic, e)
            self._sleep(1.0)
        return items

    def fetch_body(self, url: str) -> str | None:
        """관련 자료만 원문을 읽는다: 주소 풀기(요청 2번) → 브라우저 또는 HTTP로 본문."""
        try:
            real = resolve_article_url(url, self.client)
        except Exception as e:  # noqa: BLE001
            log.warning("구글 뉴스 원문 주소 풀기 실패: %s", e)
            return None
        if not real:
            return None
        if self._body is not None:
            return self._body(real)
        try:
            r = self.client.get(real, headers=UA)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            log.warning("구글 뉴스 원문 읽기 실패: %s", e)
            return None
        text = html_to_text(r.text)
        return text if len(text) >= 200 else None
