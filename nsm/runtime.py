"""실제 어댑터로 JobContext를 조립하고 잡을 실행한다 (`nsm run`).

조립(wiring)은 여기 한 곳에만 있다. 엔진·정책은 무엇이 끼워졌는지 모른다.
"""

from __future__ import annotations

import logging
from contextlib import ExitStack
from pathlib import Path

from nsm.config import paths
from nsm.config.schema import AppConfig
from nsm.config.secrets import KeyringSecretStore, SecretStore
from nsm.config.store import load_config
from nsm.domain.clock import SystemClock
from nsm.engine.context import JobContext
from nsm.store.db import Database
from nsm.store.repo import Repository

log = logging.getLogger(__name__)


class LogNotifier:
    def notify(self, title: str, body: str, level: str) -> None:
        getattr(log, "error" if level == "error" else "warning" if level == "warn" else "info")(
            "%s: %s", title, body
        )


def _web_sources(cfg: AppConfig, clock: SystemClock, stack: ExitStack) -> list:
    from nsm.adapters.drivers import DriverUnavailableError, make_driver
    from nsm.adapters.sources.common import Pacer
    from nsm.adapters.sources.web import CommunitySource, NaverSearchSource

    s = cfg.settings
    c = s.collect
    if not (c.naver or c.community):
        return []
    profile = (
        Path(s.collect_profile_dir)
        if s.collect_profile_dir
        else paths.nsm_home() / "collect-profile"
    )
    try:
        driver = make_driver(s.browser_driver, profile, headless=s.headless_collect)
    except DriverUnavailableError as e:
        log.warning("웹 수집을 건너뜁니다: %s", e)
        return []
    stack.callback(driver.close)
    pacer = Pacer(c.min_delay_seconds, c.max_delay_seconds)
    out: list = []
    if c.naver:
        out.append(NaverSearchSource(driver, clock, pacer))
    if c.community:
        out += [CommunitySource(u, driver, clock, pacer) for u in c.community_urls]
    return out


def build_context(
    cfg: AppConfig,
    db: Database,
    stack: ExitStack,
    secrets: SecretStore | None = None,
) -> JobContext:
    from nsm.adapters.classifier import make_classifiers
    from nsm.adapters.sources.google_trends import GoogleTrendsSource
    from nsm.adapters.sources.upload import UploadSource

    secrets = secrets or KeyringSecretStore()
    clock = SystemClock()
    repo = Repository(db, clock)
    c = cfg.settings.collect
    sources: list = [
        UploadSource(
            Path(c.upload_dir) if c.upload_dir else paths.uploads_dir(),
            clock,
            is_known=repo.has_url_hash,
        )
    ]
    if c.google_trends:
        sources.append(GoogleTrendsSource(clock, tz=cfg.settings.timezone))
    sources += _web_sources(cfg, clock, stack)
    classifiers = make_classifiers(cfg, secrets, repo)
    return JobContext(
        config=cfg,
        repo=repo,
        clock=clock,
        notifier=LogNotifier(),
        sources=sources,
        relevance_classifier=classifiers.relevance,
        topic_classifier=classifiers.topic,
        comment_classifier=classifiers.comment,
        classifier_name=classifiers.name,
        media_dir=paths.media_dir(),
        preview_dir=paths.preview_dir(),
        reports_dir=paths.reports_dir(),
    )


def run_job_once(job: str) -> None:
    from nsm.engine.runner import run_job

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    cfg = load_config()
    db = Database(paths.db_path())
    try:
        with ExitStack() as stack:
            ctx = build_context(cfg, db, stack)
            result = run_job(job, ctx)
            print(result.summary())
            if not result.ok:
                raise SystemExit(1)
    finally:
        db.close()


def run_forever() -> None:
    raise SystemExit(
        "`nsm run`(스케줄러 + 대시보드)은 배치 F에서 구현됩니다. 지금은 `nsm run --once --job collect`"
    )
