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


def make_platform_adapters(cfg: AppConfig, clock: SystemClock) -> dict:
    """드라이런이면 미리보기 어댑터, 아니면 실계정 어댑터(배치 G)."""
    if cfg.settings.dry_run:
        from nsm.adapters.platforms.dryrun import DryRunAdapter

        return {
            p: DryRunAdapter(p, paths.preview_dir(), clock, tz=cfg.settings.timezone)
            for p in cfg.settings.enabled_platforms
        }
    log.warning(
        "실계정 어댑터는 배치 G에서 구현됩니다. 지금은 config.yaml에서 dry_run: true로 쓰세요."
    )
    return {}


def build_context(
    cfg: AppConfig,
    db: Database,
    stack: ExitStack,
    secrets: SecretStore | None = None,
    notifier=None,
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
    from nsm.adapters.generator import make_image_generator, make_text_generator
    from nsm.render import get_renderer

    return JobContext(
        config=cfg,
        repo=repo,
        clock=clock,
        notifier=notifier or LogNotifier(),
        sources=sources,
        relevance_classifier=classifiers.relevance,
        topic_classifier=classifiers.topic,
        comment_classifier=classifiers.comment,
        classifier_name=classifiers.name,
        text_generator=make_text_generator(cfg, secrets, repo),
        image_generator=make_image_generator(cfg, secrets, repo, paths.media_dir()),
        renderer=get_renderer(),
        adapters=make_platform_adapters(cfg, clock),
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
            from nsm.engine.publish import recover_interrupted

            # 이 프로세스가 DB를 연 유일한 프로세스이므로(DuckDB 잠금), 남은 publishing은 중단된 것이다
            recover_interrupted(ctx)
            result = run_job(job, ctx)
            print(result.summary())
            if not result.ok:
                raise SystemExit(1)
    finally:
        db.close()


def _setup_logging() -> None:
    paths.logs_dir().mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    from logging.handlers import RotatingFileHandler

    handlers.append(
        RotatingFileHandler(
            paths.logs_dir() / "nsm.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
    )
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers, force=True)


def run_forever() -> None:
    """한 프로세스에서 스케줄러(작업 스레드 1개)와 대시보드(uvicorn)를 함께 띄운다 (ADR 0006)."""
    import uvicorn

    from nsm.adapters.notify.windows import DesktopNotifier
    from nsm.app.server import AppState, create_app
    from nsm.config.store import save_config
    from nsm.engine.publish import recover_interrupted
    from nsm.engine.runner import JOBS
    from nsm.scheduler.app import JobScheduler

    _setup_logging()
    cfg = load_config()
    port = cfg.settings.dashboard_port
    url = f"http://127.0.0.1:{port}"
    db = Database(paths.db_path())
    stack = ExitStack()
    try:
        ctx = build_context(cfg, db, stack, notifier=DesktopNotifier(url))
        recover_interrupted(ctx)
        scheduler = JobScheduler(lambda: ctx, set(JOBS))
        installed = scheduler.install(cfg)
        scheduler.start()
        stack.callback(scheduler.shutdown)
        log.info("잡 %s 예약됨. 대시보드: %s", ", ".join(installed), url)

        def set_config(new: AppConfig) -> None:
            save_config(new)
            ctx.config = new

        state = AppState(
            repo=ctx.repo,
            clock=ctx.clock,
            get_config=lambda: ctx.config,
            set_config=set_config,
            submit=scheduler.submit,
            adapters=ctx.adapters,
            media_dir=ctx.media_dir,
            run_job_now=scheduler.run_now,
        )
        print(f"논슬립 마케터 실행 중. 대시보드: {url}  (끄려면 Ctrl+C)")
        uvicorn.run(create_app(state), host="127.0.0.1", port=port, log_level="warning")
    finally:
        stack.close()  # 스케줄러를 먼저 멈추고(잡 종료 대기) 드라이버를 닫는다
        db.close()
