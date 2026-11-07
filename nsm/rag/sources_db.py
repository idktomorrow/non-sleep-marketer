"""DB에서 읽는 문서 출처(R4, ADR 0065): 수집 자료와 우리가 올린 글. 둘 다 `DocumentSource` 모양이라 색인·검색은 그대로다.

- 읽기 전용 SELECT만 한다. 스키마는 바꾸지 않는다.
- 본문은 마크다운으로 만든다. 맨 위 `# 제목` 아래에 출처·날짜·주소 줄을 두고, 기존 `chunk_markdown`이 제목 구조로 자른다.
  제목 경로는 `Document.label`("수집 자료"·"우리 글")로 시작해 "수집 자료 > <제목>"처럼 보인다(경로는 id로 유일).
- 원문 안의 `# ` 줄은 제목으로 읽히지 않게 앞에 `\\`를 붙인다(조각 경계와 제목 경로가 원문에 흔들리지 않게).
- DB에서 사라지거나 기준 밖으로 밀린 자료는 문서 목록에서 빠지고, 색인이 그 조각을 지운다. 0건도 정상이라
  `allow_empty`를 둔다(docs 폴더처럼 "경로 실수"로 보고 멈추지 않는다).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from nsm.rag.chunker import FENCE, HEADING
from nsm.rag.ports import Document
from nsm.store.db import Database, from_db, to_db

COLLECTED_LABEL = "수집 자료"
POSTS_LABEL = "우리 글"
_TITLE_MAX = 80
_SENTENCE_END = re.compile(r"(?<=[.!?。])\s|\n")


def one_line(text: str | None, limit: int = _TITLE_MAX) -> str:
    """제목 한 줄: 줄바꿈·연속 공백을 한 칸으로, 길면 자른다. `#`로 시작하면 떼어 낸다(제목 안 제목 방지)."""
    t = " ".join((text or "").split()).lstrip("#").strip()
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def escape_headings(text: str) -> str:
    """남이 쓴 원문의 `# ` 제목 줄과 코드 울타리(```)를 글자로: 닫히지 않은 울타리 하나가 뒤 절(카드 문구·댓글)까지
    한 덩어리로 삼켜 조각이 임베딩 입력 한도를 넘지 않게(R4 리뷰)."""
    return "\n".join(
        "\\" + ln if HEADING.match(ln) or FENCE.match(ln) else ln
        for ln in (text or "").splitlines()
    )


_HANDLE = re.compile(r"@[A-Za-z0-9_.]+")


def scrub_people(text: str | None, author: str | None) -> str:
    """댓글·답글 속 사람 표시를 지운다: `@핸들` 언급과 그 댓글 작성자 이름(답글이 "OO님 감사해요"처럼 부를 때).
    질문 화면·Claude 프롬프트·질문 기록에 남의 이름이 퍼지지 않게(ADR 0065)."""
    t = _HANDLE.sub("@…", text or "")
    name = (author or "").strip()
    if len(name) >= 2:
        t = t.replace(name, "…")
    return t


def _day(dt: datetime | None, tz: ZoneInfo) -> str:
    d = from_db(dt)
    return d.astimezone(tz).date().isoformat() if d else "날짜 없음"


class CollectedSource:
    """관련성 기준 이상이고 최근 N일 안에 모은, 본문이 있는 수집 자료. 경로 "source_items/<id>"."""

    kind = "collected"
    allow_empty = True

    def __init__(
        self, db: Database, *, min_relevance: float, days: int, now: datetime, tz: str
    ) -> None:
        self.db, self.min_relevance, self.days, self.now = db, min_relevance, days, now
        self.tz = ZoneInfo(tz)

    def documents(self) -> list[Document]:
        since = to_db(self.now - timedelta(days=self.days))
        rows = self.db.execute(
            "SELECT id, source, url, title, text, collected_at, topic FROM source_items "
            "WHERE relevance >= ? AND collected_at >= ? AND trim(text) <> '' ORDER BY id",
            [self.min_relevance, since],
        )
        return [
            Document(f"source_items/{i}", self._markdown(*rest), label=COLLECTED_LABEL)
            for i, *rest in rows
        ]

    def _markdown(
        self, source: str, url: str | None, title: str, text: str, collected: Any, topic: Any
    ) -> str:
        head = [
            f"# {one_line(title) or '제목 없음'}",
            f"출처: {source} {url or ''}".rstrip(),
            f"수집: {_day(collected, self.tz)}",
            f"주제: {topic or '없음'}",
        ]
        return "\n".join(head) + "\n\n" + escape_headings(text.strip()) + "\n"


# 플랫폼마다 올린 글 본문이 든 GeneratedContent 키(nsm/domain/models.py)
_BODY_KEY = {
    "instagram": "instagram_caption",
    "threads": "threads_text",
    "naver_blog": "blog_markdown",
}


def card_lines(deck: dict[str, Any]) -> list[str]:
    """카드 문구를 줄 목록으로(슬라이드 순서). 배경 이미지 경로·출처 주소 같은 비글자 값은 뺀다."""
    out: list[str] = []
    for s in deck.get("slides") or []:
        kind = s.get("kind")
        if kind == "cover":
            out += [one_line(s.get("title"), 200), one_line(s.get("subtitle"), 200)]
        elif kind in ("body", "list"):
            out += [one_line(s.get("heading"), 200), one_line(s.get("body"), 400)]
            out += [one_line(b, 200) for b in s.get("bullets") or []]
        elif kind == "compare":
            out.append(one_line(s.get("heading"), 200))
            for side in (s.get("left") or {}, s.get("right") or {}):
                pts = ", ".join(one_line(p, 200) for p in side.get("points") or [])
                out.append(f"{one_line(side.get('title'), 100)}: {pts}")
        elif kind == "outro":
            out.append(one_line(s.get("cta"), 200))
    return [x for x in out if x]


def post_title(gen: dict[str, Any], body: str) -> str:
    """글 제목: 블로그 제목, 없으면 카드 표지 제목, 없으면 본문 첫 문장."""
    slides = (gen.get("deck") or {}).get("slides") or []
    cover = next((s.get("title") for s in slides if s.get("kind") == "cover"), "")
    first = _SENTENCE_END.split(body.strip(), maxsplit=1)[0] if body.strip() else ""
    return one_line(gen.get("blog_title") or cover or first) or "제목 없음"


class PostsSource:
    """게시된 글(publications.status='published')과 그 글의 댓글·답글. 경로 "posts/<content_id>/<platform>".

    댓글 작성자 이름·id는 넣지 않는다(검색 결과에 남의 이름이 뜨지 않게, ADR 0065).
    """

    kind = "posts"
    allow_empty = True

    def __init__(self, db: Database, *, tz: str) -> None:
        self.db = db
        self.tz = ZoneInfo(tz)

    def documents(self) -> list[Document]:
        rows = self.db.execute(
            "SELECT p.content_id, p.platform, p.published_at, p.url, p.external_id, c.topic, c.generated "
            "FROM publications p JOIN contents c ON c.id = p.content_id "
            "WHERE p.status = 'published' ORDER BY p.content_id, p.platform"
        )
        comments: dict[tuple[str, str], list[tuple[str, str | None]]] = {}
        for platform, post, text, reply, author in self.db.execute(
            "SELECT platform, post_external_id, text, reply_text, author_name FROM comments "
            "WHERE status <> 'hidden' ORDER BY created_at, id"  # 숨긴 댓글(악성·스팸)은 싣지 않는다
        ):
            # 작성자 이름은 지우는 데만 쓰고 싣지 않는다
            comments.setdefault((platform, post), []).append(
                (scrub_people(text, author), scrub_people(reply, author) if reply else None)
            )
        out = []
        for cid, platform, published, url, ext, topic, generated in rows:
            md = self._markdown(
                platform, published, url, topic, generated, comments.get((platform, ext), [])
            )
            out.append(Document(f"posts/{cid}/{platform}", md, label=POSTS_LABEL))
        return out

    def _markdown(
        self,
        platform: str,
        published: Any,
        url: str | None,
        topic: str | None,
        generated: str,
        comments: list[tuple[str, str | None]],
    ) -> str:
        try:
            gen = json.loads(generated or "{}")
        except ValueError:
            gen = {}
        body = str(gen.get(_BODY_KEY.get(platform, "")) or "").strip()
        lines = [
            f"# {post_title(gen, body)}",
            f"플랫폼: {platform}",
            f"게시: {_day(published, self.tz)}",
            f"주소: {url or '없음'}",
            f"주제: {topic or '없음'}",
            "",
        ]
        if body:
            lines += [escape_headings(body), ""]
        cards = card_lines(gen.get("deck") or {})
        if cards:
            lines += ["## 카드 문구", *(f"- {c}" for c in cards), ""]
        if comments:
            lines.append("## 댓글")
            for text, reply in comments:
                item = f"- {one_line(text, 300)}"
                if reply:
                    item += f" → 답글: {one_line(reply, 300)}"
                lines.append(item)
            lines.append("")
        return "\n".join(lines)
