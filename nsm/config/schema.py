"""사용자 설정 파일(config.yaml) 스키마. 설정 마법사(`nsm init`)가 이 형식으로 저장한다.

API 키는 여기 두지 않는다. keyring(Windows 자격 증명 관리자)에 둔다(nsm/config/secrets.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nsm.domain.models import ActiveWindow, Brand, Persona, Platform


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# 카드 디자인 스타일(S8, ADR 0036). clean은 기존 스타일. 세 스타일 모두 배경을 코드(CSS·SVG)로 그린다.
CardStyle = Literal["clean", "photo", "illust"]
CARD_STYLES: tuple[str, ...] = get_args(CardStyle)


class BrandSettings(Brand):
    """설정 파일의 brand. 도메인 Brand(동결)에 카드 스타일 선택만 더한다. 덱 JSON 계약은 그대로다."""

    card_style: CardStyle = "clean"


class CollectSettings(_Model):
    naver: bool = True
    google_trends: bool = True
    community: bool = False
    community_urls: list[str] = Field(default_factory=list)  # {query} 자리표시자 포함 검색 URL
    upload_dir: str | None = None  # 기본: <NSM_HOME>/uploads
    # 네이버 검색 기간: 최신순으로 최근 1d/1w/1m만, all은 기간 없이 정확도순(ADR 0032)
    recent_period: Literal["1d", "1w", "1m", "all"] = "1w"
    per_source_limit: int = 30  # 소스마다 한 번에 모을 최대 건수(주제×탭으로 나눔)
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
    image: str = "gpt-image-2.5-flare"


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
    desktop_notifications: bool = True  # Windows 알림. 꺼도 대시보드 안 표시와 로그는 그대로
    # auto: MotherDuck 토큰이 있으면 jev, 없으면 claude (ADR 0003)
    classifier: Literal["auto", "jev", "claude"] = "auto"
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
    collect: CollectSettings = Field(default_factory=CollectSettings)
    models: ModelSettings = Field(default_factory=ModelSettings)


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
