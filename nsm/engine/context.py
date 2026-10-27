"""잡 실행에 필요한 모든 것(포트·저장소·시계·설정)을 담는 상자.

엔진 잡은 `run_xxx(ctx)` 하나만 받는다. 테스트는 가짜 포트를 넣은 ctx로 잡을 부른다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nsm.config.schema import AppConfig
from nsm.domain.models import Platform
from nsm.engine.summary import describe
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
    backups_dir: Path | None = None  # 없으면 백업하지 않는다(테스트·드라이 실행)
    # 화면 변경 실패의 원인 추정(E2, ADR 0055). 없으면 진단하지 않는다(Claude 키 없음·테스트)
    diagnoser: Any | None = None
    # 프로그램을 끄는 중인가(C33). 끄는 순간 돌던 잡의 오류는 "실패"가 아니라 "중단"으로 적는다
    shutting_down: Callable[[], bool] = field(default_factory=lambda: lambda: False)

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
    interrupted: bool = (
        False  # 오류가 "끄는 중 끊김"(Ctrl+C) 때문인가(C33). 실행 기록에 중단으로 적는다
    )

    def note(self, e: Exception) -> None:
        """예외가 중단(Ctrl+C) 때문이면 표시해 둔다."""
        if getattr(e, "code", None) == "interrupted":
            self.interrupted = True

    def summary(self) -> str:
        """사람이 읽는 한 줄(실행 기록·알림·로그). 예: "새 댓글 없음 · 인스타그램 요청 제한으로 쉬는 중(02:56 이후 다시)"."""
        return describe(self.job, self.skipped, self.counts, self.errors)
