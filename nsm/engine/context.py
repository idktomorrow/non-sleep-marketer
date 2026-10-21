"""잡 실행에 필요한 모든 것(포트·저장소·시계·설정)을 담는 상자.

엔진 잡은 `run_xxx(ctx)` 하나만 받는다. 테스트는 가짜 포트를 넣은 ctx로 잡을 부른다.
"""

from __future__ import annotations

import dataclasses
import logging
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
    ScenarioWriter,
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
    scenario_writer: ScenarioWriter | None = None  # 밈 대본(S14). 보통 text_generator가 겸한다
    meme_discovery: Any = None  # 밈 사례 탐색(S16). 웹 수집이 꺼져 있으면 None(창작 밈으로)
    meme_formats: Any = None  # 밈 형식 탐색(S17). 없으면 2컷 대본 경로
    platforms: dict[Platform, PlatformAdapter] = field(default_factory=dict)
    read_image: Any = None  # 올린 이미지 → 글(없으면 None)
    resources: Any = None  # 이 묶음이 연 자원(브라우저 드라이버 등)을 닫는 ExitStack. 묶음을 바꿀 때 이전 것을 닫는다


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
    scenario_writer: ScenarioWriter | None = None
    meme_discovery: Any = None
    meme_formats: Any = None
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
        """새 묶음으로 교체한다. 진행 중인 잡은 자기 스냅샷을 그대로 쓰고, 다음 잡부터 새 묶음을 본다.
        이전 묶음이 연 자원(브라우저 드라이버)은 여기서 닫는다(갱신할 때마다 브라우저가 늘지 않게)."""
        with self.lock:
            old = getattr(self, "_resources", None)
            self._resources = bundle.resources
            self.sources = list(bundle.sources)
            self.relevance_classifier = bundle.relevance_classifier
            self.topic_classifier = bundle.topic_classifier
            self.comment_classifier = bundle.comment_classifier
            self.classifier_name = bundle.classifier_name
            self.text_generator = bundle.text_generator
            self.text_generator_reason = bundle.text_generator_reason
            self.image_generator = bundle.image_generator
            self.image_studio = bundle.image_studio
            self.scenario_writer = bundle.scenario_writer
            self.meme_discovery = bundle.meme_discovery
            self.meme_formats = bundle.meme_formats
            self.adapters = dict(bundle.platforms)
        if old is not None and old is not bundle.resources:
            try:
                old.close()
            except Exception:  # noqa: BLE001  닫기 실패가 새 묶음 적용을 막지 않게
                logging.getLogger(__name__).exception("이전 어댑터 자원 닫기 실패")

    def close_adapters(self) -> None:
        """지금 묶음의 자원을 닫는다(종료 때, 작업 스레드에서)."""
        with self.lock:
            res, self._resources = getattr(self, "_resources", None), None
        if res is not None:
            res.close()

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
