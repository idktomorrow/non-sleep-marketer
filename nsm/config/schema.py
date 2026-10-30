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


# 카드 디자인 스타일(S8, ADR 0036). clean은 기존 스타일. 세 스타일 모두 배경을 코드(CSS·SVG)로 그린다.
CardStyle = Literal["clean", "photo", "illust"]
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
    google_news: bool = True  # 구글 뉴스 RSS 검색(주제별 최신 기사, C30 ADR 0050). 끄면 롤백과 같다
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


class ModelSettings(_Model):
    generation: str = "claude-sonnet-5-5"
    classifier: str = "claude-haiku-5-5"
    gemini: str = "gemini-3.5-flash-lite"  # 무료 구간에서 되는 것을 확인한 모델(ADR 0048)
    image: str = "gpt-image-2.5-flare"


class RagSettings(_Model):
    """문서 색인(R1, ADR 0056). 값은 2026-10-30 Gemini 공식 문서 기준."""

    # 조각 크기·겹침(글자). 300·500·800 비교 뒤 500/80(devlog R1)
    chunk_chars: int = Field(default=500, ge=100, le=4000)
    chunk_overlap: int = Field(default=80, ge=0, le=1000)
    # docs 기준 경로. "폴더/"는 그 아래 전부, 나머지는 파일 패턴
    include: list[str] = Field(
        default_factory=lambda: [
            "adr/",
            "devlog/",
            "incidents/",
            "user-guide.md",
            "architecture.md",
            "contracts.md",
            "glossary.md",
        ]
    )
    exclude: list[str] = Field(
        default_factory=lambda: [
            "presentation/",
            "learning/",
            "samples/",
            "adr/0000-template.md",
            "incidents/0000-template.md",
        ]
    )
    # Gemini 임베딩: 최신 모델 gemini-embedding-2(기본 3072차원, 권장 768·1536·3072, 768은 자동 정규화)
    embedding_model: str = "gemini-embedding-2"
    embedding_dim: int = Field(default=768, ge=128, le=3072)
    # 한 번에 묶는 조각 수. 공식 문서에 상한이 없고 예전 API는 100이 상한이라 100
    embed_batch_size: int = Field(default=100, ge=1, le=100)
    # 묶음 요청 사이 간격(초). 무료 구간은 "모델별 분당 100건"이고 묶음 안의 조각 하나하나를 1건으로 센다
    # (2026-10-30 429 응답의 quotaId EmbedContentRequestsPerMinutePerUserPerProjectPerModel-FreeTier, 값 100으로 확인).
    # 100개 묶음이면 1분에 한 번이라 61초. 유료면 줄여도 된다
    embed_interval_seconds: float = Field(default=61.0, ge=0, le=600)
    # 한 번 실행에 임베딩할 최대 조각 수. 무료 구간 하루 한도가 1,000조각이고 막힌 시도도 세어져서, 넘기기 전에 스스로 멈춘다.
    # 남은 조각은 다음 날 실행이 이어서 한다. 유료면 늘려도 된다
    embed_max_per_run: int = Field(default=900, ge=1, le=100000)
    # 입력 100만 토큰당 달러. 무료 구간은 0원, 유료(Standard)는 0.20(2026-10-30 가격표)
    embedding_usd_per_mtok: float = Field(default=0.0, ge=0, le=10)

    # 색인 잡(R2): 하루 한 번, 이 시각(HH:MM, 현지). 무료 구간 하루 한도는 태평양 시간 자정(한국 오후 4~5시)에 풀린다
    index_time: str = "20:00"
    # 검색(R2): 상위 몇 개, 이 점수(코사인 유사도) 아래는 "근거 없음". 값은 실제 질문 20개로 고름(ADR 0057)
    search_k: int = Field(default=5, ge=1, le=20)
    min_score: float = Field(default=0.65, ge=0.0, le=1.0)

    @field_validator("index_time")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        try:
            h, m = (int(x) for x in v.split(":"))
        except ValueError:
            raise ValueError("rag.index_time은 HH:MM 형식이어야 합니다") from None
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError("rag.index_time은 00:00~23:59여야 합니다")
        return f"{h:02d}:{m:02d}"

    @model_validator(mode="after")
    def _overlap_below_size(self) -> RagSettings:
        if self.chunk_overlap >= self.chunk_chars:
            raise ValueError("rag.chunk_overlap은 rag.chunk_chars보다 작아야 합니다")
        return self


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
    # 원고 품질 하한(C31, ADR 0051)
    min_source_chars: int = Field(
        default=800, ge=0
    )  # 본문이 이보다 짧은 자료(요약문만)는 원고 재료로 안 씀
    min_facts: int = Field(
        default=2, ge=0
    )  # 원고의 구체 사실(날짜·수량·가격·장소) 수가 이보다 적으면 검토로
    topic_weekly_limit: int = Field(
        default=2, ge=0
    )  # 주제(소재 묶음)마다 최근 7일 원고 수 상한. 0이면 없음
    max_bodies_per_run: int = Field(default=24, ge=1, le=100)  # 수집 한 번에 원문을 읽는 자료 수
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
    # 자료 정리(C33): 관련 없음·원고에 안 쓰인 자료의 본문은 며칠 뒤 비우고(지문·제목은 남김), 더 지나면 판정 기록을 지운다
    source_body_keep_days: int = Field(default=7, ge=1, le=365)
    source_keep_days: int = Field(default=60, ge=7, le=3650)
    # 실패 증거 폴더(<NSM_HOME>/logs/shots) 보관 일수(E1, ADR 0054)
    evidence_keep_days: int = Field(default=14, ge=1, le=365)
    # 상태 확인(health)이 일시적 오류로 연속 몇 번 실패하면 자동 게시·답글을 멈출지(E4, ADR 0054 개정). 로그인 풀림은 바로 멈춘다
    health_pause_after_failures: int = Field(default=2, ge=1, le=10)
    desktop_notifications: bool = True  # Windows 알림. 꺼도 대시보드 안 표시와 로그는 그대로
    # auto: Gemini 키 → gemini(무료 구간), Anthropic 키 → claude, 둘 다 없으면 keyword (ADR 0048)
    classifier: Literal["auto", "gemini", "claude", "keyword"] = "auto"
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
    rag: RagSettings = Field(default_factory=RagSettings)

    @field_validator("classifier", mode="before")
    @classmethod
    def _old_jev(cls, v: object) -> object:
        return "auto" if v == "jev" else v  # jev(MotherDuck)는 C27에서 뺐다. 예전 설정은 auto로


class AppConfig(_Model):
    version: int = 1
    risk_accepted_at: datetime | None = None
    brand: BrandSettings
    persona: Persona
    settings: Settings = Field(default_factory=Settings)

    @field_validator("brand", mode="before")
    @classmethod
    def _brand_from_domain(cls, v: object) -> object:
        # 마법사·테스트가 도메인 Brand를 넘겨도 받는다(card_style은 기본값)
        return v.model_dump() if isinstance(v, Brand) and not isinstance(v, BrandSettings) else v
