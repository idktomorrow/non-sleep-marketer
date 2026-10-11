"""로컬 대시보드 (FastAPI + Jinja2, 127.0.0.1에서만).

- 화면은 "동작 확인용 최소" HTML이다. 디자인은 배치 I(UI/UX 패스)에서 바꾼다. 라우트·폼 이름은 유지.
- 모든 POST는 CSRF 토큰을 검사한다. 다른 웹사이트가 사용자의 브라우저로 localhost에 몰래 요청을 보내
  콘텐츠를 승인하는 일을 막기 위해서다.
- 플랫폼 동작(직접 답글)은 `submit`으로 잡 스레드에서 실행한다(Playwright 스레드 제약).
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from nsm.config.schema import AppConfig
from nsm.domain.models import Platform
from nsm.domain.policy import find_banned, local, local_day_bounds
from nsm.engine.generate import AUTO_PUBLISH_KEY, all_texts
from nsm.engine.publish import PAUSED_KEY
from nsm.ports import Clock, PlatformAdapter
from nsm.store import queries
from nsm.store.repo import InvalidTransitionError, Repository

HERE = Path(__file__).resolve().parent


@dataclass
class AppState:
    repo: Repository
    clock: Clock
    get_config: Callable[[], AppConfig]
    set_config: Callable[[AppConfig], None]
    submit: Callable[[Callable[[], Any]], Future]
    adapters: dict[Platform, PlatformAdapter]
    media_dir: Path
    csrf_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    run_job_now: Callable[[str], Future] | None = None


def create_app(state: AppState) -> FastAPI:
    app = FastAPI(title="논슬립 마케터", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def tz() -> str:
        return state.get_config().settings.timezone

    def fmt(dt) -> str:
        return local(dt, tz()).strftime("%m-%d %H:%M") if dt else "-"

    templates.env.filters["kst"] = fmt

    def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        cfg = state.get_config()
        return templates.TemplateResponse(
            request,
            name,
            {
                "csrf": state.csrf_token,
                "cfg": cfg,
                "auto_publish": bool(state.repo.get_setting(AUTO_PUBLISH_KEY, False)),
                "paused": state.repo.get_setting(PAUSED_KEY, []),
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
        return page(
            request,
            "overview.html",
            ov=queries.overview(state.repo.db, start, end),
            human_approved=state.repo.count_human_approved(),
            runs=state.repo.recent_runs(15),
        )

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

    @app.post("/jobs/{name}/run", dependencies=[Depends(check_csrf)])
    def run_now(name: str) -> RedirectResponse:
        if state.run_job_now is None:
            raise HTTPException(503, "스케줄러가 없습니다")
        state.run_job_now(name)
        return back("/logs")

    # ------------------------------------------------------------------ 검토 대기열

    @app.get("/review", response_class=HTMLResponse)
    def review_list(request: Request) -> HTMLResponse:
        return page(request, "review_list.html", items=state.repo.list_contents("pending_review"))

    def _content_or_404(cid: str):
        c = state.repo.get_content(cid)
        if c is None:
            raise HTTPException(404, "콘텐츠가 없습니다")
        return c

    @app.get("/review/{cid}", response_class=HTMLResponse)
    def review_detail(request: Request, cid: str) -> HTMLResponse:
        c = _content_or_404(cid)
        cards = sorted((state.media_dir / "cards" / cid).glob("slide-*.png"))
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
        )

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
        _content_or_404(cid)
        try:
            state.repo.transition_content(cid, "approved", approved_by="human")
        except InvalidTransitionError as e:
            raise HTTPException(409, str(e)) from e
        state.repo.requeue_abandoned(cid)
        return back("/review")

    @app.post("/review/{cid}/discard", dependencies=[Depends(check_csrf)])
    def discard(cid: str) -> RedirectResponse:
        _content_or_404(cid)
        try:
            state.repo.transition_content(cid, "discarded", reason="사람이 폐기")
        except InvalidTransitionError as e:
            raise HTTPException(409, str(e)) from e
        return back("/review")

    @app.post("/review/{cid}/edit", dependencies=[Depends(check_csrf)])
    def edit(
        cid: str,
        instagram_caption: str = Form(...),
        threads_text: str = Form(...),
        blog_title: str = Form(...),
        blog_markdown: str = Form(...),
        cover_title: str = Form(...),
    ) -> RedirectResponse:
        c = _content_or_404(cid)
        if c.status != "pending_review":
            raise HTTPException(409, "검토 대기 중인 콘텐츠만 고칠 수 있습니다")
        g = c.generated
        slides = list(g.deck.slides)
        slides[0] = slides[0].model_copy(update={"title": cover_title})
        new = g.model_copy(
            update={
                "instagram_caption": instagram_caption,
                "threads_text": threads_text,
                "blog_title": blog_title,
                "blog_markdown": blog_markdown,
                "deck": g.deck.model_copy(update={"slides": slides}),
            }
        )
        state.repo.update_generated(cid, new)
        # 고친 뒤 이미 렌더된 카드는 버린다(다음 게시 때 다시 렌더)
        for p in (state.media_dir / "cards" / cid).glob("slide-*.png"):
            p.unlink()
        return back(f"/review/{cid}")

    # ------------------------------------------------------------------ 댓글

    @app.get("/comments", response_class=HTMLResponse)
    def comments(request: Request) -> HTMLResponse:
        return page(
            request,
            "comments.html",
            escalated=state.repo.list_comments("escalated"),
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
        try:
            reply_id = state.submit(lambda: adapter.reply(stored.comment, text)).result(timeout=120)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"답글 전송 실패: {e}") from e
        state.repo.update_comment(
            key, status="resolved", reply_text=text, reply_external_id=reply_id, handled_by="human"
        )
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
            followers=queries.follower_series(state.repo.db, since),
            comment_stats=queries.comment_stats(state.repo.db, since),
            costs=queries.costs_by_day(state.repo.db, now - timedelta(days=14)),
        )

    @app.get("/logs", response_class=HTMLResponse)
    def logs(request: Request) -> HTMLResponse:
        return page(request, "logs.html", runs=state.repo.recent_runs(200))

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
