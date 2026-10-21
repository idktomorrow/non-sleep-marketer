"""구글 뉴스 RSS 검색(C30, ADR 0050): 관심 주제로 최신 기사 목록을 받는다. 브라우저 없이 HTTP로.

- `news.google.com/rss/search?q=<주제> when:7d`는 제목·매체·발행 시각만 준다(요약 없음). 링크는 구글을 거치는
  리다이렉트 주소(`/rss/articles/<id>`)라, 원문은 구글 내부 호출(batchexecute)로 실제 주소를 푼 뒤 읽는다.
- 주소 풀기는 요청 2번이 들어 **관련 자료로 판정된 것만** `fetch_body` 때 푼다(ADR 0022의 지연 읽기 그대로).
  판정은 제목·매체 기준으로 한다(한국 기사 제목은 내용을 충분히 담는다).
- 같은 보도자료를 여러 매체가 받아쓴 기사는 제목 낱말이 거의 같다. 해시를 제목 낱말 집합으로 만들어
  실행을 건너서도 한 번만 저장하고, 한 수집 안에서는 낱말 겹침이 큰 것도 하나만 남긴다.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx

from nsm.adapters.sources.common import Pacer, clean_text, url_hash
from nsm.adapters.sources.web import BODY_MAX, BODY_MIN
from nsm.domain.clock import new_id
from nsm.domain.models import SourceItem
from nsm.ports import Clock

log = logging.getLogger(__name__)

RSS_URL = "https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"
ARTICLE_URL = "https://news.google.com/articles/{gid}"
BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
UA = {"User-Agent": "Mozilla/5.0"}
TITLE_OVERLAP = 0.6  # 제목 낱말 자카드 겹침이 이 이상이면 같은 기사로 본다(낱말 4개 이상일 때)
# 네이버 검색 기간(ADR 0032)과 같은 뜻의 구글 검색 연산자와 허용 나이
RECENT = {"1d": ("when:1d", 1), "1w": ("when:7d", 7), "1m": ("when:30d", 31)}
_GID = re.compile(r"/articles/([^/?#]+)")
_TOKEN = re.compile(r"[가-힣a-zA-Z0-9]{2,}")
_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style|noscript|nav|header|footer|aside)[^>]*>.*?</\1>", re.S | re.I)
_ARTICLE = re.compile(r"<article[^>]*>(.*?)</article>", re.S | re.I)
_P = re.compile(r"<p[^>]*>(.*?)</p>", re.S | re.I)
_META_CHARSET = re.compile(rb'charset=["\']?([A-Za-z0-9_-]+)', re.I)


def _tokens(title: str) -> set[str]:
    return set(_TOKEN.findall(title.lower()))


def _similar(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return False
    if min(len(a), len(b)) < 4:  # 짧은 제목은 낱말 하나 차이가 다른 소식일 수 있어 똑같을 때만
        return a == b
    return len(a & b) / len(a | b) >= TITLE_OVERLAP


def _strip_source(title: str, source: str) -> str:
    """구글은 제목 끝에 " - 매체"를 붙인다. 매체 이름이 맞으면 떼고, 매체 칸이 비었으면 짧은 꼬리를 뗀다."""
    if source and title.endswith(f" - {source}"):
        return title[: -len(source) - 3]
    head, sep, tail = title.rpartition(" - ")
    if sep and 0 < len(tail) <= 25 and head:
        return head
    return title


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
    xml_text: str,
    clock: Clock,
    limit: int,
    seen_titles: list[set[str]] | None = None,
    max_age_days: int | None = None,
) -> list[SourceItem]:
    """RSS → SourceItem. seen_titles는 같은 수집 안에서 주제를 건너 중복을 거르기 위해 공유한다.
    max_age_days가 있으면 그보다 오래된 기사(발행 시각 기준)는 버린다(검색 연산자가 안 먹었을 때 대비)."""
    root = ET.fromstring(xml_text)
    seen = seen_titles if seen_titles is not None else []
    cutoff = clock.now() - timedelta(days=max_age_days) if max_age_days else None
    out: list[SourceItem] = []
    for item in root.iter("item"):
        if len(out) >= limit:
            break
        link = (item.findtext("link") or "").strip()
        m = _GID.search(link)
        source = (item.findtext("source") or "").strip()
        title = _strip_source(html.unescape((item.findtext("title") or "").strip()), source)
        if not (m and title):
            continue
        pub = _pub(item)
        if cutoff and pub and pub < cutoff:
            continue
        toks = _tokens(title)
        if any(_similar(toks, s) for s in seen):
            continue
        seen.append(toks)
        out.append(
            SourceItem(
                id=new_id(),
                source="google_news",
                url=link,
                title=title,
                text=f"{title}" + (f" ({source})" if source else ""),
                published_at=pub,
                collected_at=clock.now(),
                # 같은 보도자료의 다른 매체 사본은 실행을 건너서도 한 번만(낱말 집합이 같으면 같은 글)
                url_hash=url_hash("gnews:" + " ".join(sorted(toks))),
            )
        )
    return out


def resolve_article_url(
    gnews_url: str, client: httpx.Client, pace: Callable[[], None] | None = None
) -> str | None:
    """구글 뉴스 리다이렉트 주소 → 원문 주소. 기사 페이지의 서명(data-n-a-sg·ts)으로 batchexecute를 부른다.
    2024년부터 주소가 암호화돼 base64 풀기로는 안 된다. 실패하면 이유를 로그에 남기고 None(제목만 쓴다)."""
    m = _GID.search(gnews_url)
    if not m:
        return None
    gid = m.group(1)
    if pace:
        pace()
    page = client.get(ARTICLE_URL.format(gid=gid), headers=UA)
    if page.status_code != 200:
        log.warning("구글 뉴스 원문 주소 풀기 실패: 기사 페이지 HTTP %s", page.status_code)
        return None
    sig = re.search(r'data-n-a-sg="([^"]+)"', page.text)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page.text)
    if not (sig and ts):
        log.warning("구글 뉴스 원문 주소 풀기 실패: 페이지에 서명이 없음(형식 변경 또는 동의 화면)")
        return None
    inner = (
        '["garturlreq",[["ko-KR","KR",["FINANCE_TOP_INDICES","WEB_TEST_1_0_0"],null,null,1,1,'
        '"KR:ko",null,180,null,null,null,null,null,0,null,null,[1608992183,723341000]],'
        '"ko-KR","KR",1,[2,3,4,8],1,0,"655000234",0,0,null,0],'
        f'"{gid}",{ts.group(1)},"{sig.group(1)}"]'
    )
    req = [[["Fbv4je", inner, None, "generic"]]]
    if pace:
        pace()
    resp = client.post(
        BATCH_URL,
        data={"f.req": json.dumps(req)},
        headers={**UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
    )
    if resp.status_code != 200:
        log.warning("구글 뉴스 원문 주소 풀기 실패: batchexecute HTTP %s", resp.status_code)
        return None
    try:
        body = resp.text.split("\n\n", 1)[1]
        url = json.loads(json.loads(body)[0][2])[1]
    except (IndexError, ValueError, TypeError, KeyError):
        log.warning("구글 뉴스 원문 주소 풀기 실패: 응답 형식이 다름")
        return None
    if not (isinstance(url, str) and url.startswith("http")):
        log.warning("구글 뉴스 원문 주소 풀기 실패: 주소가 아님")
        return None
    return url


def decode_html(content: bytes, header_charset: str | None) -> str:
    """헤더에 charset이 없는 EUC-KR 사이트를 깨지지 않게 읽는다(meta charset → utf-8 → cp949)."""
    candidates = []
    if header_charset:
        candidates.append(header_charset)
    m = _META_CHARSET.search(content[:4096])
    if m:
        candidates.append(m.group(1).decode("ascii", "ignore"))
    candidates += ["utf-8", "cp949"]
    for enc in candidates:
        try:
            return content.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode("utf-8", "replace")


def html_to_text(page: str) -> str:
    """브라우저 없이 읽을 때의 본문 추출: <article>이 있으면 그 안, 아니면 <p> 문단만(메뉴·푸터 제외)."""
    page = _SCRIPT.sub(" ", page)
    m = _ARTICLE.search(page)
    scope = m.group(1) if m else page
    paras = [_TAG.sub(" ", p) for p in _P.findall(scope)]
    raw = "\n".join(paras) if paras else _TAG.sub("\n", scope)
    return clean_text(html.unescape(raw), BODY_MAX)


class GoogleNewsSource:
    name = "google_news"

    def __init__(
        self,
        clock: Clock,
        fetch: Callable[[str], str] | None = None,
        body_fetcher: Callable[[str], str | None] | None = None,
        client: httpx.Client | None = None,
        pacer: Pacer | None = None,
        recent: str = "1w",
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.clock = clock
        self._client = client
        self._fetch = fetch or self._http_get
        self._body = body_fetcher  # 브라우저 읽기(web.fetch_article)가 있으면 그것을, 없으면 HTTP
        self._pacer = pacer
        self.recent = recent
        self._sleep = sleep

    @property
    def client(self) -> httpx.Client:
        if self._client is None:  # 실제로 쓸 때만 연다(테스트에서 fetch만 주입하면 열지 않음)
            self._client = httpx.Client(timeout=20.0, follow_redirects=True)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _pace(self) -> None:
        if self._pacer is not None:
            self._pacer.wait()
        else:
            self._sleep(1.0)

    def _http_get(self, url: str) -> str:
        r = self.client.get(url, headers=UA)
        r.raise_for_status()
        return r.text

    def collect(self, topics: list[str], limit: int) -> list[SourceItem]:
        items: list[SourceItem] = []
        seen: list[set[str]] = []
        per_topic = max(1, limit // max(1, len(topics)))
        op, max_age = RECENT.get(self.recent, ("", None))
        for i, topic in enumerate(topics):
            if len(items) >= limit:
                break
            if i:
                self._pace()
            q = f"{topic} {op}".strip()
            try:
                xml = self._fetch(RSS_URL.format(query=quote(q)))
                items += parse_news_rss(
                    xml, self.clock, min(per_topic, limit - len(items)), seen, max_age
                )
            except Exception as e:  # noqa: BLE001  한 주제 실패가 다른 주제를 막지 않게
                log.warning("구글 뉴스 수집 실패(%s): %s", topic, e)
        return items

    def fetch_body(self, url: str) -> str | None:
        """관련 자료만 원문을 읽는다: 주소 풀기(요청 2번, 간격 둠) → 브라우저 또는 HTTP로 본문."""
        try:
            real = resolve_article_url(url, self.client, pace=self._pace)
        except Exception as e:  # noqa: BLE001
            log.warning("구글 뉴스 원문 주소 풀기 실패: %s", e)
            return None
        if not real:
            return None
        if self._body is not None:
            return self._body(real)
        self._pace()
        try:
            r = self.client.get(real, headers=UA)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            log.info("구글 뉴스 원문 읽기 실패(%s): %s", real, e)
            return None
        text = html_to_text(decode_html(r.content, r.charset_encoding))
        return text if len(text) >= BODY_MIN else None
