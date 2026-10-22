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
# editorial(S12 제안, ADR 0052): 사진 우선 소식형. 중간 장에 body·products·facts를 섞어 쓸 수 있는 유일한 템플릿
TemplateKind = Literal["info", "list", "compare", "editorial", "meme"]
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

    @model_validator(mode="after")
    def _patterns_compile(self) -> Persona:
        # 깨진 정규식이 조용히 무시되면 안전장치가 꺼진 셈이라, 설정을 읽을 때 바로 막는다
        for p in self.banned_patterns:
            try:
                re.compile(p)
            except re.error as e:
                raise ValueError(f"금칙 패턴 정규식 오류 '{p}': {e}") from e
        return self


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
    ai_generated_image: bool = False  # 레거시(덱 전체 플래그). ai_slides가 None일 때만 판정에 쓴다
    # S14 제안(ADR 0055): "AI 생성 이미지"를 **장별·자산별**로. None = 레거시 덱(위 플래그 + background 규칙),
    # [] = AI 이미지 없음(빈 값은 "없음"이지 "모름"이 아니다). 값은 0부터 세는 장 번호
    ai_slides: list[int] | None = None


class ImageAsset(_Model):
    """장에 붙는 그림 하나(S14 제안): 경로 + 출처. 글자는 절대 그림에 넣지 않는다(ADR 0007)."""

    path: str
    ai: bool = False  # AI로 만든 그림이면 그 장에 "AI 생성 이미지" 표기
    alt: str = ""
    source: str = ""  # 실제 사진의 출처(기사 URL 등, S18). 출처 표기는 사용 허락이 아니다


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
    sticker: ImageAsset | None = None  # 메모 상자에 걸친 보조 캐릭터(S14 제안)
    # S18 제안(ADR 0062): 이 장 내용과 맞는 실제 사진(비율 그대로, 자르지 않음)과 그 사진을 고를 때 쓰는 힌트
    photo: ImageAsset | None = None
    photo_hint: str = ""


class ListSlide(_Model):
    kind: Literal["list"] = "list"
    heading: str
    bullets: list[str] = Field(min_length=1)
    background: str | None = None
    # S18 제안(ADR 0062): 이 장 내용과 맞는 실제 사진(비율 그대로, 자르지 않음)과 그 사진을 고를 때 쓰는 힌트
    photo: ImageAsset | None = None
    photo_hint: str = ""


class CompareSide(_Model):
    title: str
    points: list[str] = Field(min_length=1)


class CompareSlide(_Model):
    kind: Literal["compare"] = "compare"
    heading: str
    left: CompareSide
    right: CompareSide
    background: str | None = None
    # S18 제안(ADR 0062): 이 장 내용과 맞는 실제 사진(비율 그대로, 자르지 않음)과 그 사진을 고를 때 쓰는 힌트
    photo: ImageAsset | None = None
    photo_hint: str = ""


class OutroSlide(_Model):
    kind: Literal["outro"] = "outro"
    cta: str
    sources: list[str] = Field(default_factory=list)
    background: str | None = None


# ---- S12 제안(ADR 0052): 상품 그리드·일정 패널. 그림은 장별 background처럼 로컬 경로이고 글자는 HTML로 그린다


class ProductItem(_Model):
    name: str
    price: str = ""  # "¥2,310", "19,000원"처럼 표기 그대로(통화·단위를 코드가 해석하지 않는다)
    note: str = ""  # "전 7종", "8종 BOX" 같은 짧은 보조 설명
    image: str | None = None  # 상품 사진(사람이 올린 것). 없으면 이름만 크게


class ProductsSlide(_Model):
    kind: Literal["products"] = "products"
    heading: str
    items: list[ProductItem] = Field(min_length=2, max_length=4)
    background: str | None = None
    sticker: ImageAsset | None = None
    # S18 제안(ADR 0062): 이 장 내용과 맞는 실제 사진(비율 그대로, 자르지 않음)과 그 사진을 고를 때 쓰는 힌트
    photo: ImageAsset | None = None
    photo_hint: str = ""


class FactRow(_Model):
    label: str  # "출시일", "구매처", "구매 수량"
    value: str  # "10월 23일(금)부터", "ANA 온라인몰 외 4곳"


class FactsSlide(_Model):
    kind: Literal["facts"] = "facts"
    heading: str
    rows: list[FactRow] = Field(min_length=2, max_length=6)
    background: str | None = None
    sticker: ImageAsset | None = None
    # 정보 줄의 시각 배치(S18): 데이터 종류와 배치는 다르다. timeline은 모든 줄에 날짜가 있을 때만(생성기가 검증)
    layout: Literal["rows", "timeline", "conditions", "figures"] = "rows"
    # S18 제안(ADR 0062): 이 장 내용과 맞는 실제 사진(비율 그대로, 자르지 않음)과 그 사진을 고를 때 쓰는 힌트
    photo: ImageAsset | None = None
    photo_hint: str = ""


# ---- S14 제안(ADR 0055): 2컷 만화. 같은 캐릭터가 두 장면에 나오고 라벨(검은 상자)·말풍선은 HTML 글자다


class ComicPanel(_Model):
    image: ImageAsset | None = None  # 없으면 "그림 없음" 자리(도형으로 대체하지 않는다)
    label: str = Field(default="", max_length=30)  # 왼쪽 위 검은 라벨("치이카와 역 월급 225만원")
    bubble: str = Field(default="", max_length=40)  # 말풍선 대사("시러~")
    bubble_side: Literal["left", "right"] = "right"
    scene: str = ""  # 장면 지시(생성·재생성용, 카드에는 안 그린다)


class ComicSlide(_Model):
    kind: Literal["comic"] = "comic"
    panels: list[ComicPanel] = Field(min_length=2, max_length=2)
    background: str | None = None


class MemeOverlay(_Model):
    """밈 한 장 위의 글자 하나(S17 제안, ADR 0061). 글자는 HTML로 조판한다(ADR 0007). 위치는 장 안의 백분율.

    - caption: 그림 위·아래 짧은 캡션 / headline: 뉴스 자막 띠 / label: 작은 이름표 / stamp: 마지막 단어를 덮는 상자 등
    - bubble: 말풍선. tail_x·tail_y(백분율)는 **실제 화자**(입 근처)를 가리킨다. 없으면 꼬리 없음
    """

    kind: Literal["caption", "headline", "label", "stamp", "bubble"]
    text: str = Field(min_length=1, max_length=60)
    x: float = Field(ge=0, le=100)  # 상자 왼쪽(%)
    y: float = Field(ge=0, le=100)  # 상자 위(%)
    w: float = Field(default=40, gt=0, le=100)  # 상자 너비(%), 높이는 글 길이에 따라
    align: Literal["left", "center", "right"] = "center"
    size: Literal["s", "m", "l", "xl"] = "m"
    tail_x: float | None = Field(default=None, ge=0, le=100)
    tail_y: float | None = Field(default=None, ge=0, le=100)


class MemeImageSlide(_Model):
    """유명·유행 밈의 구도를 재해석한 한 장(S17 제안, ADR 0061). 컷 나눔·배경·표정은 그림이 담고 글자는 overlays.

    credits: 작은 출처 표기(캐릭터 출처·참고 밈). 출처 표기는 사용 허락이 아니다.
    """

    kind: Literal["meme_image"] = "meme_image"
    image: ImageAsset | None = None  # 없으면 "그림 없음" 자리
    overlays: list[MemeOverlay] = Field(default_factory=list, max_length=8)
    scene: str = ""  # 생성 지시(카드에는 안 그린다)
    format_key: str = ""  # 재해석한 밈 형식(예: this-is-fine)
    credits: list[str] = Field(default_factory=list, max_length=4)
    background: str | None = None


Slide = Annotated[
    CoverSlide
    | BodySlide
    | ListSlide
    | CompareSlide
    | ProductsSlide
    | FactsSlide
    | ComicSlide
    | MemeImageSlide
    | OutroSlide,
    Field(discriminator="kind"),
]


class CardDeck(_Model):
    template: TemplateKind
    brand: DeckBrand
    persona: DeckPersona
    labels: DeckLabels = Field(default_factory=DeckLabels)
    slides: list[Slide] = Field(min_length=1, max_length=10)
    # 덱의 출처(S18): 마무리 장이 없어도 캡션 출처 줄·작은 표기로 보존한다. 마무리 장이 있으면 그 장의 출처가 우선
    sources: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _shape(self) -> CardDeck:
        # 밈 한 장(S17 제안): 그 한 장이 완결이면 마무리 장을 억지로 붙이지 않는다(출처는 장 안 작은 표기·캡션)
        if self.template == "meme" and self.slides[0].kind == "meme_image":
            rest = {s.kind for s in self.slides[1:-1]}
            if self.slides[-1].kind not in ("meme_image", "outro") or not rest <= {"meme_image"}:
                raise ValueError("밈 한 장 덱은 meme_image 뒤에 meme_image 또는 마지막 outro만")
            return self
        if len(self.slides) < 2:
            raise ValueError("슬라이드는 2장 이상")
        # 밈(S14 제안): 첫 장이 만화 자체일 수 있다(레퍼런스는 2컷이 곧 첫 이미지). 다른 템플릿은 표지로 시작
        first_ok = {"cover", "comic"} if self.template == "meme" else {"cover"}
        if self.slides[0].kind not in first_ok:
            raise ValueError(
                "첫 슬라이드는 cover" + ("(밈은 comic도 가능)" if self.template == "meme" else "")
            )
        # 소식형(S18): 마무리 장은 필요할 때만. 없으면 마지막 내용 장으로 끝난다(출처는 덱 출처로)
        optional_outro = self.template == "editorial"
        if self.slides[-1].kind != "outro" and not optional_outro:
            raise ValueError("마지막 슬라이드는 outro")
        rest = self.slides[1:-1] if self.slides[-1].kind == "outro" else self.slides[1:]
        middle = {s.kind for s in rest}
        allowed = {
            "info": {"body"},
            "list": {"list"},
            "compare": {"compare"},
            # 소식형: 개요(body) + 상품 그리드 + 일정·구매처 패널(S12) + 만화 한 장(혼합 표현, S14)
            "editorial": {"body", "products", "facts", "comic", "list", "compare"},
            "meme": {"comic", "body", "meme_image"},
        }[self.template]
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
