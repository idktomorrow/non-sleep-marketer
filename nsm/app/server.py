"""로컬 대시보드 (FastAPI + Jinja2, 127.0.0.1에서만).

- 화면 디자인은 배치 I(ADR 0025): 밝은 관리 화면, Pretendard, 브랜드 주 색을 강조색으로.
  라우트·폼 이름·데이터 계약은 그대로 두고(tests/app이 지킴), 내부 값은 화면에서만 한국어로 바꾼다(ko.py).
- 모든 POST는 CSRF 토큰을 검사한다. 다른 웹사이트가 사용자의 브라우저로 localhost에 몰래 요청을 보내
  콘텐츠를 승인하는 일을 막기 위해서다.
- 플랫폼 동작(직접 답글)은 `submit`으로 잡 스레드에서 실행한다(Playwright 스레드 제약).
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from nsm.adapters.sources.image_reader import is_image
from nsm.app.card_editor import (
    MAX_UPLOAD_BYTES,
    EditError,
    apply_slide_edits,
    clear_rendered,
    drop_old_upload,
    save_upload,
    set_background,
)
from nsm.app.ko import ALL_JOBS as RUN_LABELS
from nsm.app.ko import FILTERS as KO_FILTERS
from nsm.app.ko import JOBS as JOB_LABELS
from nsm.app.ko import result_text
from nsm.app.materials import (
    MAX_FILE_BYTES,
    MaterialError,
    delete_file,
    list_files,
    save_material,
    source_label,
    verdict,
)
from nsm.app.run_view import RUN_WINDOW, build_rows, job_statuses
from nsm.app.settings_form import (
    CARD_STYLE_NAMES,
    PERIODS,
    SettingsError,
    apply_settings_form,
    form_values,
)
from nsm.config.schema import AppConfig
from nsm.domain.models import Platform
from nsm.domain.policy import find_banned, local, local_day_bounds
from nsm.engine.generate import AUTO_PUBLISH_KEY, all_texts
from nsm.engine.keyword import KeywordError, clean_keyword
from nsm.engine.publish import PAUSED_KEY, labeled_deck
from nsm.engine.watchdog import STALLS_KEY
from nsm.ports import CardRenderer, Clock, PlatformAdapter
from nsm.store import queries
from nsm.store.repo import InvalidTransitionError, Repository

HERE = Path(__file__).resolve().parent
MAX_MANUAL_RUNS = 10
PREVIEW_CHARS = 200  # 올린 이미지에서 옮긴 글을 올림 안내에 이만큼 보여 준다(C16)
# 화면 모드(C13): 간단(일반 사용자, 기본) / 고급(개발자). 보이는 것만 다르고 기능은 같다
UI_MODE_KEY = "ui_mode"
UI_MODES = ("simple", "advanced")
# 간단 모드 메뉴에서 빼는 화면(주소로는 그대로 열린다)
ADVANCED_PATHS = ("/materials", "/logs")
# 간단 모드 "오늘"의 지금 실행 버튼
SIMPLE_JOBS = ("collect", "generate", "reply")


@dataclass
class ManualRun:
    """대시보드 "지금 실행"으로 부른 작업 하나(진행 표시와 끝난 뒤 알림용, ADR 0030)."""

    id: str
    name: str
    started: datetime
    future: Future | None = None
    finished: datetime | None = None
    ok: bool = True
    text: str = ""
    dismissed: bool = False  # 사람이 x를 눌러 닫음
    detail: str = ""  # 진행 표시에 이름과 함께 보일 값(키워드 원고의 키워드)


def _local_path(referer: str | None) -> str | None:
    """같은 대시보드(127.0.0.1·localhost)에서 온 요청이면 그 경로. 다른 곳이면 None(열린 리다이렉트 방지)."""
    if not referer:
        return None
    parts = urlsplit(referer)
    if parts.hostname not in ("127.0.0.1", "localhost", "testserver"):
        return None
    path = parts.path or "/"
    return path if path.startswith("/") and not path.startswith("//") else None


@dataclass
class AppState:
    repo: Repository
    clock: Clock
    get_config: Callable[[], AppConfig]
    set_config: Callable[[AppConfig], None]
    submit: Callable[[Callable[[], Any]], Future]
    adapters: dict[Platform, PlatformAdapter]
    media_dir: Path
    reports_dir: Path | None = None
    csrf_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    run_job_now: Callable[[str], Future] | None = None
    job_names: frozenset[str] = frozenset()
    renderer: CardRenderer | None = None  # 카드 미리보기(M16). 없으면 미리보기 버튼이 안내만 한다
    notify: Callable[[str, str, str], None] | None = None  # 데스크톱 알림(제목, 내용, 수준)
    uploads_dir: Path | None = None  # 직접 올린 자료 폴더(자료 화면, M18)
    manual_runs: list[ManualRun] = field(default_factory=list)
    run_keyword_now: Callable[[str], Future] | None = None  # 키워드 원고(C15)
    read_image: Callable[[Path], str] | None = None  # 올린 이미지 읽기(C16). 없으면 이미지는 거절


def create_app(state: AppState) -> FastAPI:
    app = FastAPI(title="논슬립 마케터", docs_url=None, redoc_url=None)
    # DNS 리바인딩 방어: 다른 도메인 이름으로 들어온 요청은 거부한다(토큰 탈취 → CSRF 우회 차단)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    # 카드 렌더러와 같은 Pretendard를 대시보드도 쓴다(nsm/templates/fonts)
    app.mount(
        "/fonts", StaticFiles(directory=str(HERE.parent / "templates" / "fonts")), name="fonts"
    )
    templates.env.filters.update(KO_FILTERS)
    templates.env.globals["JOBS"] = JOB_LABELS

    def tz() -> str:
        return state.get_config().settings.timezone

    def fmt(dt) -> str:
        return local(dt, tz()).strftime("%m-%d %H:%M") if dt else "-"

    templates.env.filters["kst"] = fmt

    def safe_url(u: str | None) -> str:
        """수집한 링크는 신뢰할 수 없다: http(s)·file만 링크로 쓴다(javascript: 차단)."""
        if u and u.split(":", 1)[0].lower() in ("http", "https", "file"):
            return u
        return "#"

    templates.env.filters["safe_url"] = safe_url

    def theme(cfg: AppConfig) -> dict[str, str]:
        """브랜드 주 색을 강조색으로. 밝은 바탕에서 읽히도록 어둡게 맞춘 색(primary_ink)을 쓴다."""
        try:
            from nsm.render.html_renderer import palette

            c = palette(cfg.brand.primary_color, cfg.brand.accent_color)
            return {"accent": c["primary_ink"], "accent_soft": c["card"]}
        except Exception:  # noqa: BLE001  색 값이 이상해도 화면은 떠야 한다
            return {"accent": "#1E3A5F", "accent_soft": "#F0F3F7"}

    def pending_count() -> int:
        return int(
            state.repo.db.execute("SELECT count(*) FROM contents WHERE status = 'pending_review'")[
                0
            ][0]
        )

    def oldest_pending() -> str | None:
        rows = state.repo.db.execute(
            "SELECT id FROM contents WHERE status = 'pending_review' ORDER BY created_at, rowid LIMIT 1"
        )
        return rows[0][0] if rows else None

    def queue_count() -> int:
        return int(
            state.repo.db.execute("SELECT count(*) FROM contents WHERE status = 'approved'")[0][0]
        )

    def ui_mode() -> str:
        mode = state.repo.get_setting(UI_MODE_KEY, "simple")
        return mode if mode in UI_MODES else "simple"

    def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        cfg = state.get_config()
        return templates.TemplateResponse(
            request,
            name,
            {
                "csrf": state.csrf_token,
                "cfg": cfg,
                "theme": theme(cfg),
                "pending_count": pending_count(),
                "auto_publish": bool(state.repo.get_setting(AUTO_PUBLISH_KEY, False)),
                "paused": state.repo.get_setting(PAUSED_KEY, []),
                "has_reports": state.reports_dir is not None,
                "js": job_status(),
                "queue_count": queue_count(),
                "simple": ui_mode() == "simple",
                "bell": {
                    "unread": state.repo.unread_notification_count(),
                    "items": state.repo.list_notifications(15),
                },
                **ctx,
            },
        )

    async def check_csrf(request: Request) -> None:
        form = await request.form()
        if not secrets.compare_digest(str(form.get("csrf", "")), state.csrf_token):
            raise HTTPException(status_code=403, detail="CSRF 토큰이 맞지 않습니다")

    def back(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    # ------------------------------------------------------------------ 개요·스위치

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        now = state.clock.now()
        start, end = local_day_bounds(now, tz())
        ov = queries.overview(state.repo.db, start, end)
        runs, _ = build_rows(state.repo.recent_runs(RUN_WINDOW), show_all=False)
        # 곧 다시 시도할 것부터(대표로 보이는 오류가 매번 바뀌지 않게)
        failed = sorted(
            state.repo.publications_in_status("failed"),
            key=lambda p: (p.next_attempt_at is None, p.next_attempt_at, p.id),
        )
        cap = state.get_config().settings.daily_cost_cap_usd
        return page(
            request,
            "overview.html",
            ov=ov,
            human_approved=state.repo.count_human_approved(),
            runs=runs[:10],  # 변화가 있던 실행만, 같은 결과는 묶어서(C11)
            stalls=state.repo.get_setting(STALLS_KEY, []) or [],  # 계속 0건(C12)
            # 오늘 할 일: 가장 오래 기다린 원고부터 바로 연다
            first_pending=oldest_pending(),
            failed_pubs=failed,
            first_run=not runs and state.repo.count_contents() == 0,
            cost_ratio=min(1.0, ov["cost_today"] / cap) if cap else 0.0,
            simple_jobs=[j for j in SIMPLE_JOBS if j in JOB_LABELS],
            can_upload=state.uploads_dir is not None,
            max_mb=MAX_FILE_BYTES // (1024 * 1024),
            uploaded=request.query_params.get("uploaded"),
            chars=request.query_params.get("chars"),
            preview=request.query_params.get("preview"),
            upload_error=request.query_params.get("error"),
            kw_error=request.query_params.get("kw_error"),
            can_keyword=state.run_keyword_now is not None,
        )

    @app.post("/ui-mode", dependencies=[Depends(check_csrf)])
    def set_ui_mode(request: Request, mode: str = Form(...)) -> RedirectResponse:
        if mode not in UI_MODES:
            raise HTTPException(400, f"알 수 없는 화면 모드: {mode}")
        state.repo.set_setting(UI_MODE_KEY, mode)
        path = _local_path(request.headers.get("referer")) or "/"
        # 간단 모드로 바꿀 때 메뉴에서 사라지는 화면에 있었으면 "오늘"로
        if mode == "simple" and any(path == p or path.startswith(p + "/") for p in ADVANCED_PATHS):
            path = "/"
        return back(path)

    @app.post("/persona/toggle", dependencies=[Depends(check_csrf)])
    def toggle_persona() -> RedirectResponse:
        cfg = state.get_config()
        persona = cfg.persona.model_copy(update={"enabled": not cfg.persona.enabled})
        state.set_config(cfg.model_copy(update={"persona": persona}))
        return back("/")

    @app.post("/auto-publish/toggle", dependencies=[Depends(check_csrf)])
    def toggle_auto() -> RedirectResponse:
        cur = bool(state.repo.get_setting(AUTO_PUBLISH_KEY, False))
        state.repo.set_setting(AUTO_PUBLISH_KEY, not cur)
        return back("/")

    # ------------------------------------------------------------------ 설정 (M17)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, saved: int = 0) -> HTMLResponse:
        return page(
            request,
            "settings.html",
            periods=PERIODS,
            card_styles=CARD_STYLE_NAMES,
            style_previews=hasattr(state.renderer, "render_sample"),
            v=form_values(state.get_config()),
            error=None,
            saved=bool(saved),
        )

    @app.get("/settings/style-preview/{style}/{n}.png")
    def style_preview(style: str, n: int, primary: str = "", accent: str = "") -> FileResponse:
        """카드 스타일 견본(S8): 지금 페르소나와 화면의 색(저장 전 값도)으로 그린 표지·본문 장."""
        from nsm.app.style_preview import StylePreviewError, style_preview_png

        if not hasattr(state.renderer, "render_sample"):
            raise HTTPException(404, "카드 렌더러가 없습니다(`nsm doctor`로 크로미움 확인)")
        try:
            path = style_preview_png(
                state.renderer, state.get_config(), state.media_dir, style, n, primary, accent
            )
        except StylePreviewError as e:
            raise HTTPException(400, str(e)) from e
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "max-age=3600"})

    @app.post("/settings", dependencies=[Depends(check_csrf)], response_model=None)
    async def save_settings(request: Request) -> HTMLResponse | RedirectResponse:
        form = {k: str(v) for k, v in (await request.form()).items()}
        try:
            new = apply_settings_form(state.get_config(), form)
        except SettingsError as e:
            # 넣은 값을 그대로 다시 보여 주고 무엇이 틀렸는지 알린다(아무것도 저장하지 않음)
            resp = page(
                request,
                "settings.html",
                periods=PERIODS,
                card_styles=CARD_STYLE_NAMES,
                style_previews=hasattr(state.renderer, "render_sample"),
                v={**form_values(state.get_config()), **form},
                error=str(e),
                saved=False,
            )
            resp.status_code = 400
            return resp
        state.set_config(new)
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/jobs/{name}/run", dependencies=[Depends(check_csrf)])
    def run_now(name: str, request: Request) -> RedirectResponse:
        if state.run_job_now is None:
            raise HTTPException(503, "스케줄러가 없습니다")
        if name not in state.job_names:
            raise HTTPException(404, f"알 수 없는 잡: {name}")
        _track(name, state.run_job_now(name))
        # 누른 화면에 그대로 머물며 진행 상황을 본다(실행 기록으로 보내지 않음)
        return back(_local_path(request.headers.get("referer")) or "/")

    @app.post("/keyword-draft", dependencies=[Depends(check_csrf)])
    def keyword_draft(request: Request, keyword: str = Form("")) -> RedirectResponse:
        """키워드 원고(C15): 키워드로 최근 글을 찾아 원고 1건. 진행은 오른쪽 아래 표시로 본다."""
        if state.run_keyword_now is None:
            raise HTTPException(503, "스케줄러가 없습니다")
        path = _local_path(request.headers.get("referer")) or "/"
        try:
            kw = clean_keyword(keyword)
        except KeywordError as e:
            return back(path.split("?")[0] + "?" + urlencode({"kw_error": str(e)}))
        # 두 번 눌러도 같은 키워드를 두 번 돌리지 않는다(검색·판정 비용, 남은 자료로 두 번째 원고)
        busy = any(
            r.name == "keyword" and r.detail == kw and r.finished is None for r in state.manual_runs
        )
        if not busy:
            _track("keyword", state.run_keyword_now(kw), detail=kw)
        return back(path)

    def _track(name: str, future: Future, detail: str = "") -> None:
        run = ManualRun(
            id=secrets.token_hex(4), name=name, started=state.clock.now(), detail=detail
        )
        run.future = future
        state.manual_runs.append(run)
        del state.manual_runs[:-MAX_MANUAL_RUNS]
        future.add_done_callback(lambda f, r=run: _manual_done(r, f))

    def _manual_done(run: ManualRun, fut: Future) -> None:
        run.finished = state.clock.now()
        label = RUN_LABELS.get(run.name, (run.name, ""))[0]
        err = fut.exception()
        run.ok = err is None and bool(getattr(fut.result(), "ok", True))
        run.text = f"{type(err).__name__}: {err}"[:300] if err else result_text(fut.result())
        if state.notify:
            state.notify(
                f"{label} {'완료' if run.ok else '실패'}", run.text, "info" if run.ok else "warn"
            )

    def job_status() -> dict[str, Any]:
        """지금 실행 중이거나 최근 3분 안에 끝난 "지금 실행" 작업(화면 오른쪽 아래 알림)."""
        now = state.clock.now()
        items = []
        for r in state.manual_runs:
            if r.dismissed or (r.finished is not None and now - r.finished > timedelta(minutes=3)):
                continue
            secs = int(((r.finished or now) - r.started).total_seconds())
            items.append(
                {
                    "id": r.id,
                    "label": RUN_LABELS.get(r.name, (r.name, ""))[0]
                    + (f" '{r.detail}'" if r.detail else ""),
                    "state": "running" if r.finished is None else ("ok" if r.ok else "bad"),
                    "elapsed": f"{secs // 60}분 {secs % 60}초" if secs >= 60 else f"{secs}초",
                    "text": r.text,
                }
            )
        return {"items": items, "running": any(i["state"] == "running" for i in items)}

    @app.get("/jobs/status", response_class=HTMLResponse)
    def jobs_status(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "_jobstatus.html", {"js": job_status(), "csrf": state.csrf_token}
        )

    @app.post("/jobs/status/{rid}/dismiss", dependencies=[Depends(check_csrf)])
    def dismiss_job_status(request: Request, rid: str) -> HTMLResponse:
        for r in state.manual_runs:
            if r.id == rid and r.finished is not None:
                r.dismissed = True
        return templates.TemplateResponse(
            request, "_jobstatus.html", {"js": job_status(), "csrf": state.csrf_token}
        )

    # ------------------------------------------------------------------ 알림함 (N2)

    @app.get("/notifications", response_class=HTMLResponse)
    def notifications_page(request: Request) -> HTMLResponse:
        return page(request, "notifications.html", items=state.repo.list_notifications(200))

    @app.get("/notifications/badge", response_class=HTMLResponse)
    def notifications_badge(request: Request) -> HTMLResponse:
        n = state.repo.unread_notification_count()
        return templates.TemplateResponse(request, "_bell_badge.html", {"bell": {"unread": n}})

    @app.post("/notifications/{nid}/read", dependencies=[Depends(check_csrf)])
    def notification_read(nid: str) -> RedirectResponse:
        link = state.repo.mark_notification_read(nid)
        if link is None:
            raise HTTPException(404, "알림이 없습니다")
        # 저장된 링크는 우리가 정한 경로뿐이지만, 한 번 더 내부 경로인지 확인한다
        return back(link if link.startswith("/") and not link.startswith("//") else "/")

    @app.post("/notifications/read-all", dependencies=[Depends(check_csrf)])
    def notifications_read_all(request: Request) -> RedirectResponse:
        state.repo.mark_all_notifications_read()
        return back(_local_path(request.headers.get("referer")) or "/")

    @app.post("/notify/test", dependencies=[Depends(check_csrf)])
    def notify_test() -> RedirectResponse:
        if not state.get_config().settings.desktop_notifications:
            return back("/settings?notified=off")
        if state.notify:
            state.notify(
                "논슬립 마케터 알림 테스트",
                "이 알림이 보이면 데스크톱 알림이 켜져 있습니다",
                "info",
            )
        return back("/settings?notified=1")

    # ------------------------------------------------------------------ 검토 대기열

    @app.get("/review", response_class=HTMLResponse)
    def review_list(request: Request) -> HTMLResponse:
        items = state.repo.list_contents("pending_review")
        return page(request, "review_list.html", items=items, thumbs=_thumbs(items))

    def _thumbs(items) -> dict[str, str]:
        thumbs = {}
        for c in items:  # 표지 썸네일: 미리보기가 있으면 그것, 없으면 게시용으로 그린 카드
            for sub, route in (("previews", "/previews/"), ("cards", "/cards/")):
                if (state.media_dir / sub / c.id / "slide-01.png").is_file():
                    thumbs[c.id] = f"{route}{c.id}/slide-01.png"
                    break
        return thumbs

    # ------------------------------------------------------------------ 자료 (M18)

    MATERIAL_FILTERS = {
        "all": None,
        "naver": "naver",
        "upload": "upload",
        "trends": "google_trends",
    }

    @app.get("/materials", response_class=HTMLResponse)
    def materials(request: Request, source: str = "all") -> HTMLResponse:
        if source not in MATERIAL_FILTERS:
            source = "all"
        threshold = state.get_config().settings.relevance_threshold
        items = state.repo.list_source_items_view(MATERIAL_FILTERS[source], limit=150)
        for it in items:
            it["verdict"], it["tone"] = verdict(it, threshold)
            it["source_label"] = source_label(it["source"])
        uploads = state.repo.list_source_items_view("upload", limit=500)
        files = list_files(state.uploads_dir, uploads) if state.uploads_dir else []
        for f in files:
            if f["item"]:
                f["verdict"], f["tone"] = verdict(f["item"], threshold)
        q = request.query_params
        return page(
            request,
            "materials.html",
            items=items,
            files=files,
            source=source,
            counts=state.repo.source_counts(),
            uploads_dir=str(state.uploads_dir or ""),
            max_mb=MAX_FILE_BYTES // (1024 * 1024),
            uploaded=q.get("uploaded"),
            chars=q.get("chars"),
            preview=q.get("preview"),
            error=q.get("error"),
            deleted=q.get("deleted"),
        )

    @app.post("/materials/upload", dependencies=[Depends(check_csrf)])
    async def upload_material(
        file: UploadFile = File(...), next_path: str = Form("", alias="next")
    ) -> RedirectResponse:
        if state.uploads_dir is None:
            raise HTTPException(503, "자료 폴더가 설정되지 않았습니다")
        # 간단 모드 "오늘"에서 올리면 "오늘"로 돌아간다(C13). 그 밖의 값은 무시(열린 리다이렉트 방지)
        dest = "/?" if next_path == "/" else "/materials?"
        data = await file.read(MAX_FILE_BYTES + 1)
        try:
            # 이미지는 Claude가 읽느라 몇 초 걸린다: 이벤트 루프를 막지 않게 스레드에서
            path, chars = await run_in_threadpool(
                save_material, state.uploads_dir, file.filename or "자료", data, state.read_image
            )
        except MaterialError as e:
            return back(dest + urlencode({"error": str(e)}))
        done = {"uploaded": path.name, "chars": chars}
        if is_image(path) and state.read_image is not None:
            # 무엇을 읽었는지 바로 보여 준다(캐시에서 읽으므로 다시 돈이 들지 않음)
            text = await run_in_threadpool(state.read_image, path)
            done["preview"] = " ".join(text.split())[:PREVIEW_CHARS]
        return back(dest + urlencode(done))

    @app.post("/materials/files/delete", dependencies=[Depends(check_csrf)])
    def delete_material_file(name: str = Form(...)) -> RedirectResponse:
        if state.uploads_dir is None:
            raise HTTPException(503, "자료 폴더가 설정되지 않았습니다")
        try:
            delete_file(state.uploads_dir, name)
        except MaterialError as e:
            raise HTTPException(400, str(e)) from e
        return back("/materials?" + urlencode({"deleted": name}))

    # ------------------------------------------------------------------ 게시 대기 (N2)

    @app.get("/queue", response_class=HTMLResponse)
    def queue(request: Request) -> HTMLResponse:
        """승인돼 올라가기를 기다리는 원고와 최근 7일 게시한 원고."""
        waiting = sorted(state.repo.list_contents("approved"), key=lambda c: c.created_at)
        since = state.clock.now() - timedelta(days=7)
        done = []
        for c in state.repo.list_contents("published", limit=50):
            pubs = state.repo.publications_for_content(c.id)
            last = max((p.published_at for p in pubs if p.published_at), default=None)
            if last and last >= since:
                done.append((c, pubs, last))
        done.sort(key=lambda x: x[2], reverse=True)
        return page(
            request,
            "queue.html",
            waiting=[(c, state.repo.publications_for_content(c.id)) for c in waiting],
            done=done,
            thumbs=_thumbs(waiting + [d[0] for d in done]),
        )

    @app.post("/queue/{cid}/unapprove", dependencies=[Depends(check_csrf)])
    def unapprove(cid: str) -> RedirectResponse:
        """게시 대기 → 검토 대기(고치려고). 게시 작업은 승인된 원고만 올리므로 바로 멈춘다."""
        c = _content_or_404(cid)
        pubs = state.repo.publications_for_content(cid)
        if any(p.status == "publishing" for p in pubs):
            raise HTTPException(409, "지금 올리는 중입니다. 잠시 뒤 다시 시도하세요")
        if c.status != "approved":
            raise HTTPException(409, "게시 대기 중인 원고만 되돌릴 수 있습니다")
        try:
            state.repo.transition_content(
                cid, "pending_review", reason="사람이 게시 대기에서 되돌림"
            )
        except InvalidTransitionError as e:
            raise HTTPException(409, str(e)) from e
        return back(f"/review/{cid}")

    def _content_or_404(cid: str):
        c = state.repo.get_content(cid)
        if c is None:
            raise HTTPException(404, "콘텐츠가 없습니다")
        return c

    @app.get("/review/{cid}", response_class=HTMLResponse)
    def review_detail(request: Request, cid: str) -> HTMLResponse:
        c = _content_or_404(cid)
        cards = sorted((state.media_dir / "cards" / cid).glob("slide-*.png"))
        prev_dir = state.media_dir / "previews" / cid
        previews = sorted(prev_dir.glob("slide-*.png"))
        ov_file = prev_dir / "overflow.txt"
        if previews and not ov_file.exists():
            previews = []  # 그리는 중이거나 실패한 미리보기는 보여 주지 않는다
        overflow = ov_file.read_text("utf-8").strip() if previews else ""
        return page(
            request,
            "review_detail.html",
            c=c,
            banned=find_banned(all_texts(c.generated), state.get_config().persona),
            pubs=state.repo.publications_for_content(cid),
            sources=[
                state.repo.get_source_item(i)
                for i in {x.source_item_id for x in c.generated.claims}
            ],
            cards=[p.name for p in cards],
            previews=[p.name for p in previews],
            overflow=overflow,
            can_preview=state.renderer is not None,
            max_upload_mb=MAX_UPLOAD_BYTES // (1024 * 1024),
        )

    def _png_name_ok(name: str) -> bool:
        return (
            "/" not in name
            and "\\" not in name
            and name.startswith("slide-")
            and name.endswith(".png")
        )

    @app.get("/previews/{cid}/{name}")
    def preview_image(cid: str, name: str) -> FileResponse:
        _content_or_404(cid)  # cid도 DB에 있는 것만(경로에 쓰므로)
        if not _png_name_ok(name):
            raise HTTPException(404)
        path = state.media_dir / "previews" / cid / name
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png")

    @app.get("/cards/{cid}/{name}")
    def card_image(cid: str, name: str) -> FileResponse:
        if (
            "/" in name
            or "\\" in name
            or not name.startswith("slide-")
            or not name.endswith(".png")
        ):
            raise HTTPException(404)
        path = state.media_dir / "cards" / cid / name
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png")

    @app.post("/review/{cid}/approve", dependencies=[Depends(check_csrf)])
    def approve(cid: str) -> RedirectResponse:
        c = _content_or_404(cid)
        banned = find_banned(all_texts(c.generated), state.get_config().persona)
        if banned:
            # 금칙어가 든 콘텐츠는 사람도 그대로 승인할 수 없다: 고친 뒤 승인
            raise HTTPException(
                409, f"금칙어·금칙 패턴이 있어 승인할 수 없습니다: {', '.join(banned)}"
            )
        try:
            state.repo.transition_content(cid, "approved", approved_by="human")
        except InvalidTransitionError as e:
            raise HTTPException(409, str(e)) from e
        state.repo.requeue_abandoned(cid)
        return back("/review")

    @app.post("/review/{cid}/discard", dependencies=[Depends(check_csrf)])
    def discard(cid: str, request: Request) -> RedirectResponse:
        c = _content_or_404(cid)
        if any(p.status == "publishing" for p in state.repo.publications_for_content(cid)):
            raise HTTPException(409, "지금 올리는 중입니다. 잠시 뒤 다시 시도하세요")
        was_queued = c.status == "approved"
        try:
            state.repo.transition_content(cid, "discarded", reason="사람이 폐기")
        except InvalidTransitionError as e:
            raise HTTPException(409, str(e)) from e
        return back("/queue" if was_queued else "/review")

    def _editable(cid: str):
        c = _content_or_404(cid)
        if c.status != "pending_review":
            raise HTTPException(409, "검토 대기 중인 콘텐츠만 고칠 수 있습니다")
        return c

    @app.post("/review/{cid}/edit", dependencies=[Depends(check_csrf)])
    async def edit(cid: str, request: Request) -> RedirectResponse:
        """원고 저장: 캡션·스레드·블로그 + 슬라이드별 문구(s{번호}_{칸}). 폼에 없는 칸은 그대로 둔다."""
        c = _editable(cid)
        form = dict((await request.form()).items())
        if "cover_title" in form and "s0_title" not in form:
            form["s0_title"] = form["cover_title"]  # 예전 폼 호환
        g = c.generated
        try:
            deck = apply_slide_edits(g.deck, form)
        except EditError as e:
            raise HTTPException(400, str(e)) from e
        updates: dict[str, Any] = {"deck": deck}
        for f in ("instagram_caption", "threads_text", "blog_title", "blog_markdown"):
            if f in form:
                updates[f] = str(form[f])
        state.repo.update_generated(cid, g.model_copy(update=updates))
        # 고친 뒤 이미 렌더된 카드·미리보기는 버린다(다음 게시 때 다시 렌더)
        clear_rendered(state.media_dir, cid)
        return back(f"/review/{cid}")

    @app.post("/review/{cid}/slides/{index}/background", dependencies=[Depends(check_csrf)])
    async def upload_background(
        cid: str, index: int, file: UploadFile = File(...)
    ) -> RedirectResponse:
        c = _editable(cid)
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        try:
            if not 0 <= index < len(c.generated.deck.slides):
                raise EditError("없는 슬라이드입니다")
            path = save_upload(state.media_dir, cid, index, data)
            new = set_background(c.generated, index, str(path), state.media_dir)
        except EditError as e:
            raise HTTPException(400, str(e)) from e
        state.repo.update_generated(cid, new)
        drop_old_upload(state.media_dir, cid, c.generated.deck.slides[index].background)
        clear_rendered(state.media_dir, cid)
        return back(f"/review/{cid}")

    @app.post("/review/{cid}/slides/{index}/background/remove", dependencies=[Depends(check_csrf)])
    def remove_background(cid: str, index: int) -> RedirectResponse:
        c = _editable(cid)
        try:
            new = set_background(c.generated, index, None, state.media_dir)
        except EditError as e:
            raise HTTPException(400, str(e)) from e
        state.repo.update_generated(cid, new)
        drop_old_upload(state.media_dir, cid, c.generated.deck.slides[index].background)
        clear_rendered(state.media_dir, cid)
        return back(f"/review/{cid}")

    @app.post("/review/{cid}/preview", dependencies=[Depends(check_csrf)])
    def preview(cid: str) -> RedirectResponse:
        """지금 원고로 카드를 그려 본다. 게시용 카드(cards/)와 따로 둔다."""
        _content_or_404(cid)
        if state.renderer is None:
            raise HTTPException(503, "카드 렌더러가 없습니다(`nsm doctor`로 크로미움 확인)")
        out = state.media_dir / "previews" / cid
        renderer = state.renderer

        def render() -> None:
            # 잡 스레드에서 실행된다. 요청이 시간 초과로 먼저 끝나도 이 안에서 결과를 완결한다:
            # 실행 시점의 원고를 다시 읽고, 넘침 기록은 PNG와 함께 쓰고, 실패하면 반쯤 그린 것을 지운다.
            clear_rendered(state.media_dir, cid, cards=False)
            c = state.repo.get_content(cid)
            if c is None:
                return
            try:
                renderer.render(labeled_deck(c), out)
                overflow = list(getattr(renderer, "last_overflow", []) or [])
                (out / "overflow.txt").write_text(",".join(map(str, overflow)), encoding="utf-8")
            except Exception:
                clear_rendered(state.media_dir, cid, cards=False)
                raise

        # 잡 스레드에서 그린다: 게시 잡의 렌더와 겹쳐 넘침 결과가 섞이지 않게
        try:
            state.submit(render).result(timeout=90)
        except FutureTimeout as e:
            raise HTTPException(
                504, "다른 작업이 끝나면 미리보기를 그립니다. 잠시 뒤 새로고침하세요"
            ) from e
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"미리보기 렌더 실패: {e}") from e
        return back(f"/review/{cid}#preview")

    # ------------------------------------------------------------------ 댓글

    @app.get("/comments", response_class=HTMLResponse)
    def comments(request: Request) -> HTMLResponse:
        return page(
            request,
            "comments.html",
            escalated=state.repo.list_comments("escalated"),
            replying=state.repo.list_comments("replying"),
            recent=state.repo.list_comments(limit=50),
        )

    @app.post("/comments/{key}/resolve", dependencies=[Depends(check_csrf)])
    def resolve(key: str) -> RedirectResponse:
        if state.repo.get_comment(key) is None:
            raise HTTPException(404)
        state.repo.update_comment(key, status="resolved", handled_by="human")
        return back("/comments")

    @app.post("/comments/{key}/reply", dependencies=[Depends(check_csrf)])
    def reply(key: str, text: str = Form(...)) -> RedirectResponse:
        stored = state.repo.get_comment(key)
        if stored is None:
            raise HTTPException(404)
        adapter = state.adapters.get(stored.comment.platform)
        if adapter is None:
            raise HTTPException(503, "이 플랫폼 어댑터가 연결돼 있지 않습니다")
        if stored.status != "escalated":
            raise HTTPException(409, "이미 처리 중이거나 처리된 댓글입니다")
        # 먼저 '보내는 중'으로 바꿔 두 번 눌러도 중복 답글이 나가지 않게 한다
        state.repo.update_comment(key, status="replying", reply_text=text)

        def send() -> str:
            try:
                reply_id = adapter.reply(stored.comment, text)
            except Exception as e:  # noqa: BLE001
                state.repo.update_comment(
                    key, status="escalated", reason=f"직접 답글 실패: {e}"[:300]
                )
                raise
            state.repo.update_comment(
                key, status="resolved", reply_external_id=reply_id, handled_by="human"
            )
            return reply_id

        fut = state.submit(send)
        try:
            fut.result(timeout=120)
        except FutureTimeout:
            # 작업 스레드가 다른 잡을 처리 중: 요청은 대기열에 있고 곧 보내진다
            return back("/comments")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"답글 전송 실패: {e}") from e
        return back("/comments")

    # ------------------------------------------------------------------ 성과·로그

    @app.get("/performance", response_class=HTMLResponse)
    def performance(request: Request) -> HTMLResponse:
        now = state.clock.now()
        since = queries.default_since(now)
        return page(
            request,
            "performance.html",
            posts=queries.post_performance(state.repo.db),
            followers=queries.follower_series(state.repo.db, since, tz()),
            comment_stats=queries.comment_stats(state.repo.db, since),
            costs=queries.costs_by_day(state.repo.db, now - timedelta(days=14), tz()),
        )

    @app.get("/logs", response_class=HTMLResponse)
    def logs(request: Request, show_all: bool = Query(False, alias="all")) -> HTMLResponse:
        """?all=1(또는 true): 걸러내지 않고 최근 200줄 전부."""
        runs = state.repo.recent_runs(200 if show_all else RUN_WINDOW)
        rows, hidden = build_rows(runs, show_all)
        statuses = job_statuses(
            state.get_config(),
            state.repo.latest_runs(),
            state.repo.last_success_at(),
            state.clock.now(),
            fmt,
        )
        return page(
            request, "logs.html", runs=rows, hidden=hidden, show_all=show_all, statuses=statuses
        )

    if state.reports_dir is not None:
        from nsm.app.reports import make_router

        app.include_router(make_router(state.reports_dir, page))

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
