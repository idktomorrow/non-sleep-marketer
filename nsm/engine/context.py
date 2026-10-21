"""잡 실행에 필요한 모든 것(포트·저장소·시계·설정)을 담는 상자.

엔진 잡은 `run_xxx(ctx)` 하나만 받는다. 테스트는 가짜 포트를 넣은 ctx로 잡을 부른다.
"""

from __future__ import annotations

import dataclasses
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from nsm.config.schema import AppConfig
from nsm.domain.models import Platform
from nsm.engine.summary import describe
from nsm.ports import (
    CardRenderer,
    Classifier,
    Clock,
    CostLedger,
    ImageGenerator,
    ImageStudio,
    Notifier,
    PlatformAdapter,
    Source,
    TextGenerator,
)
from nsm.store.repo import Repository


@dataclass(frozen=True)
class Adapters:
    """바깥 세계 어댑터 한 묶음(S13). 키·공급자·모델 설정이 바뀌면 묶음을 **새로** 만들어 통째로 바꾼다.

    잡은 시작할 때 묶음을 스냅샷으로 받아 끝까지 그것만 쓴다(실행 중 어댑터가 바뀌지 않는다).
    """

    sources: tuple[Source, ...] = ()
    relevance_classifier: Classifier | None = None
    topic_classifier: Classifier | None = None
    comment_classifier: Classifier | None = None
    classifier_name: str = "unknown"
    text_generator: TextGenerator | None = None
    text_generator_reason: str = ""  # 생성기가 없을 때 사람이 읽을 이유(어느 키가 없는지)
    image_generator: ImageGenerator | None = None
    image_studio: ImageStudio | None = None
    platforms: dict[Platform, PlatformAdapter] = field(default_factory=dict)
    read_image: Any = None  # 올린 이미지 → 글(없으면 None)


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
    text_generator_reason: str = ""
    image_generator: ImageGenerator | None = None
    image_studio: ImageStudio | None = None
    renderer: CardRenderer | None = None
    adapters: dict[Platform, PlatformAdapter] = field(default_factory=dict)
    cost_ledger: CostLedger | None = (
        None  # 전역 비용 원장(S13). 없으면 작업 공간 DB의 costs 표로 센다
    )
    media_dir: Path = Path("media")
    preview_dir: Path = Path("preview")
    reports_dir: Path = Path("reports")
    backups_dir: Path | None = None  # 없으면 백업하지 않는다(테스트·드라이 실행)
    # 묶음 교체와 스냅샷이 겹치지 않게. 잡이 보는 ctx는 snapshot()으로 만든 복사본이라 교체의 영향을 받지 않는다
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    @property
    def tz(self) -> str:
        return self.config.settings.timezone

    # ------------------------------------------------------------ 어댑터 묶음(S13)
    def apply_adapters(self, bundle: Adapters) -> None:
        """새 묶음으로 교체한다. 진행 중인 잡은 자기 스냅샷을 그대로 쓰고, 다음 잡부터 새 묶음을 본다."""
        with self.lock:
            self.sources = list(bundle.sources)
            self.relevance_classifier = bundle.relevance_classifier
            self.topic_classifier = bundle.topic_classifier
            self.comment_classifier = bundle.comment_classifier
            self.classifier_name = bundle.classifier_name
            self.text_generator = bundle.text_generator
            self.text_generator_reason = bundle.text_generator_reason
            self.image_generator = bundle.image_generator
            self.image_studio = bundle.image_studio
            self.adapters = dict(bundle.platforms)

    def snapshot(self) -> JobContext:
        """잡 하나가 끝까지 쓸 고정 컨텍스트: 지금 설정과 어댑터 묶음의 얕은 복사. repo·clock·renderer는 공유."""
        with self.lock:
            return dataclasses.replace(
                self, sources=list(self.sources), adapters=dict(self.adapters)
            )

    # ------------------------------------------------------------ 비용(S13: 전역 원장)
    def spent_between(self, start: datetime, end: datetime) -> float:
        if self.cost_ledger is not None:
            return self.cost_ledger.spent_between(start, end)
        return self.repo.cost_between(start, end)


@dataclass
class JobResult:
    job: str
    ok: bool = True
    skipped: str = ""  # 건너뛴 이유(활동 시간 밖 등)
    counts: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """사람이 읽는 한 줄(실행 기록·알림·로그). 예: "새 댓글 없음 · 인스타그램 요청 제한으로 쉬는 중(02:56 이후 다시)"."""
        return describe(self.job, self.skipped, self.counts, self.errors)
