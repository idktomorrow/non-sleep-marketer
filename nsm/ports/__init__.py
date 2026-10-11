"""포트: 엔진이 바깥 세계와 대화하는 인터페이스. docs/contracts.md 3절의 코드 원본 (동결).

엔진·정책은 이 Protocol만 알고, 구현(어댑터)은 nsm/adapters/ 아래에 있다.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from nsm.domain.models import (
    AccountMetrics,
    Brand,
    CardDeck,
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

CAPABILITIES = frozenset(
    {
        "publish",
        "fetch_comments",
        "reply",
        "hide_comment",
        "delete_comment",
        "post_metrics",
        "account_metrics",
    }
)


class NotSupportedError(Exception):
    """어댑터가 capability를 선언하지 않은 동작을 호출받았을 때."""


class AdapterError(Exception):
    """외부 동작 실패(네트워크, 화면 변경, 로그인 만료). 재시도 대상."""


@runtime_checkable
class PlatformAdapter(Protocol):
    platform: Platform

    def capabilities(self) -> set[str]: ...
    def health(self) -> HealthStatus: ...
    def publish(self, post: RenderedPost) -> PublishResult: ...
    def fetch_new_comments(
        self, post_external_ids: list[str], since: datetime | None
    ) -> list[Comment]: ...
    def reply(self, comment: Comment, text: str) -> str: ...
    def hide_comment(self, comment: Comment) -> None: ...
    def read_post_metrics(self, post_external_id: str) -> PostMetrics: ...
    def read_account_metrics(self) -> AccountMetrics: ...


@runtime_checkable
class Source(Protocol):
    name: str

    def collect(self, topics: list[str], limit: int) -> list[SourceItem]: ...


@runtime_checkable
class Classifier(Protocol):
    def classify(self, text: str, labels: list[str], instruction: str) -> Classification: ...
    def classify_many(
        self, texts: list[str], labels: list[str], instruction: str
    ) -> list[Classification]: ...


@runtime_checkable
class TextGenerator(Protocol):
    def generate_content(
        self, brief: ContentBrief, persona: Persona, brand: Brand
    ) -> GeneratedContent: ...
    def generate_reply(self, comment: Comment, persona: Persona, post_context: str) -> str: ...


@runtime_checkable
class ImageGenerator(Protocol):
    def generate_background(self, prompt: str, width: int, height: int) -> Path | None: ...


@runtime_checkable
class CardRenderer(Protocol):
    def render(self, deck: CardDeck, out_dir: Path) -> list[Path]: ...


@runtime_checkable
class Notifier(Protocol):
    def notify(self, title: str, body: str, level: Literal["info", "warn", "error"]) -> None: ...


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...


@runtime_checkable
class CostSink(Protocol):
    """어댑터가 API 비용을 보고하는 곳. 저장소가 구현한다."""

    def record_cost(self, kind: str, provider: str, usd: float, ref: str = "") -> None: ...
