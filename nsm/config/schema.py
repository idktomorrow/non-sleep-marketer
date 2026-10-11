"""사용자 설정 파일(config.yaml) 스키마. 설정 마법사(`nsm init`)가 이 형식으로 저장한다.

API 키는 여기 두지 않는다. keyring(Windows 자격 증명 관리자)에 둔다(nsm/config/secrets.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nsm.domain.models import Brand, Persona, Platform


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CollectSettings(_Model):
    naver: bool = True
    google_trends: bool = True
    community: bool = False
    community_urls: list[str] = Field(default_factory=list)  # {query} 자리표시자 포함 검색 URL
    upload_dir: str | None = None  # 기본: <NSM_HOME>/uploads
    per_source_limit: int = 10
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
    enabled_platforms: list[Platform] = Field(
        default_factory=lambda: ["instagram", "threads", "naver_blog"]
    )
    quotas: dict[Platform, int] = Field(
        default_factory=lambda: {"instagram": 1, "threads": 2, "naver_blog": 1}
    )
    daily_cost_cap_usd: float = 3.0
    protect_count: int = 20
    retry_delays_minutes: list[int] = Field(default_factory=lambda: [15, 60, 240])
    relevance_threshold: float = 0.8
    reply_daily_limit_per_author: int = 3
    topic_cooldown_days: int = 14
    comment_lookback_days: int = 14
    # auto: MotherDuck 토큰이 있으면 jev, 없으면 claude (ADR 0003)
    classifier: Literal["auto", "jev", "claude"] = "auto"
    # auto: OpenAI 키가 있으면 생성, 없으면 템플릿 배경만
    image_generation: Literal["auto", "on", "off"] = "auto"
    browser_driver: Literal["aside", "playwright"] = "playwright"  # ADR 0010
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
    brand: Brand
    persona: Persona
    settings: Settings = Field(default_factory=Settings)
