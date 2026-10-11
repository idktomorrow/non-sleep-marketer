"""도메인 모델. docs/contracts.md 2절과 5절의 코드 원본.

이 파일은 동결 대상이다. 바꾸려면 메인·서브가 합의한 PR 하나에서 호출부까지 함께 고친다.
시각은 모두 timezone-aware datetime(UTC 권장)으로 다룬다. 저장소 레이어가 UTC로 변환한다.
"""

from __future__ import annotations

import re
from datetime import datetime, time
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

Platform = Literal["instagram", "threads", "naver_blog"]
PLATFORMS: tuple[Platform, ...] = ("instagram", "threads", "naver_blog")

# 콘텐츠 수준 상태. 플랫폼별 게시 상태는 PublicationStatus (ADR 0009).
ContentStatus = Literal["draft", "pending_review", "approved", "published", "discarded"]
PublicationStatus = Literal["queued", "publishing", "published", "failed", "abandoned"]

CommentCategory = Literal[
    "praise_general", "question", "complaint", "price_refund", "legal", "spam"
]
COMMENT_CATEGORIES: tuple[CommentCategory, ...] = (
    "praise_general",
    "question",
    "complaint",
    "price_refund",
    "legal",
    "spam",
)
CommentAction = Literal["auto_reply", "hide", "escalate", "ignore"]
TemplateKind = Literal["info", "list", "compare"]
CostKind = Literal["llm", "image", "classifier"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _check_hex(v: str) -> str:
    if not _HEX.match(v):
        raise ValueError(f"색은 #RGB 또는 #RRGGBB 형식이어야 합니다: {v!r}")
    return v


HexColor = Annotated[str, AfterValidator(_check_hex)]


# ---------------------------------------------------------------- 설정 계열


class ActiveWindow(_Model):
    """활동 시간 구간(로컬 시간). end < start 이면 자정을 넘는 구간."""

    start: time
    end: time


class Persona(_Model):
    name: str
    handle: str
    character: str
    tone: str
    banned_words: list[str] = Field(default_factory=list)
    banned_patterns: list[str] = Field(default_factory=list)  # 정규식
    topics: list[str] = Field(min_length=1)
    active_hours: list[ActiveWindow] = Field(min_length=1)
    enabled: bool = False
    ad_label: bool = False  # 협찬 게시물 "광고" 표기


class Brand(_Model):
    name: str
    industry: str
    products: list[str] = Field(default_factory=list)
    key_messages: list[str] = Field(default_factory=list)
    primary_color: HexColor = "#1E40AF"
    accent_color: HexColor = "#F59E0B"
    logo_path: Path | None = None


# ---------------------------------------------------------------- 수집·분류


class SourceItem(_Model):
    id: str
    source: str  # "upload" | "naver" | "google_trends" | "community:<name>"
    url: str | None
    title: str
    text: str
    published_at: datetime | None = None
    collected_at: datetime
    url_hash: str


class Classification(_Model):
    label: str
    confidence: float = Field(ge=0.0, le=1.0)


# ---------------------------------------------------------------- 카드 덱 (contracts 5절)


class DeckBrand(_Model):
    name: str
    primary_color: HexColor
    accent_color: HexColor
    logo_path: str | None = None


class DeckPersona(_Model):
    name: str
    handle: str


class DeckLabels(_Model):
    virtual_person: bool = True
    ad: bool = False
    ai_generated_image: bool = False


class CoverSlide(_Model):
    kind: Literal["cover"] = "cover"
    title: str
    subtitle: str = ""
    background: str | None = None


class BodySlide(_Model):
    kind: Literal["body"] = "body"
    heading: str
    body: str
    bullets: list[str] = Field(default_factory=list)
    background: str | None = None


class ListSlide(_Model):
    kind: Literal["list"] = "list"
    heading: str
    bullets: list[str] = Field(min_length=1)
    background: str | None = None


class CompareSide(_Model):
    title: str
    points: list[str] = Field(min_length=1)


class CompareSlide(_Model):
    kind: Literal["compare"] = "compare"
    heading: str
    left: CompareSide
    right: CompareSide
    background: str | None = None


class OutroSlide(_Model):
    kind: Literal["outro"] = "outro"
    cta: str
    sources: list[str] = Field(default_factory=list)
    background: str | None = None


Slide = Annotated[
    CoverSlide | BodySlide | ListSlide | CompareSlide | OutroSlide,
    Field(discriminator="kind"),
]


class CardDeck(_Model):
    template: TemplateKind
    brand: DeckBrand
    persona: DeckPersona
    labels: DeckLabels = Field(default_factory=DeckLabels)
    slides: list[Slide] = Field(min_length=2, max_length=10)

    @model_validator(mode="after")
    def _shape(self) -> CardDeck:
        if self.slides[0].kind != "cover":
            raise ValueError("첫 슬라이드는 cover")
        if self.slides[-1].kind != "outro":
            raise ValueError("마지막 슬라이드는 outro")
        middle = {s.kind for s in self.slides[1:-1]}
        allowed = {"info": {"body"}, "list": {"list"}, "compare": {"compare"}}[self.template]
        if not middle <= allowed:
            raise ValueError(f"{self.template} 템플릿에 허용되지 않는 슬라이드: {middle - allowed}")
        return self


# ---------------------------------------------------------------- 생성·콘텐츠


class Claim(_Model):
    text: str
    source_item_id: str


class ContentBrief(_Model):
    topic: str
    template: TemplateKind
    sources: list[SourceItem] = Field(min_length=1)
    is_ad: bool = False


class GeneratedContent(_Model):
    topic: str
    template: TemplateKind
    deck: CardDeck
    instagram_caption: str
    threads_text: str
    blog_title: str
    blog_markdown: str
    claims: list[Claim]


class Content(_Model):
    id: str
    status: ContentStatus
    generated: GeneratedContent
    relevance_confidence: float
    is_ad: bool = False
    created_at: datetime
    review_reason: str | None = None


class Publication(_Model):
    id: str
    content_id: str
    platform: Platform
    status: PublicationStatus
    attempts: int = 0
    next_attempt_at: datetime | None = None
    external_id: str | None = None
    url: str | None = None
    last_error: str | None = None
    published_at: datetime | None = None


class RenderedPost(_Model):
    """어댑터에 넘기는 플랫폼별 최종 형태. 표기는 이미 삽입돼 있다."""

    content_id: str
    platform: Platform
    text: str
    images: list[Path]
    title: str | None = None  # naver_blog는 필수

    @model_validator(mode="after")
    def _blog_title(self) -> RenderedPost:
        if self.platform == "naver_blog" and not self.title:
            raise ValueError("naver_blog 게시물은 title 필수")
        return self


class PublishResult(_Model):
    external_id: str
    url: str


class Comment(_Model):
    platform: Platform
    external_id: str
    post_external_id: str
    author_id: str
    author_name: str
    text: str
    created_at: datetime


class PostMetrics(_Model):
    platform: Platform
    post_external_id: str
    captured_at: datetime
    views: int | None = None
    reach: int | None = None
    impressions: int | None = None
    likes: int | None = None
    comments: int | None = None
    saves: int | None = None
    shares: int | None = None


class AccountMetrics(_Model):
    platform: Platform
    captured_at: datetime
    followers: int | None = None


class HealthStatus(_Model):
    logged_in: bool
    detail: str = ""


class CostRecord(_Model):
    kind: CostKind
    provider: str
    usd: float = Field(ge=0.0)
    at: datetime
    ref: str = ""
