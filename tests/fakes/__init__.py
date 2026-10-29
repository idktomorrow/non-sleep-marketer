"""테스트 전용 가짜 포트 구현 (docs/contracts.md 7절).

드라이런 어댑터(제품 기능, 미리보기 폴더에 씀)와 다르다. 가짜는 메모리에 호출을 기록만 한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from nsm.domain.models import (
    AccountMetrics,
    Brand,
    CardDeck,
    Claim,
    Classification,
    Comment,
    ContentBrief,
    GeneratedContent,
    HealthStatus,
    Persona,
    Platform,
    PostMetrics,
    PublishResult,
    RenderedPost,
    SourceItem,
)
from nsm.ports import CAPABILITIES, AdapterError, NotSupportedError

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load_deck(template: str) -> CardDeck:
    return CardDeck.model_validate_json(
        (FIXTURES / "decks" / f"{template}.json").read_text("utf-8")
    )


class FixedClock:
    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FixedClock에는 aware datetime을 주세요")
        self._now = at

    def now(self) -> datetime:
        return self._now

    def set(self, at: datetime) -> None:
        self._now = at

    def advance(self, **kwargs: float) -> None:
        self._now = self._now + timedelta(**kwargs)


class FakePlatformAdapter:
    def __init__(
        self,
        platform: Platform,
        capabilities: set[str] | None = None,
        clock: FixedClock | None = None,
    ) -> None:
        self.platform = platform
        self.clock = clock
        self._caps = set(CAPABILITIES if capabilities is None else capabilities)
        self.published: list[RenderedPost] = []
        self.replied: list[tuple[Comment, str]] = []
        self.hidden: list[Comment] = []
        self.comments: dict[str, list[Comment]] = {}
        self.post_metrics: dict[str, PostMetrics] = {}
        self.followers: int | None = 100
        self.fail_next_publish = 0
        self.logged_in = True
        self._n = 0

    def capabilities(self) -> set[str]:
        return set(self._caps)

    def health(self) -> HealthStatus:
        return HealthStatus(logged_in=self.logged_in, detail="fake")

    def _now(self) -> datetime:
        return self.clock.now() if self.clock else datetime.now().astimezone()

    def publish(self, post: RenderedPost) -> PublishResult:
        if "publish" not in self._caps:
            raise NotSupportedError("publish")
        if self.fail_next_publish > 0:
            self.fail_next_publish -= 1
            raise AdapterError("fake publish failure")
        self.published.append(post)
        self._n += 1
        ext = f"{self.platform}-post-{self._n}"
        return PublishResult(external_id=ext, url=f"https://example.com/{self.platform}/{ext}")

    def fetch_new_comments(
        self, post_external_ids: list[str], since: datetime | None
    ) -> list[Comment]:
        if "fetch_comments" not in self._caps:
            raise NotSupportedError("fetch_comments")
        out: list[Comment] = []
        for pid in post_external_ids:
            for c in self.comments.get(pid, []):
                if since is None or c.created_at > since:
                    out.append(c)
        return out

    def reply(self, comment: Comment, text: str) -> str:
        if "reply" not in self._caps:
            raise NotSupportedError("reply")
        self.replied.append((comment, text))
        return f"reply-{len(self.replied)}"

    def hide_comment(self, comment: Comment) -> None:
        if "hide_comment" not in self._caps:
            raise NotSupportedError("hide_comment")
        self.hidden.append(comment)

    def read_post_metrics(self, post_external_id: str) -> PostMetrics:
        return self.post_metrics.get(
            post_external_id,
            PostMetrics(
                platform=self.platform,
                post_external_id=post_external_id,
                captured_at=self._now(),
                likes=1,
            ),
        )

    def read_account_metrics(self) -> AccountMetrics:
        return AccountMetrics(
            platform=self.platform,
            captured_at=self._now(),
            followers=self.followers,
        )


class FakeSource:
    def __init__(self, name: str, items: list[SourceItem]) -> None:
        self.name = name
        self.items = items
        self.calls: list[tuple[list[str], int]] = []

    def collect(self, topics: list[str], limit: int) -> list[SourceItem]:
        self.calls.append((topics, limit))
        return self.items[:limit]


class FakeClassifier:
    """규칙: `by_text`에 정확히 일치하는 텍스트가 있으면 그 결과, 없으면 `default`.

    default가 None이면 첫 번째 라벨을 confidence 0.9로 돌려준다.
    """

    def __init__(
        self,
        by_text: dict[str, Classification] | None = None,
        default: Classification | None = None,
        by_instruction: dict[str, Classification] | None = None,
    ) -> None:
        self.by_text = by_text or {}
        self.default = default
        self.by_instruction = by_instruction or {}
        self.calls: list[tuple[str, tuple[str, ...], str]] = []

    def classify(self, text: str, labels: list[str], instruction: str) -> Classification:
        self.calls.append((text, tuple(labels), instruction))
        if text in self.by_text:
            return self.by_text[text]
        for key, value in self.by_instruction.items():
            if key in instruction:
                return value
        if self.default is not None:
            return self.default
        return Classification(label=labels[0], confidence=0.9)

    def classify_many(
        self, texts: list[str], labels: list[str], instruction: str
    ) -> list[Classification]:
        return [self.classify(t, labels, instruction) for t in texts]


class FakeTextGenerator:
    """픽스처 덱으로 GeneratedContent를 만든다.

    - missing_source=True: 존재하지 않는 출처 ID를 가진 claim을 섞는다.
    - inject_text: 캡션·본문에 끼워 넣을 문구(금칙어 시나리오).
    - reply_text: 답글 문구.
    """

    def __init__(
        self,
        missing_source: bool = False,
        inject_text: str = "",
        reply_text: str = "고마워요! 또 놀러 와 주세요",
    ) -> None:
        self.missing_source = missing_source
        self.inject_text = inject_text
        self.reply_text = reply_text
        self.briefs: list[ContentBrief] = []
        self.reply_calls: list[Comment] = []

    def generate_content(
        self, brief: ContentBrief, persona: Persona, brand: Brand
    ) -> GeneratedContent:
        self.briefs.append(brief)
        deck = load_deck(brief.template)
        claims = [Claim(text=f"사실 {i}", source_item_id=s.id) for i, s in enumerate(brief.sources)]
        if self.missing_source:
            claims.append(Claim(text="출처 없는 주장", source_item_id="does-not-exist"))
        extra = f" {self.inject_text}" if self.inject_text else ""
        return GeneratedContent(
            topic=brief.topic,
            template=brief.template,
            deck=deck,
            instagram_caption=f"{brief.topic} 정리{extra}",
            threads_text=f"{brief.topic} 한 줄 요약{extra}",
            blog_title=f"{brief.topic} 총정리",
            blog_markdown=f"# {brief.topic}\n\n본문{extra}",
            claims=claims,
        )

    def generate_reply(self, comment: Comment, persona: Persona, post_context: str) -> str:
        self.reply_calls.append(comment)
        return self.reply_text


class NullImageGenerator:
    def generate_background(self, prompt: str, width: int, height: int) -> Path | None:
        return None


_ONE_PX_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d4944415478da63f8ffff3f0005fe02fea7d6a4b0000000"
    "0049454e44ae426082"
)


class FakeRenderer:
    def __init__(self) -> None:
        self.rendered: list[CardDeck] = []

    def render(self, deck: CardDeck, out_dir: Path) -> list[Path]:
        self.rendered.append(deck)
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i in range(len(deck.slides)):
            p = out_dir / f"slide-{i + 1:02d}.png"
            p.write_bytes(_ONE_PX_PNG)
            paths.append(p)
        return paths


@dataclass
class FakeNotifier:
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    def notify(self, title: str, body: str, level: Literal["info", "warn", "error"]) -> None:
        self.calls.append((title, body, level))


@dataclass
class FakeCostSink:
    records: list[tuple[str, str, float, str]] = field(default_factory=list)

    def record_cost(self, kind: str, provider: str, usd: float, ref: str = "") -> None:
        self.records.append((kind, provider, usd, ref))


def load_comment_cases() -> list[tuple[Comment, str]]:
    raw = json.loads((FIXTURES / "comments" / "sample.json").read_text("utf-8"))
    return [(Comment.model_validate(r["comment"]), r["expected_category"]) for r in raw]


def load_source_items() -> list[SourceItem]:
    raw = json.loads((FIXTURES / "sources" / "sample.json").read_text("utf-8"))
    return [SourceItem.model_validate(r) for r in raw]


class FakeDriver:
    """URL 접두어 → 링크 목록. 방문 기록을 남긴다."""

    def __init__(self, pages: dict[str, list] | None = None, fail_on: str | None = None) -> None:
        self.pages = pages or {}
        self.fail_on = fail_on
        self.visited: list[str] = []
        self._current = ""
        self.closed = False

    def goto(self, url: str) -> None:
        if self.fail_on and self.fail_on in url:
            raise AdapterError(f"fake navigation failure: {url}")
        self.visited.append(url)
        self._current = url

    def links(self) -> list:
        for prefix, links in self.pages.items():
            if self._current.startswith(prefix):
                return list(links)
        return []

    def page_text(self) -> str:
        return ""

    def close(self) -> None:
        self.closed = True


class FakeEmbedder:
    """R1 테스트용 결정적 임베더: 같은 글은 늘 같은 벡터. 네트워크 없음. 부른 글을 기록한다."""

    def __init__(
        self, dim: int = 8, model: str = "fake-embed", fail_after: int | None = None
    ) -> None:
        self.dim = dim
        self.model = model
        self.batch_size = 100
        self.calls: list[list[str]] = []
        self.fail_after = fail_after  # 이만큼 묶음을 부른 뒤엔 요청 제한처럼 실패

    def embed(self, texts: list[str]) -> list[list[float]]:
        import hashlib

        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            from nsm.rag.gemini import EmbeddingError

            raise EmbeddingError("가짜 요청 제한(HTTP 429)", "rate_limited")
        self.calls.append(list(texts))
        out = []
        for t in texts:
            h = hashlib.sha256(t.encode("utf-8")).digest()
            out.append([round(b / 255.0, 6) for b in h[: self.dim]])
        return out
