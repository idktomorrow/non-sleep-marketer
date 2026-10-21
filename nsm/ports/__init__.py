"""포트: 엔진이 바깥 세계와 대화하는 인터페이스. docs/contracts.md 3절의 코드 원본 (동결).

엔진·정책은 이 Protocol만 알고, 구현(어댑터)은 nsm/adapters/ 아래에 있다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from nsm.domain.models import (
    AccountMetrics,
    Brand,
    CardDeck,
    Classification,
    Comment,
    ContentBrief,
    CostKind,
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


# ---- S13 제안(동결 계약 추가, 합의 필요): 범용 이미지 제작기와 비용 원장 ------------------------------------
# ImageGenerator(표지 배경 한 장)와 별개로, 역할·참조 이미지·투명 배경을 지정해 그림을 만드는 포트.
# 글자는 절대 그림에 넣지 않는다(ADR 0007). 엔진은 ImageSpec을 만들고 결과 파일만 받는다.


@dataclass(frozen=True)
class ImageSpec:
    prompt: str
    width: int = 1024
    height: int = 1024
    quality: Literal["low", "medium", "high"] = "medium"
    transparent: bool = False  # 투명 배경(스티커). png로 받는다
    references: tuple[Path, ...] = ()  # 참조 이미지(캐릭터 외형 유지용). 있으면 편집 엔드포인트
    negative: str = ""  # 제외할 요소(문장으로 프롬프트 끝에 붙는다)
    ref: str = ""  # 비용·로그에 남길 이름(예: "sticker:s1")


@dataclass(frozen=True)
class ImageResult:
    path: Path
    model: str
    usd: float
    cost_status: Literal["confirmed", "estimated", "unknown"]
    request_id: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


class ImageRefused(Exception):
    """정책 차단(moderation_blocked)처럼 다시 보내도 같은 결과인 실패. 사유를 사람에게 보여 준다."""

    def __init__(self, message: str, categories: tuple[str, ...] = (), stage: str = "") -> None:
        super().__init__(message)
        self.categories = categories
        self.stage = stage


class ImageUnknown(Exception):
    """요청이 서버에 닿은 뒤 응답을 못 받음(타임아웃·끊김). 생성·과금 여부가 불명확하므로 자동 재시도 금지."""


@runtime_checkable
class ImageStudio(Protocol):
    def make(self, spec: ImageSpec) -> ImageResult: ...


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

    def record_cost(self, kind: CostKind, provider: str, usd: float, ref: str = "") -> None: ...


CostStatus = Literal["confirmed", "estimated", "unknown", "reserved", "released"]


@runtime_checkable
class CostLedger(CostSink, Protocol):
    """S13 제안: 전역 비용 원장. 유료 호출 전에 예약하고(reserve), 응답의 usage로 같은 행을 확정(confirm)하거나
    전송 전 실패면 해제(release), 전송 뒤 응답을 못 받았으면 미확인(mark_unknown)으로 남긴다.
    비용 상한은 confirmed+estimated+reserved(미확인 포함)를 더해 센다 — 미확인은 금액이 없으니 예약액으로."""

    def reserve(
        self, kind: CostKind, provider: str, estimate_usd: float, ref: str = "", model: str = ""
    ) -> str: ...
    def confirm(
        self,
        reservation_id: str,
        usd: float,
        status: Literal["confirmed", "estimated"] = "confirmed",
        detail: dict[str, Any] | None = None,
        request_id: str = "",
    ) -> None: ...
    def release(self, reservation_id: str) -> None: ...
    def mark_unknown(self, reservation_id: str, request_id: str = "") -> None: ...
    def spent_between(self, start: datetime, end: datetime) -> float: ...
