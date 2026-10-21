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
from nsm.engine.context import Adapters, JobContext
from nsm.ports import ScenarioWriter
from nsm.store.costs import CostLedger
from nsm.store.db import Database
from nsm.store.picks import PicksStore
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


def make_adapters(
    cfg: AppConfig, secrets: SecretStore, repo: Repository, cost_sink=None
) -> Adapters:
    """키·공급자·모델 설정으로 어댑터 묶음을 만든다(S13). 설정이나 키가 바뀌면 묶음을 통째로 새로 만든다.
    묶음이 연 자원(브라우저 드라이버)은 `bundle.resources`(ExitStack)가 쥐고, 다음 묶음을 적용할 때 닫힌다."""
    stack = ExitStack()
    from nsm.adapters.classifier import make_classifiers
    from nsm.adapters.generator import (
        make_image_generator,
        make_image_studio,
        make_text_generator_with_reason,
    )
    from nsm.adapters.sources.google_trends import GoogleTrendsSource
    from nsm.adapters.sources.image_reader import make_image_reader
    from nsm.adapters.sources.upload import UploadSource

    sink = cost_sink or repo
    clock = repo.clock if hasattr(repo, "clock") else SystemClock()
    c = cfg.settings.collect
    read_image = make_image_reader(cfg, secrets, sink, paths.upload_text_dir())
    sources: list = [
        UploadSource(
            Path(c.upload_dir) if c.upload_dir else paths.uploads_dir(),
            clock,
            is_known=repo.has_url_hash,
            read_image=read_image,
        )
    ]
    if c.google_trends:
        sources.append(GoogleTrendsSource(clock, tz=cfg.settings.timezone))
    sources += _web_sources(cfg, clock, stack)
    classifiers = make_classifiers(cfg, secrets, sink)
    text_gen, reason = make_text_generator_with_reason(cfg, secrets, sink)
    bundle = Adapters(
        sources=tuple(sources),
        relevance_classifier=classifiers.relevance,
        topic_classifier=classifiers.topic,
        comment_classifier=classifiers.comment,
        classifier_name=classifiers.name,
        text_generator=text_gen,
        text_generator_reason=reason,
        image_generator=make_image_generator(cfg, secrets, sink, paths.media_dir()),
        image_studio=make_image_studio(cfg, secrets, sink, paths.media_dir()),
        # 밈 대본 작가(S14): Claude·OpenAI 텍스트 생성기가 겸한다(ScenarioWriter 메서드가 있으면)
        scenario_writer=text_gen if isinstance(text_gen, ScenarioWriter) else None,
        meme_discovery=_meme_discovery(cfg, clock, sources),
        platforms=make_platform_adapters(cfg, clock),
        read_image=read_image,
        resources=stack,
    )
    return bundle, classifiers  # type: ignore[return-value]


def _meme_discovery(cfg: AppConfig, clock, sources: list):
    """밈 탐색(S16): 네이버 수집기와 같은 브라우저 드라이버를 쓴다. 웹 수집이 꺼져 있으면 None."""
    from nsm.adapters.sources.common import Pacer
    from nsm.adapters.sources.meme_discovery import MemeDiscovery

    naver = next((s for s in sources if getattr(s, "name", "") == "naver"), None)
    if naver is None:
        return None
    c = cfg.settings.collect
    return MemeDiscovery(
        naver.driver,
        clock,
        Pacer(c.min_delay_seconds, c.max_delay_seconds),
        tuple(c.community_urls),
    )


def _adapter_settings(cfg: AppConfig) -> tuple:
    """이 값이 바뀌면 어댑터 묶음을 새로 만든다."""
    s = cfg.settings
    return (
        s.text_provider,
        s.models.model_dump_json(),
        s.classifier,
        s.image_generation,
        s.dry_run,
        tuple(s.enabled_platforms),
        tuple(sorted(s.accounts.items())),
        s.collect.model_dump_json(),
        s.timezone,
    )


def refresh_adapters(ctx: JobContext, secrets: SecretStore) -> None:
    """키나 공급자 설정이 바뀐 뒤: 새 묶음을 만들어 한 번에 바꾼다. 진행 중인 잡은 영향 없고 다음 잡부터.
    이전 묶음의 브라우저 드라이버는 apply_adapters가 닫는다."""
    bundle, classifiers = make_adapters(ctx.config, secrets, ctx.repo, ctx.cost_ledger)
    classifiers.set_on_switch(
        lambda a, b, e: ctx.notifier.notify(
            "분류기 대체", f"{a} 분류기가 실패해 {b}로 분류합니다: {str(e)[:120]}", "warn"
        )
    )
    ctx.apply_adapters(bundle)
    log.info(
        "어댑터 묶음 갱신: 글=%s, 그림=%s, 분류=%s",
        type(bundle.text_generator).__name__
        if bundle.text_generator
        else f"없음({bundle.text_generator_reason})",
        "있음" if bundle.image_studio else "없음",
        bundle.classifier_name,
    )


def make_cost_ledger(repo: Repository, workspace: str = "기본") -> CostLedger:
    """전역 비용 원장(S13): <NSM_HOME>/costs.duckdb(원장 표만, 작업 공간 스키마는 적용하지 않음).
    비어 있으면 지금 작업 공간 DB의 costs를 한 번 옮긴다. 작업 공간 이름은 build_context가 ctx에 묶어 준다."""
    ledger_db = Database(paths.costs_db_path(), apply_schema=False)
    ledger = CostLedger(
        ledger_db,
        repo.clock,
        workspace=workspace,
        mirror=repo.record_cost,  # 작업 공간 DB의 costs 표에도 사본(그 공간의 지출 표시용)
    )
    if ledger.count() == 0:
        rows = repo.db.execute("SELECT kind, provider, usd, occurred_at, ref FROM costs")
        n = ledger.import_legacy([tuple(r) for r in rows], workspace)
        if n:
            log.info("비용 원장에 예전 기록 %d건을 옮김", n)
    return ledger


def build_context(
    cfg: AppConfig,
    db: Database,
    stack: ExitStack,
    secrets: SecretStore | None = None,
    notifier=None,
    cost_ledger: CostLedger | None = None,
) -> JobContext:
    secrets = secrets or KeyringSecretStore()
    clock = SystemClock()
    repo = Repository(db, clock)
    ledger = cost_ledger or make_cost_ledger(repo, cfg.settings.preset)
    bundle, classifiers = make_adapters(cfg, secrets, repo, ledger)
    from nsm.render import get_renderer

    ctx = JobContext(
        config=cfg,
        repo=repo,
        clock=clock,
        notifier=notifier or LogNotifier(),
        cost_ledger=ledger,
        media_dir=paths.media_dir(),
        preview_dir=paths.preview_dir(),
        reports_dir=paths.reports_dir(),
        backups_dir=paths.backups_dir(),
    )
    ctx.apply_adapters(bundle)
    stack.callback(ctx.close_adapters)  # 호출자의 스택을 닫으면 지금 묶음의 드라이버도 닫힌다
    if cost_ledger is None:
        ledger.set_workspace(
            lambda: ctx.config.settings.preset
        )  # 프리셋 전환 뒤에도 지금 작업 공간으로 기록
    # 고른 분류기가 실패해 다음 것으로 넘어가면 알린다(2026-10-20 MotherDuck 체험 종료 같은 조용한 실패 방지, C27)
    classifiers.set_on_switch(
        lambda a, b, e: ctx.notifier.notify(
            "분류기 대체", f"{a} 분류기가 실패해 {b}로 분류합니다: {str(e)[:120]}", "warn"
        )
    )
    # 카드 스타일은 렌더할 때마다 현재 설정에서 읽는다: 대시보드 설정 저장이 재시작 없이 반영(S8)
    from nsm.render.character import character_art

    ctx.renderer = get_renderer(
        style=lambda: ctx.config.brand.card_style,
        character=lambda: character_art(
            ctx.config.brand
        ),  # 설정 화면에서 바꾸면 다음 렌더부터(C17)
        title_style=lambda: ctx.config.workspace.title_style,  # 작업 공간별 디자인 옵션(S14)
    )
    return ctx


def recover_stale_slots(ctx: JobContext) -> None:
    """재시작 복구(S14·S15): 멈춘 추천 슬롯과 그림 작업, 예전 승인 원고의 산출물."""
    from nsm.engine.artifacts import migrate_approved
    from nsm.engine.production import recover_image_jobs
    from nsm.store.slots import SlotStore

    n = SlotStore(ctx.repo.db, ctx.clock).recover_stale(ctx.clock.now())
    if n:
        log.info("중단된 추천 슬롯 %d개를 되돌림", n)
    queued, unknown = recover_image_jobs(ctx)
    if queued or unknown:
        log.info("그림 작업 복구: 다시 대기 %d, 응답 불명 %d", queued, unknown)
    if unknown:
        ctx.notifier.notify(
            "그림 응답 불명",
            f"보낸 뒤 결과를 못 받은 그림 요청 {unknown}건이 있습니다. 검토 화면에서 다시 요청하거나 정리하세요",
            "warn",
        )
    migrate_approved(ctx)


def _current_read_image(ctx: JobContext):
    reader = next((s.read_image for s in ctx.sources if getattr(s, "name", "") == "upload"), None)
    if reader is None:

        def _none(_p):
            raise RuntimeError("이미지를 읽을 공급자가 없습니다")

        return _none
    return reader


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
            recover_stale_slots(ctx)
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
    url = f"http://127.0.0.1:{port}"
    db = Database(workspace_db_path(cfg))  # 전환해 둔 프리셋의 DB(C29)
    stack = ExitStack()
    drivers = ExitStack()  # 브라우저는 작업 스레드에서 열리므로 닫기도 작업 스레드에서
    scheduler = None
    try:
        notifier = DesktopNotifier(url)
        secrets = KeyringSecretStore()
        ctx = build_context(cfg, db, drivers, secrets=secrets, notifier=notifier)
        notifier.enabled = lambda: ctx.config.settings.desktop_notifications
        # 모든 알림을 대시보드 알림함(종 모양)에도 남긴다(데스크톱 알림을 꺼도 남음, N2)
        from nsm.adapters.notify.recording import RecordingNotifier

        ctx.notifier = RecordingNotifier(ctx.repo, notifier)
        recover_interrupted(ctx)
        recover_interrupted_replies(ctx)
        recover_stale_slots(ctx)
        scheduler = JobScheduler(lambda: ctx, set(JOBS))
        installed = scheduler.install(cfg)
        scheduler.start()
        log.info("잡 %s 예약됨. 대시보드: %s", ", ".join(installed), url)

        def refresh() -> None:
            # 어댑터 묶음은 작업 스레드에서 새로 만든다(브라우저 드라이버·네트워크 자원은 거기서만)
            scheduler.submit(lambda: refresh_adapters(ctx, secrets))

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
            if _adapter_settings(new) != _adapter_settings(old):
                refresh()  # 공급자·모델·플랫폼 설정이 바뀌면 다음 작업부터 새 묶음(S13)

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
            # 올린 이미지 읽기는 묶음이 바뀌어도 지금 것을 쓰도록 매번 ctx에서 찾는다(S13)
            read_image=lambda p: _current_read_image(ctx)(p),
            brand_dir=paths.brand_dir(),
            job_names=frozenset(JOBS),
            renderer=ctx.renderer,
            notify=ctx.notifier.notify,
            presets=presets,
            switch_preset=lambda name: scheduler.submit(
                lambda: switch_preset(ctx, db, name, set_config, presets)
            ),
            picks=PicksStore(db, ctx.clock),  # 오늘의 추천(S11)
            picks_dir=paths.media_dir() / "picks",
            secrets=secrets,
            on_secrets_changed=refresh,
            cost_ledger=ctx.cost_ledger,
        )
        print(f"논슬립 마케터 실행 중. 대시보드: {url}  (끄려면 Ctrl+C)")
        uvicorn.run(create_app(state), host="127.0.0.1", port=port, log_level="warning")
    finally:
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
