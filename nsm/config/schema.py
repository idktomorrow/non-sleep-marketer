"""사용자 설정 파일(config.yaml) 스키마. 설정 마법사(`nsm init`)가 이 형식으로 저장한다.

API 키는 여기 두지 않는다. keyring(Windows 자격 증명 관리자)에 둔다(nsm/config/secrets.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nsm.domain.models import ActiveWindow, Brand, Persona, Platform


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# 카드 디자인 스타일(S8, ADR 0036). clean은 기존 스타일. clean·photo·illust는 배경을 코드(CSS·SVG)로 그린다.
# editorial(S10 제안, ADR 0050): 사람이 확인한 사진을 크게 깔고 글은 그 위 패널에 올리는 사진 우선 레이아웃.
CardStyle = Literal["clean", "photo", "illust", "editorial"]
CARD_STYLES: tuple[str, ...] = get_args(CardStyle)


# 일러스트형 카드 캐릭터(C17·C20, ADR 0046): 없음(기본), 직접 올린 그림, 코드로 그린 캐릭터
CharacterChoice = Literal["none", "custom", "drawn"]


class BrandSettings(Brand):
    """설정 파일의 brand. 도메인 Brand(동결)에 카드 모양 설정만 더한다. 덱 JSON 계약은 그대로다."""

    card_style: CardStyle = "clean"
    character: CharacterChoice = "none"
    character_cover: str | None = None  # custom일 때 표지 그림(<NSM_HOME>/brand/에 저장한 투명 PNG)
    character_outro: str | None = None  # custom일 때 마지막 장 그림. 비우면 표지 그림
    # 캐릭터가 있는 장에 "AI 생성 이미지" 표기(ADR 0046). AI로 만든 그림이라도 실제 사람·사진처럼 보이지 않는
    # 만화 캐릭터면 표기하지 않는다(C19, 선웅제 결정). 실제처럼 보이는 그림을 올리면 켠다
    character_ai_made: bool = False

    @field_validator("character", mode="before")
    @classmethod
    def _old_bundled(cls, v: object) -> object:
        # C17의 기본 캐릭터(앵무새·햄스터)는 C20에서 뺐다. 예전 설정 파일은 "없음"으로 읽는다
        return "none" if v in ("parrot", "hamster") else v


# 대시보드·마법사가 다시 저장할 때 지켜야 하는 카드 모양 설정
CARD_LOOK_FIELDS = (
    "card_style",
    "character",
    "character_cover",
    "character_outro",
    "character_ai_made",
)


class CollectSettings(_Model):
    naver: bool = True
    google_trends: bool = True
    community: bool = False
    community_urls: list[str] = Field(default_factory=list)  # {query} 자리표시자 포함 검색 URL
    upload_dir: str | None = None  # 기본: <NSM_HOME>/uploads
    # 네이버 검색 기간: 최신순으로 최근 1d/1w/1m만, all은 기간 없이 정확도순(ADR 0032)
    recent_period: Literal["1d", "1w", "1m", "all"] = "1w"
    per_source_limit: int = 30  # 소스마다 한 번에 모을 최대 건수(주제×탭으로 나눔)
    # 제목·본문에 이 낱말이 든 글은 모으지 않고, 이미 모은 것도 원고에 쓰지 않는다(C15, ADR 0044)
    exclude_words: list[str] = Field(default_factory=list)
    min_delay_seconds: float = 3.0
    max_delay_seconds: float = 9.0

    @field_validator("community_urls")
    @classmethod
    def _has_query(cls, v: list[str]) -> list[str]:
        bad = [u for u in v if "{query}" not in u]
        if bad:
            raise ValueError(f"커뮤니티 검색 URL에는 {{query}} 자리표시자가 필요합니다: {bad}")
        return v


class PicksSettings(_Model):
    """오늘의 추천(S11 제안, ADR 0051): 매일 최근 소식 중 서로 다른 소재 최대 N개를 완성 원고로 추천한다.

    켜면 일일 원고 만들기(generate)는 쉬고 추천(picks)이 대신 돈다. 추천 원고는 전부 검토 대기로 가고,
    일일 생성 목표·게시 쿼터를 쓰지 않는다. 사진 후보는 사람이 "쓰기"를 눌러야 카드에 들어간다.
    """

    enabled: bool = False
    count: int = Field(default=3, ge=1, le=5)  # (S13부터 workspace.info_count로 옮김, 호환용)
    fresh_hours: int = Field(default=72, ge=6, le=24 * 14)  # 이 안에 공개된 자료만 "최근 소식"
    photo_candidates: bool = (
        True  # 기사 대표 이미지(og:image)를 후보로 받아 둘지(카드에는 사람이 확인한 뒤)
    )
    # (S13부터 workspace.aliases로 옮김, 호환용) 대표 주제 → 검색 별칭
    aliases: dict[str, list[str]] = Field(default_factory=dict)


class ModelSettings(_Model):
    generation: str = "claude-sonnet-5-5"
    classifier: str = "claude-haiku-5-5"
    gemini: str = "gemini-3.5-flash-lite"  # 무료 구간에서 되는 것을 확인한 모델(ADR 0048)
    image: str = "gpt-image-2.5-flare"
    # OpenAI 텍스트(S13 제안): Claude sonnet과 같은 값대($2/$10)의 gpt-6.1-sol. 추론은 낮게(출력 토큰 절약)
    openai_text: str = "gpt-6.1-sol"
    openai_text_effort: Literal["none", "minimal", "low", "medium"] = "low"
    # 이미지 품질(S13 제안). medium이 세로형 한 장 약 $0.04~0.05(2026-10 공식 안내 기준 추정)
    image_quality: Literal["low", "medium", "high"] = "medium"


class Settings(_Model):
    dry_run: bool = True
    timezone: str = "Asia/Seoul"
    # 네이버 블로그는 MVP 게시 대상에서 뺐다(ADR 0016). "naver_blog" 값은 동결 계약이라 남아 있다.
    enabled_platforms: list[Platform] = Field(default_factory=lambda: ["instagram", "threads"])
    quotas: dict[Platform, int] = Field(default_factory=lambda: {"instagram": 1, "threads": 2})
    daily_cost_cap_usd: float = 3.0
    protect_count: int = 20
    retry_delays_minutes: list[int] = Field(default_factory=lambda: [15, 60, 240])
    min_hours_between_posts: float = 3.0  # 같은 플랫폼 게시 사이 최소 간격
    # 게시만 이 시간에(ADR 0027). 비우면 페르소나 활동 시간과 같다. 수집·생성·답글은 활동 시간을 따른다.
    posting_hours: list[ActiveWindow] | None = None
    relevance_threshold: float = 0.8
    reply_daily_limit_per_author: int = 3
    topic_cooldown_days: int = 14
    comment_lookback_days: int = 14
    # 댓글 확인 간격(분). 너무 짧으면 플랫폼이 자동화로 의심할 수 있어 3분 아래로는 두지 않는다(ADR 0029).
    reply_interval_minutes: int = Field(default=5, ge=3, le=60)
    # 인스타그램은 댓글을 보려면 글 페이지를 하나씩 열어야 해서 요청 제한(HTTP 429)에 걸리기 쉽다.
    # 위 간격과 따로, 이 간격(분)보다 자주 보지 않고 최근 며칠 글만 본다(ADR 0038).
    instagram_comment_interval_minutes: int = Field(default=30, ge=10, le=180)
    instagram_comment_lookback_days: int = Field(default=3, ge=1, le=14)
    # 안정화(C12): 수집 관련 자료·게시가 이 일수 동안 0건이면 알림(화면 구조 변경 등으로 조용히 멈춘 경우)
    stall_alert_days: int = Field(default=2, ge=1, le=14)
    backup_keep: int = Field(default=7, ge=1, le=60)  # 하루 한 번 데이터 백업, 최근 몇 개를 남길지
    run_log_keep_days: int = Field(default=90, ge=7, le=3650)  # 실행 기록 보관 일수
    desktop_notifications: bool = True  # Windows 알림. 꺼도 대시보드 안 표시와 로그는 그대로
    # auto: Gemini 키 → gemini(무료 구간), Anthropic 키 → claude, 둘 다 없으면 keyword (ADR 0048)
    classifier: Literal["auto", "gemini", "claude", "openai", "keyword"] = "auto"
    # 글 생성·답글 공급자(S13 제안). auto: Claude 키 → claude, 아니면 OpenAI 키 → openai(기존 사용자 동작 유지).
    # 명시하면 그 공급자만 쓰고, 키가 없으면 생성하지 않는다(조용히 다른 공급자로 바꾸지 않음)
    text_provider: Literal["auto", "openai", "claude"] = "auto"
    # off(기본): AI 배경을 만들지 않고 코드로 그린 배경만(유료라서 꺼 둠, ADR 0036).
    # auto: OpenAI 키가 있으면 생성, 없으면 템플릿 배경. on: 항상 생성(키 없으면 경고 후 템플릿 배경)
    image_generation: Literal["auto", "on", "off"] = "off"
    # ADR 0017: 1일차 검증에서 Aside 무인 게시 확인. 구현 전(배치 G)에는 Playwright로 대신 동작
    browser_driver: Literal["aside", "playwright"] = "aside"
    # 게시 계정 핸들(예: {"threads": "@nonsleepmarketer"}). 실계정 어댑터(배치 G)는 로그인된
    # 계정이 여기 적힌 핸들과 다르면 게시하지 않는다. 비어 있으면 실계정 게시를 하지 않는다.
    accounts: dict[Platform, str] = Field(default_factory=dict)
    browser_profile_dir: str | None = None  # 기본: <NSM_HOME>/browser-profile
    collect_profile_dir: str | None = (
        None  # 수집 전용 계정 프로필. 기본: <NSM_HOME>/collect-profile
    )
    headless_collect: bool = True
    dashboard_port: int = 8765
    preset: str = "기본"  # 지금 쓰는 프리셋(페르소나+브랜드+DB 묶음, C29 ADR 0049)
    collect: CollectSettings = Field(default_factory=CollectSettings)
    models: ModelSettings = Field(default_factory=ModelSettings)
    picks: PicksSettings = Field(default_factory=PicksSettings)

    @field_validator("classifier", mode="before")
    @classmethod
    def _old_jev(cls, v: object) -> object:
        return "auto" if v == "jev" else v  # jev(MotherDuck)는 C27에서 뺐다. 예전 설정은 auto로


class WorkspaceSettings(_Model):
    """작업 공간(프리셋)별 설정(S13 제안): 페르소나·브랜드처럼 프리셋 파일에 함께 저장·복원된다.

    무엇을 다루는지(별칭), 하루 추천 구성(정보/밈 개수), 제작 모드, 기본 출연 캐릭터, 디자인 옵션은
    작업 공간마다 다르다. API 키·공급자·모델·비용 상한·플랫폼·스케줄은 전역(settings)에 그대로 둔다.
    """

    schema_version: int = (
        1  # 필드 "없음"과 "빈 값"을 구분하기 위한 버전(예전 설정은 이 필드가 없다)
    )
    # 대표 주제 → 검색 별칭. 검색어 확장에만 쓰고 분류 라벨·추천 주제는 대표 주제로 정규화(ADR 0051)
    aliases: dict[str, list[str]] = Field(default_factory=dict)
    info_count: int = Field(default=3, ge=0, le=5)  # 하루 정보 추천 수
    meme_count: int = Field(default=0, ge=0, le=3)  # 하루 밈·공감 추천 수(S14)
    allow_substitute: bool = False  # 한 종류 후보가 모자라면 다른 종류로 채울지(기본 안 함)
    # plan_first: 제목·대본·구성을 먼저 보고 고른 것만 그림 제작(유료). full: 예산 안에서 그림까지 완성(S15)
    production_mode: Literal["plan_first", "full"] = "plan_first"
    hero_default: str | None = None  # 기본 주인공 캐릭터 프로필 id(S15)
    sidekick_default: str | None = (
        None  # 기본 보조 캐릭터 프로필 id(S15). None이면 보조 캐릭터 없음
    )
    meme_tone: str = ""  # 밈 대본 말투(호출별로만 씀, 전역 페르소나 말투는 그대로. S14)
    title_style: Literal["plain", "sticker"] = "plain"  # 표지 제목 모양(S14)
    color_from_photo: bool = True  # 소재 사진에서 카드 색을 뽑을지(S14)


class AppConfig(_Model):
    version: int = 1
    risk_accepted_at: datetime | None = None
    brand: BrandSettings
    persona: Persona
    settings: Settings = Field(default_factory=Settings)
    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)

    @model_validator(mode="after")
    def _legacy_picks_into_workspace(self) -> AppConfig:
        """S11의 settings.picks.count/aliases에서 작업 공간 설정으로 옮긴다(호환).

        "없음"과 "빈 값"을 구분한다: 예전 설정 파일에는 workspace 절 자체가 없으므로 그때만 picks 값을
        가져온다. workspace.aliases = {}처럼 사용자가 일부러 비운 값은 그대로 둔다.
        """
        if "workspace" in self.model_fields_set:
            return self
        picks = self.settings.picks
        legacy: dict[str, object] = {}
        if "count" in picks.model_fields_set:
            legacy["info_count"] = picks.count
        if "aliases" in picks.model_fields_set:
            legacy["aliases"] = dict(picks.aliases)
        if legacy:
            object.__setattr__(self, "workspace", self.workspace.model_copy(update=legacy))
        return self

    @property
    def daily_slot_total(self) -> int:
        return self.workspace.info_count + self.workspace.meme_count

    @field_validator("brand", mode="before")
    @classmethod
    def _brand_from_domain(cls, v: object) -> object:
        # 마법사·테스트가 도메인 Brand를 넘겨도 받는다(card_style은 기본값)
        return v.model_dump() if isinstance(v, Brand) and not isinstance(v, BrandSettings) else v
