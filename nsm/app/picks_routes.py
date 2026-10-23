"""오늘의 추천 화면(S11 제안, ADR 0051): /picks(오늘·보관함)와 추천 전용 동작. server.create_app이 붙인다."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from nsm.app.review_actions import ReviewActions
from nsm.config.schema import PHOTO_FIRST_STYLES
from nsm.domain.policy import local
from nsm.domain.topics import account_topic_conflict
from nsm.store.picks import Pick
from nsm.store.slots import Slot, SlotStore

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


STAGE_LABELS = {
    "planned": ("gray", "계획됨"),
    "writing": ("warn", "쓰는 중"),
    "written": ("gray", "기획 완료"),
    "images_pending": ("warn", "그림 제작 대기"),
    "producing": ("warn", "그림 제작 중"),
    "ready": ("ok", "검토 가능"),
    "failed_text": ("bad", "원고 실패"),
    "failed_image": ("bad", "그림 실패"),
    "empty": ("gray", "후보 없음"),
    "unknown_image": ("warn", "그림 응답 불명"),
    "cancelled": ("gray", "제외됨"),
}
KIND_LABELS = {"info": "정보", "meme": "밈"}
PRODUCTION_BLOCKING = ("images_pending", "producing", "failed_image", "unknown_image")


def _has_real_photo(content) -> bool:
    # 상품마다 넣은 사진도 실제 사진이다(S18 리뷰: 상품 사진만 있는 덱을 "사진 없음"으로 띄웠다)
    return any(
        getattr(s, "background", None)
        or getattr(s, "photo", None)
        or any(it.image for it in getattr(s, "items", None) or [])
        for s in content.generated.deck.slides
    )


def pick_view(
    p: Pick, content, now, tz: str, slot: Slot | None = None, photos_expected: bool = False
) -> dict[str, Any]:
    """카드 한 장에 보일 값. 상태 배지는 슬롯 제작 단계·사진 준비·렌더 확인·재확인 여부를 함께 본다(S14)."""
    age = now - content.created_at
    needs_recheck = ReviewActions.needs_recheck(content.created_at, p.rechecked_at, now)
    stage = slot.stage if slot else "ready"
    if stage in PRODUCTION_BLOCKING:
        readiness = STAGE_LABELS[stage]  # 그림이 끝나기 전엔 "게시 준비"가 아니다
    elif p.ready_to_publish:
        readiness = ("ok", "게시 준비 완료")
    elif p.photo_status == "candidate":
        readiness = ("warn", "사진 확인 대기")
    elif (p.kind or "info") == "info" and photos_expected and not _has_real_photo(content):
        # 소재 사진이 없는 정보 원고를 조용히 완성으로 보이지 않게(S18): 직접 올리거나 다른 자료로
        readiness = ("warn", "소재 사진 없음 · 직접 올리기 필요")
    else:
        readiness = ("gray", "미리보기 확인 전")
    kind = p.kind or "info"
    requested = slot.requested_kind if slot else kind
    return {
        "pick": p,
        "content": content,
        "slot": slot,
        "kind": KIND_LABELS.get(kind, kind),
        "substituted": requested != kind,
        "requested_kind": KIND_LABELS.get(requested, requested),
        "stage": STAGE_LABELS.get(stage, ("gray", stage)),
        "plan": (slot.plan or {}) if slot else {},
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
    approve_or_preview: Callable[[str, str], RedirectResponse],
) -> None:
    def tz() -> str:
        return state.get_config().settings.timezone

    def _slots() -> SlotStore:
        return SlotStore(state.repo.db, state.clock)

    def _views(picks: list[Pick]) -> list[dict[str, Any]]:
        now = state.clock.now()
        store = _slots()
        by_key: dict[str, Slot] = {}
        for d in {p.run_date for p in picks if p.slot_key}:  # 날짜마다 한 번만 읽는다(S14 리뷰)
            by_key.update({s.key: s for s in store.list_for_date(d)})
        out = []
        contents = {p.content_id: state.repo.get_content(p.content_id) for p in picks}
        topics = state.repo.content_topics([cid for cid, c in contents.items() if c is not None])
        # 출처는 한 번에 읽는다(카드마다 자료 수만큼 조회하던 것, S16 리뷰)
        src_ids = {
            cid: list(dict.fromkeys(x.source_item_id for x in c.generated.claims))
            for cid, c in contents.items()
            if c is not None
        }
        items = state.repo.get_source_items(sorted({i for ids in src_ids.values() for i in ids}))
        zone = tz()
        cfg = state.get_config()
        # 사진 우선형이고 사진 후보를 모으는 설정일 때만 "소재 사진 없음"을 띄운다(다른 스타일은 원래 사진이 없다, S18 리뷰)
        photos_expected = (
            cfg.brand.card_style in PHOTO_FIRST_STYLES and cfg.settings.picks.photo_candidates
        )
        for p in picks:
            c = contents[p.content_id]
            if c is not None:
                v = pick_view(p, c, now, zone, by_key.get(p.slot_key or ""), photos_expected)
                v["topic"] = topics.get(c.id) or v["plan"].get("topic") or ""
                v["sources"] = [items[i] for i in src_ids[c.id] if i in items][:4]
                out.append(v)
        return out

    def _plan_today(today):
        from nsm.engine.picks import effective_plan

        class _Ctx:  # effective_plan은 repo·config만 쓴다
            repo = state.repo
            config = state.get_config()

        return effective_plan(_Ctx(), today)  # type: ignore[arg-type]

    def _slot_rows(today) -> list[dict[str, Any]]:
        rows = []
        for sl in _slots().list_for_date(today):
            if sl.stage == "cancelled":
                continue
            rows.append(
                {
                    "slot": sl,
                    "kind": KIND_LABELS.get(sl.requested_kind, sl.requested_kind),
                    "actual": KIND_LABELS.get(sl.actual_kind or "", "") if sl.actual_kind else "",
                    "stage": STAGE_LABELS.get(sl.stage, ("gray", sl.stage)),
                    "retryable": sl.retryable,
                }
            )
        return rows

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
        plan = _plan_today(today)
        return page(
            request,
            "picks.html",
            todays=todays,
            older=older,
            saved=saved,
            thumbs=thumbs(all_c),
            picks_enabled=state.get_config().settings.picks.enabled,
            can_run=state.run_job_now is not None and "picks" in state.job_names,
            plan=plan,
            slot_rows=_slot_rows(today),
            topics=list(state.get_config().persona.topics),
            conflict=account_topic_conflict(
                state.get_config().brand.industry,
                state.get_config().persona.character,
                list(state.get_config().persona.topics),
            ),
            plan_msg=request.query_params.get("plan"),
        )

    @app.post("/picks/plan", dependencies=[Depends(check_csrf)])
    def plan_override(info: int = Form(...), meme: int = Form(...)) -> RedirectResponse:
        """오늘만 구성 변경(S14): 프리셋 DB에 저장. 이미 만든 원고는 지우거나 다시 만들지 않는다."""
        from nsm.engine.picks import PLAN_OVERRIDE_KEY

        if not (0 <= info <= 5 and 0 <= meme <= 3 and info + meme <= 6):
            raise HTTPException(400, "정보 0~5, 밈 0~3, 합계 6까지")
        today = local(state.clock.now(), tz()).date()
        state.repo.set_setting(
            PLAN_OVERRIDE_KEY, {"date": today.isoformat(), "info": info, "meme": meme}
        )
        _slots().plan(today, info, meme, state.get_config().settings.preset)
        return RedirectResponse("/picks?plan=saved", status_code=303)

    @app.post("/picks/slots/{key}/retry", dependencies=[Depends(check_csrf)])
    def retry_slot(key: str) -> RedirectResponse:
        sl = _slots().by_key(key) if key.count(":") == 2 else None
        if sl is None:
            raise HTTPException(404, "슬롯이 없습니다")
        if not _slots().retry(sl):
            raise HTTPException(409, "다시 시도할 수 없는 상태이거나 횟수를 넘었습니다")
        if state.run_job_now is not None and "slots_retry" in state.job_names:
            state.run_job_now("slots_retry")
        return RedirectResponse("/picks", status_code=303)

    @app.post("/picks/{cid}/save", dependencies=[Depends(check_csrf)])
    def save_pick(cid: str) -> RedirectResponse:
        _run(lambda: actions.save_for_later(cid))
        return back("/picks#saved")

    @app.post("/picks/{cid}/queue", dependencies=[Depends(check_csrf)])
    def queue_pick(cid: str) -> RedirectResponse:
        # 승인 = 본 미리보기 판을 굳히기(S15). 미리보기가 없거나 낡았으면 새로 그려 상세 화면으로
        return approve_or_preview(cid, "/queue")

    @app.post("/picks/{cid}/dismiss", dependencies=[Depends(check_csrf)])
    def dismiss_pick(cid: str) -> RedirectResponse:
        _run(lambda: actions.discard(cid))
        return back("/picks")

    @app.post("/picks/{cid}/meme/remake", dependencies=[Depends(check_csrf)])
    def remake_meme(cid: str, key: str = Form(...)) -> RedirectResponse:
        """다른 밈으로 다시 만들기(S17): 고른 후보 형식으로 다시 기획한다(작업 스레드에서, AI 호출 1회)."""
        if state.run_job_now is None:
            raise HTTPException(503, "작업 실행기가 없습니다")
        if not re.fullmatch(r"[a-z0-9\-]{1,80}", key):
            raise HTTPException(400, "밈 후보 이름 형식이 아닙니다")
        state.run_job_now("meme_remake", f"{cid}|{key}")
        return back("/picks")

    @app.post("/picks/{cid}/recheck", dependencies=[Depends(check_csrf)])
    def recheck_pick(cid: str) -> RedirectResponse:
        _run(lambda: actions.recheck(cid))
        return back("/picks#saved")

    @app.post("/picks/{cid}/photo/use", dependencies=[Depends(check_csrf)])
    def photo_use(cid: str) -> RedirectResponse:
        # 사진 색을 카드 색으로(작업 공간 옵션, S14)
        _run(
            lambda: actions.photo_use(
                cid, state.media_dir, recolor=state.get_config().workspace.color_from_photo
            )
        )
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
