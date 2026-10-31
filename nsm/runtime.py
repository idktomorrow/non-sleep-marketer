"""실제 어댑터로 JobContext를 조립하고 잡을 실행한다 (`nsm run`).

조립(wiring)은 여기 한 곳에만 있다. 엔진·정책은 무엇이 끼워졌는지 모른다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

from nsm.config import paths
from nsm.config.presets import PresetStore, workspace_db_path
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
    from nsm.adapters.sources.google_news import GoogleNewsSource
    from nsm.adapters.sources.web import CommunitySource, NaverSearchSource, fetch_article

    s = cfg.settings
    c = s.collect
    pacer = Pacer(c.min_delay_seconds, c.max_delay_seconds)

    def gnews(body_fetcher=None):
        src = GoogleNewsSource(
            clock, body_fetcher=body_fetcher, pacer=pacer, recent=c.recent_period
        )
        stack.callback(src.close)
        return src

    if not (c.naver or c.community):
        # 브라우저 없이도 구글 뉴스는 된다(원문은 HTTP로 읽는다)
        return [gnews()] if c.google_news else []
    profile = (
        Path(s.collect_profile_dir)
        if s.collect_profile_dir
        else paths.nsm_home() / "collect-profile"
    )
    try:
        driver = make_driver(s.browser_driver, profile, headless=s.headless_collect)
    except DriverUnavailableError as e:
        log.warning("웹 수집을 건너뜁니다: %s", e)
        return [gnews()] if c.google_news else []
    stack.callback(driver.close)
    out: list = []
    if c.google_news:
        # 원문은 네이버와 같은 브라우저 읽기로(기사 본문 추출기 공유, ADR 0022)
        out.append(gnews(lambda url: fetch_article(driver, pacer, url)))
    if c.naver:
        out.append(NaverSearchSource(driver, clock, pacer, recent=c.recent_period))
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
    return make_live_adapters(cfg, clock)


def make_live_adapters(cfg: AppConfig, clock: SystemClock) -> dict:
    """실계정 어댑터(배치 G). settings.accounts에 핸들이 없는 플랫폼은 만들지 않는다(게시 안 함)."""
    from nsm.adapters.platforms.instagram import InstagramAdapter
    from nsm.adapters.platforms.threads import ThreadsAdapter

    live = {"threads": ThreadsAdapter, "instagram": InstagramAdapter}
    out = {}
    for p in cfg.settings.enabled_platforms:
        cls = live.get(p)
        handle = cfg.settings.accounts.get(p)
        if cls is None:
            log.warning("%s 실계정 어댑터는 아직 없습니다. 이 플랫폼은 게시하지 않습니다.", p)
        elif not handle:
            log.warning("%s 게시 계정이 settings.accounts에 없어 실계정 게시를 하지 않습니다.", p)
        else:
            out[p] = cls(handle, clock, media_dir=paths.media_dir())
    return out


def make_secret_parts(cfg: AppConfig, secrets: SecretStore, repo: Repository) -> dict:
    """API 키를 쓰는 부품을 한 번에 만든다. 처음 켤 때(build_context)와 키를 바꿀 때(refresh) 같은 길(C35)."""
    from nsm.adapters.classifier import make_classifiers
    from nsm.adapters.diagnosis import make_diagnoser
    from nsm.adapters.generator import make_image_generator, make_text_generator
    from nsm.adapters.sources.image_reader import make_image_reader
    from nsm.rag.answer import make_answerer
    from nsm.rag.gemini import make_embedder

    return {
        "classifiers": make_classifiers(cfg, secrets, repo),
        "text_generator": make_text_generator(cfg, secrets, repo),
        "image_generator": make_image_generator(cfg, secrets, repo, paths.media_dir()),
        "diagnoser": make_diagnoser(cfg, secrets, repo),
        "read_image": make_image_reader(cfg, secrets, repo, paths.upload_text_dir()),
        "embedder": make_embedder(cfg, secrets, repo),  # 문서 색인·검색(R2)
        "answerer": make_answerer(cfg, secrets, repo),  # 문서 질문 답변(R3)
    }


def _install_secret_parts(ctx: JobContext, parts: dict) -> None:
    """만든 부품을 ctx에 끼운다. 다 만든 뒤에 한꺼번에(반쯤 바뀐 상태가 남지 않게)."""
    classifiers = parts["classifiers"]
    # 고른 분류기가 실패해 다음 것으로 넘어가면 알린다(2026-10-20 MotherDuck 체험 종료 같은 조용한 실패 방지, C27)
    classifiers.set_on_switch(
        lambda a, b, e: ctx.notifier.notify(
            "분류기 대체", f"{a} 분류기가 실패해 {b}로 분류합니다: {str(e)[:120]}", "warn"
        )
    )
    ctx.relevance_classifier = classifiers.relevance
    ctx.topic_classifier = classifiers.topic
    ctx.comment_classifier = classifiers.comment
    ctx.classifier_name = classifiers.name
    ctx.text_generator = parts["text_generator"]
    ctx.image_generator = parts["image_generator"]
    ctx.diagnoser = parts["diagnoser"]
    from nsm.rag.budget import close_embedder

    if ctx.embedder is not parts["embedder"]:
        close_embedder(ctx.embedder)  # 옛 HTTP 연결을 닫는다(키를 여러 번 바꿔도 새지 않게)
    ctx.embedder = parts["embedder"]
    if ctx.embedder is not None:
        ctx.embedder.should_stop = lambda: ctx.shutting_down()  # 끄는 중이면 질문의 기다림도 끊는다
    if ctx.answerer is not parts["answerer"]:
        old = getattr(getattr(ctx.answerer, "client", None), "close", None)
        if old:
            old()  # 옛 Claude 연결도 닫는다
    ctx.answerer = parts["answerer"]
    for src in ctx.sources:
        if src.name == "upload":
            src.read_image = parts["read_image"]


def build_context(
    cfg: AppConfig,
    db: Database,
    stack: ExitStack,
    secrets: SecretStore | None = None,
    notifier=None,
) -> JobContext:
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
    from nsm.render import get_renderer

    ctx = JobContext(
        config=cfg,
        repo=repo,
        clock=clock,
        notifier=notifier or LogNotifier(),
        sources=sources,
        adapters=make_platform_adapters(cfg, clock),
        media_dir=paths.media_dir(),
        preview_dir=paths.preview_dir(),
        reports_dir=paths.reports_dir(),
        backups_dir=paths.backups_dir(),
    )
    _install_secret_parts(ctx, make_secret_parts(cfg, secrets, repo))
    # 카드 스타일은 렌더할 때마다 현재 설정에서 읽는다: 대시보드 설정 저장이 재시작 없이 반영(S8)
    from nsm.render.character import character_art

    ctx.renderer = get_renderer(
        style=lambda: ctx.config.brand.card_style,
        character=lambda: character_art(
            ctx.config.brand
        ),  # 설정 화면에서 바꾸면 다음 렌더부터(C17)
    )
    return ctx


def refresh_secret_parts(ctx: JobContext, secrets: SecretStore) -> Callable | None:
    """API 키가 바뀐 뒤 키를 쓰는 부품만 새로 만든다(C35). 작업 스레드에서 부른다(돌던 잡과 겹치지 않게).

    플랫폼 어댑터·수집기는 키를 안 쓰므로 그대로. 다 만든 뒤 한꺼번에 끼우므로, 만들다 실패하면 예전 부품이 그대로 남는다.
    새 이미지 읽기 함수를 돌려줘 대시보드 상태도 맞춘다.
    """
    parts = make_secret_parts(ctx.config, secrets, ctx.repo)
    _install_secret_parts(ctx, parts)
    log.info(
        "API 키가 바뀌어 분류기·생성기·진단을 새로 만들었습니다(분류기: %s)", ctx.classifier_name
    )
    return parts["read_image"]


def switch_preset(
    ctx: JobContext,
    db: Database,
    name: str,
    save: Callable[[AppConfig], None],
    store: PresetStore | None = None,
) -> str:
    """프리셋 전환(C29). 작업 스레드에서 부른다(다른 잡과 겹치지 않게).

    1) 지금 프리셋을 그 이름으로 저장해 두고(바꾼 설정이 사라지지 않게)
    2) 대상 프리셋의 페르소나·브랜드를 입힌 설정을 저장·반영한 뒤
    3) DB를 그 프리셋 파일로 갈아끼운다. 실패하면 설정을 되돌려 설정과 DB가 어긋나지 않게 한다.
    4) 새 DB에 남은 중단 작업을 복구한다.
    """
    from nsm.engine.publish import recover_interrupted
    from nsm.engine.reply import recover_interrupted_replies

    store = store or PresetStore()
    old = ctx.config
    cur = old.settings.preset
    if store.exists(name) and store.stored_name(name) == cur:
        return cur
    store.save(old, cur)
    new = store.load_into(old, name)
    save(new)
    try:
        db.switch(store.db_path(new.settings.preset))
    except Exception:
        save(old)  # DB는 그대로이므로 설정도 원래대로
        raise
    recover_interrupted(ctx)
    recover_interrupted_replies(ctx)
    log.info("프리셋 전환: %s → %s (DB %s)", cur, new.settings.preset, db.path)
    ctx.notifier.notify(
        "프리셋 전환",
        f"'{new.settings.preset}'으로 바꿨습니다. 다음 작업부터 이 페르소나로 돕니다",
        "info",
    )
    return new.settings.preset


def run_job_once(job: str, arg: str | None = None) -> None:
    from nsm.engine.runner import run_job

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    cfg = load_config()
    db = Database(workspace_db_path(cfg))  # 전환해 둔 프리셋의 DB(C29)
    try:
        with ExitStack() as stack:
            ctx = build_context(cfg, db, stack)
            from nsm.engine.publish import recover_interrupted
            from nsm.engine.reply import recover_interrupted_replies

            # 이 프로세스가 DB를 연 유일한 프로세스이므로(DuckDB 잠금), 남은 publishing은 중단된 것이다
            recover_interrupted(ctx)
            recover_interrupted_replies(ctx)
            result = run_job(job, ctx, arg=arg)
            print(f"{result.job}: {result.summary()}")
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
    import threading

    import uvicorn

    from nsm.adapters.notify.windows import DesktopNotifier
    from nsm.app.server import AppState, create_app
    from nsm.config.store import save_config
    from nsm.engine.publish import recover_interrupted
    from nsm.engine.reply import recover_interrupted_replies
    from nsm.engine.runner import JOBS
    from nsm.scheduler.app import JobScheduler

    _setup_logging()
    cfg = load_config()
    port = cfg.settings.dashboard_port
    stopping = threading.Event()  # 끄는 중 표시(C33): 그 순간 돌던 잡의 오류는 "중단"으로 적는다
    url = f"http://127.0.0.1:{port}"
    db = Database(workspace_db_path(cfg))  # 전환해 둔 프리셋의 DB(C29)
    stack = ExitStack()
    drivers = ExitStack()  # 브라우저는 작업 스레드에서 열리므로 닫기도 작업 스레드에서
    scheduler = None
    try:
        notifier = DesktopNotifier(url)
        secrets = KeyringSecretStore()
        ctx = build_context(cfg, db, drivers, secrets=secrets, notifier=notifier)
        ctx.shutting_down = stopping.is_set
        notifier.enabled = lambda: ctx.config.settings.desktop_notifications
        # 모든 알림을 대시보드 알림함(종 모양)에도 남긴다(데스크톱 알림을 꺼도 남음, N2)
        from nsm.adapters.notify.recording import RecordingNotifier

        ctx.notifier = RecordingNotifier(ctx.repo, notifier)
        recover_interrupted(ctx)
        recover_interrupted_replies(ctx)
        scheduler = JobScheduler(lambda: ctx, set(JOBS))
        installed = scheduler.install(cfg)
        scheduler.start()
        log.info("잡 %s 예약됨. 대시보드: %s", ", ".join(installed), url)

        def set_config(new: AppConfig) -> None:
            save_config(new)
            old, ctx.config = ctx.config, new
            if (new.persona.active_hours, new.settings.reply_interval_minutes) != (
                old.persona.active_hours,
                old.settings.reply_interval_minutes,
            ):
                log.info(
                    "활동 시간이 바뀌어 예약을 다시 겁니다: %s",
                    ", ".join(scheduler.reschedule(new)),
                )

        presets = PresetStore()
        state = AppState(
            repo=ctx.repo,
            clock=ctx.clock,
            get_config=lambda: ctx.config,
            set_config=set_config,
            submit=scheduler.submit,
            adapters=ctx.adapters,
            media_dir=ctx.media_dir,
            uploads_dir=Path(cfg.settings.collect.upload_dir)
            if cfg.settings.collect.upload_dir
            else paths.uploads_dir(),
            reports_dir=ctx.reports_dir,
            run_job_now=scheduler.run_now,
            run_keyword_now=lambda kw: scheduler.run_now("keyword", kw),
            read_image=next((s.read_image for s in ctx.sources if s.name == "upload"), None),
            brand_dir=paths.brand_dir(),
            job_names=frozenset(JOBS),
            renderer=ctx.renderer,
            notify=ctx.notifier.notify,
            presets=presets,
            switch_preset=lambda name: scheduler.submit(
                lambda: switch_preset(ctx, db, name, set_config, presets)
            ),
            secrets=secrets,
            get_rag=lambda: (ctx.embedder, ctx.answerer),  # 문서 질문(R3): 키를 바꾸면 새 것
        )

        def on_secrets_changed() -> None:
            # 작업 스레드에서 다시 만든다: 돌던 잡은 옛 부품으로 끝나고 다음 작업부터 새 키(C35)
            def _refresh() -> None:
                state.read_image = refresh_secret_parts(ctx, secrets)

            def _done(f) -> None:  # 실패를 삼키지 않는다(리뷰): 예전 부품이 그대로 남았다고 알린다
                if (e := f.exception()) is not None:
                    log.error("API 키 반영 실패: %s", type(e).__name__)
                    ctx.notifier.notify(
                        "API 키 반영 실패",
                        f"새 키로 바꾸지 못해 예전 설정으로 계속합니다. nsm run을 다시 켜 주세요({type(e).__name__})",
                        "error",
                    )

            scheduler.submit(_refresh).add_done_callback(_done)

        state.on_secrets_changed = on_secrets_changed
        print(f"논슬립 마케터 실행 중. 대시보드: {url}  (끄려면 Ctrl+C)")
        server = uvicorn.Server(
            uvicorn.Config(create_app(state), host="127.0.0.1", port=port, log_level="warning")
        )
        _orig_exit = server.handle_exit

        def _handle_exit(
            sig, frame
        ):  # Ctrl+C를 받는 즉시 "끄는 중"을 켠다(작업 스레드가 바로 알게, C33)
            stopping.set()
            _orig_exit(sig, frame)

        server.handle_exit = _handle_exit
        server.run()
    finally:
        stopping.set()
        if scheduler is not None:
            try:
                scheduler.submit(drivers.close).result(timeout=60)
            except Exception:  # noqa: BLE001
                log.exception("브라우저 종료 실패")
            scheduler.shutdown()
        else:
            drivers.close()
        stack.close()
        db.close()
