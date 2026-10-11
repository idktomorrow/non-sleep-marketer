"""잡 실행에 필요한 모든 것(포트·저장소·시계·설정)을 담는 상자.

엔진 잡은 `run_xxx(ctx)` 하나만 받는다. 테스트는 가짜 포트를 넣은 ctx로 잡을 부른다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nsm.config.schema import AppConfig
from nsm.domain.models import Platform
from nsm.ports import (
    CardRenderer,
    Classifier,
    Clock,
    ImageGenerator,
    Notifier,
    PlatformAdapter,
    Source,
    TextGenerator,
)
from nsm.store.repo import Repository


@dataclass
class JobContext:
    config: AppConfig
    repo: Repository
    clock: Clock
    notifier: Notifier
    sources: list[Source] = field(default_factory=list)
    relevance_classifier: Classifier | None = None
    topic_classifier: Classifier | None = None
    comment_classifier: Classifier | None = None
    classifier_name: str = "unknown"
    text_generator: TextGenerator | None = None
    image_generator: ImageGenerator | None = None
    renderer: CardRenderer | None = None
    adapters: dict[Platform, PlatformAdapter] = field(default_factory=dict)
    media_dir: Path = Path("media")
    preview_dir: Path = Path("preview")
    reports_dir: Path = Path("reports")

    @property
    def tz(self) -> str:
        return self.config.settings.timezone


@dataclass
class JobResult:
    job: str
    ok: bool = True
    skipped: str = ""  # 건너뛴 이유(활동 시간 밖 등)
    counts: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.skipped:
            return f"{self.job}: 건너뜀 ({self.skipped})"
        parts = ", ".join(f"{k}={v}" for k, v in self.counts.items())
        err = f" / 오류 {len(self.errors)}건: {self.errors[0]}" if self.errors else ""
        return f"{self.job}: {parts or '변화 없음'}{err}"
