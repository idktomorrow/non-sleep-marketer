"""로컬 대시보드 (FastAPI + Jinja2, 127.0.0.1에서만).

- 화면 디자인은 배치 I(ADR 0025): 밝은 관리 화면, Pretendard, 브랜드 주 색을 강조색으로.
  라우트·폼 이름·데이터 계약은 그대로 두고(tests/app이 지킴), 내부 값은 화면에서만 한국어로 바꾼다(ko.py).
- 모든 POST는 CSRF 토큰을 검사한다. 다른 웹사이트가 사용자의 브라우저로 localhost에 몰래 요청을 보내
  콘텐츠를 승인하는 일을 막기 위해서다.
- 플랫폼 동작(직접 답글)은 `submit`으로 잡 스레드에서 실행한다(Playwright 스레드 제약).
"""

from __future__ import annotations

import hashlib
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

from nsm import app_version
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
from nsm.app.keys import install_keys_routes, key_status
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
from nsm.app.next_publish import next_publishes
from nsm.app.run_view import RUN_WINDOW, build_rows, job_statuses
from nsm.app.settings_form import (
    CARD_STYLE_NAMES,
    CHARACTER_NAMES,
    PERIODS,
    SettingsError,
    apply_settings_form,
    form_values,
)
from nsm.config.presets import DEFAULT_PRESET, PresetError, PresetStore, check_name
from nsm.config.presets import same as preset_same
from nsm.config.schema import AppConfig
from nsm.config.secrets import SecretStore
from nsm.domain.models import GeneratedContent, Platform
from nsm.domain.policy import find_banned, local, local_day_bounds
from nsm.engine.generate import AUTO_PUBLISH_KEY, all_texts
from nsm.engine.keyword import KeywordError, clean_keyword
from nsm.engine.publish import PAUSED_KEY, labeled_deck
from nsm.engine.watchdog import STALLS_KEY
from nsm.ports import CardRenderer, Clock, PlatformAdapter
from nsm.render.character import character_art
from nsm.store import queries
from nsm.store.repo import InvalidTransitionError, Repository

HERE = Path(__file__).resolve().parent
MAX_MANUAL_RUNS = 10
BRAND_IMAGE_KINDS = (
    "logo_path",
    "character_cover",
    "character_outro",
)  # 설정 화면에서 올리는 그림(C17)
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
    brand_dir: Path | None = None  # 로고·캐릭터 그림 저장 폴더(C17). 없으면 올리기 칸이 안 보인다
    # 프리셋 전환(C29): 작업 스레드에서 DB까지 갈아끼우므로 Future. 없으면 프리셋 칸이 안 보인다
    switch_preset: Callable[[str], Future] | None = None
    presets: PresetStore | None = None  # 없으면 기본 위치(<NSM_HOME>/presets)
    # API 키 칸(C35): 없으면 칸이 안 보인다. 저장·삭제 뒤 키를 쓰는 부품을 다시 만드는 콜백
    secrets: SecretStore | None = None
    on_secrets_changed: Callable[[], None] | None = None
    check_key: Callable[[str, str | None], tuple[str, int | None]] | None = None  # 테스트용 대체


_ASSET_CACHE: dict[str, tuple[tuple[float, int], str]] = {}


def asset_url(name: str) -> str:
    """정적 파일 주소에 내용 지문을 붙인다. 파일이 바뀌면 주소가 바뀌어 브라우저가 옛 CSS를 쓰지 않는다
    (C20: 새 화면 규칙이 안 먹고 올린 그림이 원래 크기로 보였던 일)."""
    path = HERE / "static" / name
    try:
        st = path.stat()
    except OSError:
        return f"/static/{name}"
    key = (st.st_mtime, st.st_size)
    hit = _ASSET_CACHE.get(name)
    if hit is None or hit[0] != key:
        hit = (key, hashlib.sha256(path.read_bytes()).hexdigest()[:10])
        _ASSET_CACHE[name] = hit
    return f"/static/{name}?v={hit[1]}"


LOOPBACK = frozenset({"127.0.0.1", "::1"})  # 실패 화면은 내 PC에서 연 대시보드에만(E2)


def create_app(state: AppState) -> FastAPI:
    app = FastAPI(title="논슬립 마케터", docs_url=None, redoc_url=None)
    # DNS 리바인딩 방어: 다른 도메인 이름으로 들어온 요청은 거부한다(토큰 탈취 → CSRF 우회 차단)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals["asset"] = asset_url
    templates.env.globals["default_preset"] = DEFAULT_PRESET  # 프리셋 칸·상단 배지(C29)
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

    def paused_why(paused: list[str]) -> dict[str, str]:
        if not paused:  # 멈춘 곳이 없으면 이유를 읽지 않는다
            return {}
        from nsm.engine.pause import pause_info, short_reason

        info = pause_info(state.repo)
        return {p: short_reason(info.get(p)) for p in paused}

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
        paused = state.repo.get_setting(PAUSED_KEY, []) or []
        return templates.TemplateResponse(
            request,
            name,
            {
                "csrf": state.csrf_token,
                "cfg": cfg,
                "theme": theme(cfg),
                "pending_count": pending_count(),
                "auto_publish": bool(state.repo.get_setting(AUTO_PUBLISH_KEY, False)),
                "paused": paused,
                "paused_why": paused_why(paused),
                "has_reports": state.reports_dir is not None,
                "js": job_status(),
                "queue_count": queue_count(),
                "version": app_version(),
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
        ov = queries.overview(
            state.repo.db, start, end, state.get_config().settings.min_source_chars
        )
        next_pubs = (
            next_publishes(state.repo, state.get_config(), now, state.adapters)
            if ov["approved_waiting"]
            else []
        )
        runs, _ = build_rows(state.repo.recent_runs(RUN_WINDOW), show_all=False)
        # 곧 다시 시도할 것부터(대표로 보이는 오류가 매번 바뀌지 않게)
        failed = sorted(
            state.repo.publications_in_status("failed"),
            key=lambda p: (p.next_attempt_at is None, p.next_attempt_at, p.id),
        )
        cap = state.get_config().settings.daily_cost_cap_usd
        from nsm.app.incidents import ongoing as ongoing_incidents

        return page(
            request,
            "overview.html",
            # 진행 중인 장애만 한 줄씩(E2): "스레드: 요청 제한으로 14:00까지 쉬는 중"
            incidents_now=ongoing_incidents(state, now, lambda t: local(t, tz()).strftime("%H:%M")),
            ov=ov,
            human_approved=state.repo.count_human_approved(),
            runs=runs[:10],  # 변화가 있던 실행만, 같은 결과는 묶어서(C11)
            stalls=state.repo.get_setting(STALLS_KEY, []) or [],  # 계속 0건(C12)
            # 오늘 할 일: 가장 오래 기다린 원고부터 바로 연다
            first_pending=oldest_pending(),
            failed_pubs=failed,
            first_run=not runs and state.repo.count_contents() == 0,
            cost_ratio=min(1.0, ov["cost_today"] / cap) if cap else 0.0,
            next_pubs=next_pubs,
            now_local=local(now, tz()),
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

    def doc_index_line() -> dict[str, Any]:
        """설정 화면 한 줄(R2): 문서 색인 조각 수·벡터 없는 조각 수·마지막 실행 시각."""
        from nsm.rag.indexer import stored_stats

        st = stored_stats(state.repo.db, "docs")
        last = next((r for r in state.repo.latest_runs(30) if r["job"] == "index"), None)
        return {
            "chunks": st["chunks"],
            "without_vector": st["without_vector"],
            "last": last["started_at"] if last else None,
            "time": state.get_config().settings.rag.index_time,
        }

    def settings_extra(request: Request) -> dict[str, Any]:
        b = state.get_config().brand
        q = request.query_params
        return {
            "presets": _presets().list() if state.switch_preset else [],
            "can_presets": state.switch_preset is not None,
            "preset_msg": q.get("preset"),
            "preset_error": q.get("preset_error"),
            "periods": PERIODS,
            "card_styles": CARD_STYLE_NAMES,
            "characters": CHARACTER_NAMES,
            "style_previews": hasattr(state.renderer, "render_sample"),
            "can_brand_images": state.brand_dir is not None,
            "has_image": {
                k: bool(getattr(b, k) and Path(getattr(b, k)).is_file()) for k in BRAND_IMAGE_KINDS
            },
            "image_saved": q.get("image_saved"),
            "image_error": q.get("image_error"),
            "keys": key_status(state.secrets),
            "doc_index": doc_index_line(),
            "can_keys": state.secrets is not None,
            "key_msg": q.get("key_msg"),
            "key_error": q.get("key_error"),
            "key_check": {
                "name": q.get("key_check"),
                "text": q.get("key_check_text"),
                "ok": q.get("key_check_code") == "ok",
            },
            # 캐릭터를 바꾸면 일러스트형 견본 주소도 바뀌게(브라우저 캐시 때문에 옛 견본이 남지 않게)
            "character_fp": character_art(b).fingerprint,
        }

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, saved: int = 0) -> HTMLResponse:
        return page(
            request,
            "settings.html",
            **settings_extra(request),
            v=form_values(state.get_config()),
            error=None,
            saved=bool(saved),
        )

    def _presets() -> PresetStore:
        if state.presets is None:
            state.presets = PresetStore()
        return state.presets

    def _preset_redirect(msg: str = "", error: str = "") -> RedirectResponse:
        q = urlencode({"preset": msg} if msg else {"preset_error": error})
        return RedirectResponse(f"/settings?{q}#presets", status_code=303)

    @app.post("/settings/preset/save", dependencies=[Depends(check_csrf)])
    async def preset_save(request: Request) -> RedirectResponse:
        """지금 페르소나·브랜드를 프리셋으로 저장. 현재 프리셋이면 덮어쓰고, 새 이름이면 새 프리셋.
        다른 기존 프리셋은 덮어쓰지 않는다(그 프리셋의 DB가 다른 페르소나 것이 되지 않게)."""
        form = await request.form()
        cfg = state.get_config()
        try:
            name = check_name(str(form.get("name", "")))
            is_current = preset_same(name, cfg.settings.preset)
            _presets().save(cfg, name, overwrite=is_current)
        except PresetError as e:
            return _preset_redirect(error=str(e))
        if is_current:
            return _preset_redirect(msg=f"'{name}' 프리셋을 저장했습니다")
        return _preset_redirect(
            msg=f"'{name}' 프리셋을 만들었습니다. 바꾸려면 목록에서 고르고 '전환'을 누르세요"
        )

    @app.post("/settings/preset/switch", dependencies=[Depends(check_csrf)])
    async def preset_switch(request: Request) -> RedirectResponse:
        if state.switch_preset is None:
            raise HTTPException(status_code=404)
        form = await request.form()
        name = str(form.get("name", "")).strip()
        try:
            name = check_name(name)
            if not _presets().exists(name):
                raise PresetError(f"프리셋 '{name}'이 없습니다")
        except PresetError as e:
            return _preset_redirect(error=str(e))
        if preset_same(name, state.get_config().settings.preset):
            return _preset_redirect(msg=f"이미 '{name}' 프리셋입니다")
        # 작업 스레드에서 DB까지 갈아끼운다. 진행 중인 작업이 있으면 그 뒤에 바뀐다(오른쪽 아래 진행 표시)
        _track("preset", state.switch_preset(name), detail=name)
        return RedirectResponse("/settings#presets", status_code=303)

    @app.post("/settings/preset/delete", dependencies=[Depends(check_csrf)])
    async def preset_delete(request: Request) -> RedirectResponse:
        form = await request.form()
        name = str(form.get("name", "")).strip()
        try:
            _presets().delete(name, current=state.get_config().settings.preset)
        except PresetError as e:
            return _preset_redirect(error=str(e))
        return _preset_redirect(msg=f"'{name}' 프리셋을 지웠습니다(자료·원고·성과 포함)")

    @app.get("/settings/brand-image/{kind}.png")
    def brand_image_png(kind: str) -> FileResponse:
        """올린 로고·캐릭터 그림 보기. 설정에 적힌 경로만 보낸다(경로를 요청으로 받지 않음)."""
        if kind not in BRAND_IMAGE_KINDS:
            raise HTTPException(404)
        p = getattr(state.get_config().brand, kind)
        if not p or not Path(p).is_file():
            raise HTTPException(404)
        return FileResponse(p, headers={"Cache-Control": "no-cache"})

    @app.post("/settings/brand-image", dependencies=[Depends(check_csrf)])
    async def upload_brand_image(
        kind: str = Form(...),
        file: UploadFile = File(...),
        clear_background: str = Form(""),
    ) -> RedirectResponse:
        """로고·캐릭터 그림 올리기(C17, ADR 0046). 캐릭터는 바탕을 지우고 그림만 남게 자른다."""
        from nsm.app.brand_images import (
            MAX_BYTES,
            BrandImageError,
            drop_old,
            prepare_character,
            prepare_logo,
            save_png,
        )

        if state.brand_dir is None:
            raise HTTPException(503, "그림 저장 폴더가 설정되지 않았습니다")
        if kind not in BRAND_IMAGE_KINDS:
            raise HTTPException(400, "알 수 없는 그림 종류")
        data = await file.read(MAX_BYTES + 1)
        try:
            if kind == "logo_path":
                img = await run_in_threadpool(prepare_logo, data)
            else:
                img = await run_in_threadpool(prepare_character, data, clear_background == "on")
        except BrandImageError as e:
            return back("/settings?" + urlencode({"image_error": str(e)}) + "#brand-images")
        path = await run_in_threadpool(save_png, img, state.brand_dir, kind.removesuffix("_path"))
        cfg = state.get_config()
        old = getattr(cfg.brand, kind)
        update: dict[str, Any] = {kind: path if kind == "logo_path" else str(path)}
        if kind != "logo_path":
            update["character"] = (
                "custom"  # 올리면 바로 그 그림을 쓴다(되돌리려면 위에서 다시 고름)
            )
        state.set_config(cfg.model_copy(update={"brand": cfg.brand.model_copy(update=update)}))
        if kind != "logo_path":
            # 로고는 원고를 만들 때 덱에 경로가 들어가므로, 이미 만든 원고를 위해 예전 파일을 남긴다
            drop_old(state.brand_dir, str(old) if old else None)
        return back("/settings?" + urlencode({"image_saved": kind}) + "#brand-images")

    @app.post("/settings/brand-image/delete", dependencies=[Depends(check_csrf)])
    async def delete_brand_image(kind: str = Form(...)) -> RedirectResponse:
        # 올리기·설정 저장과 같은 이벤트 루프에서 돌려 설정 읽기→쓰기가 엇갈리지 않게(async)
        from nsm.app.brand_images import drop_old

        if state.brand_dir is None or kind not in BRAND_IMAGE_KINDS:
            raise HTTPException(400, "지울 수 없는 그림")
        cfg = state.get_config()
        old = getattr(cfg.brand, kind)
        update: dict[str, Any] = {kind: None}
        b = cfg.brand
        others = [k for k in ("character_cover", "character_outro") if k != kind and getattr(b, k)]
        if kind != "logo_path" and b.character == "custom" and not others:
            update["character"] = "none"  # 올린 그림이 하나도 안 남으면 캐릭터 없음으로
        state.set_config(cfg.model_copy(update={"brand": b.model_copy(update=update)}))
        if kind != "logo_path":  # 로고 파일은 이미 만든 원고가 쓰므로 남긴다
            drop_old(state.brand_dir, str(old) if old else None)
        return back("/settings?" + urlencode({"image_saved": "deleted"}) + "#brand-images")

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
                **settings_extra(request),
                v={
                    **form_values(state.get_config()),
                    **form,
                    # 끈 체크 칸은 폼에 없으므로, 캐릭터 칸이 있었으면 끈 상태를 지킨다
                    **(
                        {"character_ai_made": form.get("character_ai_made", "")}
                        if "card_character" in form
                        else {}
                    ),
                },
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
        "gnews": "google_news",
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
            path, text = await run_in_threadpool(
                save_material, state.uploads_dir, file.filename or "자료", data, state.read_image
            )
        except MaterialError as e:
            return back(dest + urlencode({"error": str(e)}))
        done = {"uploaded": path.name, "chars": len(text.strip())}
        if is_image(path):
            # 무엇을 읽었는지 바로 보여 준다
            flat = " ".join(text.split())
            done["preview"] = flat[:PREVIEW_CHARS] + ("…" if len(flat) > PREVIEW_CHARS else "")
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

    def _save_edit(cid: str, g: GeneratedContent) -> None:
        """검토 대기일 때만 저장된다. 검사 뒤 승인이 끼어들었으면 409(C24)."""
        try:
            state.repo.update_generated(cid, g)
        except InvalidTransitionError as e:
            raise HTTPException(409, str(e)) from e

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
        _save_edit(cid, g.model_copy(update=updates))
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
        _save_edit(cid, new)
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
        _save_edit(cid, new)
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

    # ------------------------------------------------------------------ 장애(E2, ADR 0055)

    @app.get("/incidents", response_class=HTMLResponse)
    def incidents(request: Request) -> HTMLResponse:
        from nsm.app import incidents as inc
        from nsm.domain.errors import POLICIES, short_label

        now = state.clock.now()
        live = inc.ongoing(state, now, lambda t: local(t, tz()).strftime("%H:%M"))
        return page(
            request,
            "incidents.html",
            kinds=inc.kind_rows(state, now, live),
            others=[o for o in live if not o.kind],  # 종류 밖의 멈춤(수동·이유 미기록)
            failures=state.repo.recent_failures(50),
            labels={k: short_label(k) for k in POLICIES},
            days=inc.SUMMARY_DAYS,
        )

    @app.get("/incidents/{failure_id}", response_class=HTMLResponse)
    def incident_detail(request: Request, failure_id: str) -> HTMLResponse:
        from nsm.app import incidents as inc
        from nsm.domain.errors import POLICIES, short_label

        f = state.repo.get_failure(failure_id)
        if f is None:
            raise HTTPException(status_code=404)
        return page(
            request,
            "incident_detail.html",
            f=f,
            d=inc.detail(f),
            labels={k: short_label(k) for k in POLICIES},
        )

    @app.get("/incidents/{failure_id}/screenshot.png")
    def incident_screenshot(request: Request, failure_id: str) -> FileResponse:
        """실패 순간 화면. 내 PC(127.0.0.1)에서 연 대시보드에만 준다."""
        from nsm.app import incidents as inc

        host = request.client.host if request.client else ""
        if host not in LOOPBACK:
            raise HTTPException(status_code=403)
        f = state.repo.get_failure(failure_id)
        d = inc.evidence_path(f) if f else None
        shot = d / "screenshot.png" if d else None
        if shot is None or not shot.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(shot, media_type="image/png", headers={"Cache-Control": "no-store"})

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

    install_keys_routes(app, state, check_csrf)  # API 키 칸(C35)

    return app
