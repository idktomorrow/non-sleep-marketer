"""오늘의 추천 화면(S11 제안, ADR 0051): /picks(오늘·보관함)와 추천 전용 동작. server.create_app이 붙인다."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from nsm.app.review_actions import ReviewActions
from nsm.domain.policy import local
from nsm.store.picks import Pick

STATE_LABELS = {
    "ready": "오늘의 추천",
    "saved": "보관",
    "queued": "게시 대기",
    "dismissed": "관심 없음",
}
PHOTO_LABELS = {
    "candidate": "사진 확인 대기",
    "applied": "사진 적용",
    "none": "사진 없음",
    "dropped": "사진 안 씀",
}
FRESH_LABELS = {"fresh": "최근 소식", "stale": "오래된 자료", "unknown": "날짜 확인 필요"}


def pick_view(p: Pick, content, now, tz: str) -> dict[str, Any]:
    """카드 한 장에 보일 값. 상태 배지는 사진 준비·렌더 확인·재확인 여부를 함께 본다."""
    age = now - content.created_at
    needs_recheck = ReviewActions.needs_recheck(content.created_at, p.rechecked_at, now)
    if p.ready_to_publish:
        readiness = ("ok", "게시 준비 완료")
    elif p.photo_status == "candidate":
        readiness = ("warn", "사진 확인 대기")
    else:
        readiness = ("gray", "미리보기 확인 전")
    return {
        "pick": p,
        "content": content,
        "state": STATE_LABELS.get(p.state, p.state),
        "fresh": FRESH_LABELS.get(p.freshness, p.freshness),
        "photo": PHOTO_LABELS.get(p.photo_status, p.photo_status),
        "readiness": readiness,
        "age_days": age.days,
        "needs_recheck": needs_recheck,
        "published_local": local(p.published_at, tz) if p.published_at else None,
    }


def install_picks_routes(
    app: FastAPI,
    state: Any,
    *,
    page: Callable[..., HTMLResponse],
    check_csrf: Callable[..., Any],
    back: Callable[[str], RedirectResponse],
    act: Callable[[Callable[[], Any]], Any],
    actions: ReviewActions,
    thumbs: Callable[[list], dict[str, str]],
) -> None:
    def tz() -> str:
        return state.get_config().settings.timezone

    def _views(picks: list[Pick]) -> list[dict[str, Any]]:
        now = state.clock.now()
        out = []
        for p in picks:
            c = state.repo.get_content(p.content_id)
            if c is not None:
                out.append(pick_view(p, c, now, tz()))
        return out

    _run = act  # ActionError → HTTPException 변환은 server.create_app과 같은 함수

    @app.get("/picks", response_class=HTMLResponse)
    def picks_page(request: Request) -> HTMLResponse:
        if state.picks is None:
            raise HTTPException(503, "추천 저장소가 없습니다")
        today = local(state.clock.now(), tz()).date()
        ready = _views(state.picks.list(state="ready"))
        saved = _views(state.picks.list(state="saved"))
        todays = [v for v in ready if v["pick"].run_date == today]
        older = [v for v in ready if v["pick"].run_date != today]
        all_c = [v["content"] for v in ready + saved]
        return page(
            request,
            "picks.html",
            todays=todays,
            older=older,
            saved=saved,
            thumbs=thumbs(all_c),
            picks_enabled=state.get_config().settings.picks.enabled,
            can_run=state.run_job_now is not None and "picks" in state.job_names,
        )

    @app.post("/picks/{cid}/save", dependencies=[Depends(check_csrf)])
    def save_pick(cid: str) -> RedirectResponse:
        _run(lambda: actions.save_for_later(cid))
        return back("/picks#saved")

    @app.post("/picks/{cid}/queue", dependencies=[Depends(check_csrf)])
    def queue_pick(cid: str) -> RedirectResponse:
        cfg = state.get_config()
        _run(
            lambda: actions.approve(
                cid, persona=cfg.persona, platforms=list(cfg.settings.enabled_platforms)
            )
        )
        return back("/queue")

    @app.post("/picks/{cid}/dismiss", dependencies=[Depends(check_csrf)])
    def dismiss_pick(cid: str) -> RedirectResponse:
        _run(lambda: actions.discard(cid))
        return back("/picks")

    @app.post("/picks/{cid}/recheck", dependencies=[Depends(check_csrf)])
    def recheck_pick(cid: str) -> RedirectResponse:
        _run(lambda: actions.recheck(cid))
        return back("/picks#saved")

    @app.post("/picks/{cid}/photo/use", dependencies=[Depends(check_csrf)])
    def photo_use(cid: str) -> RedirectResponse:
        _run(lambda: actions.photo_use(cid, state.media_dir))
        return back(f"/review/{cid}")

    @app.post("/picks/{cid}/photo/drop", dependencies=[Depends(check_csrf)])
    def photo_drop(cid: str) -> RedirectResponse:
        _run(lambda: actions.photo_drop(cid))
        return back(f"/review/{cid}")

    @app.get("/picks/{cid}/photo")
    def photo_file(cid: str) -> FileResponse:
        """사진 후보 보기. DB에 적힌 경로만, 그것도 추천 사진 폴더 안의 파일만 보낸다."""
        pick = state.picks.get(cid) if state.picks is not None else None
        if pick is None or not pick.photo_candidate_path:
            raise HTTPException(404)
        path = Path(pick.photo_candidate_path).resolve()
        root = (state.picks_dir or (state.media_dir / "picks")).resolve()
        if root not in path.parents or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, headers={"Cache-Control": "no-cache"})
