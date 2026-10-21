"""그림 제작 화면(S15 제안, ADR 0058): 검토 상세의 "그림" 칸과 설정의 "캐릭터" 칸.

긴 작업은 요청에서 하지 않는다: 버튼은 작업을 DB에 넣고 images 잡을 깨우기만 한다(작업 스레드가 1장씩 그림).
대사·라벨 수정은 그림 작업을 만들지 않는다(spec에 안 들어감). 장면·캐릭터를 바꾸면 그 자리 spec이 바뀐다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from nsm.app.card_editor import MAX_UPLOAD_BYTES, sniff_image
from nsm.config.characters import CharacterError
from nsm.domain.production import Cast, CharacterRef
from nsm.engine.context import JobContext
from nsm.engine.production import (
    apply_saved_result,
    characters,
    content_dir,
    cost_ref,
    current_slots,
    enqueue_production,
    production_status,
    ref_from_profile,
    upload_image,
    use_image,
)
from nsm.ports import AdapterError
from nsm.store.production import IN_FLIGHT, ProductionStore

log = logging.getLogger(__name__)

ROLE_LABELS = {"panel": "만화 컷", "sticker": "보조 캐릭터 스티커"}
JOB_LABELS = {
    "queued": ("warn", "대기"),
    "claimed": ("warn", "그리는 중"),
    "sent": ("warn", "그리는 중"),
    "succeeded": ("ok", "적용됨"),
    "saved": ("warn", "적용 중"),
    "apply_failed": ("bad", "적용 실패(그림 보존)"),
    "succeeded_stale": ("gray", "후보로 보관"),
    "failed": ("bad", "실패"),
    "unknown": ("warn", "응답 불명"),
    "cancelled": ("gray", "취소"),
}
KIND_LABELS = {"account": "계정 캐릭터", "content": "이 콘텐츠의 캐릭터", "custom": "직접 지정"}


class _Notify:
    def __init__(self, fn) -> None:
        self.fn = fn

    def notify(self, title: str, body: str, level: str) -> None:
        if self.fn:
            self.fn(title, body, level)


def _ctx(state: Any) -> JobContext:
    return JobContext(
        config=state.get_config(),
        repo=state.repo,
        clock=state.clock,
        notifier=_Notify(state.notify),
        media_dir=state.media_dir,
        cost_ledger=state.cost_ledger,
    )


def _label(target: str, role: str) -> str:
    slide, part = target.split(".")
    n = int(slide[1:]) + 1
    if role == "panel":
        return f"{n}장 {int(part[-1]) + 1}번째 컷"
    return f"{n}장 보조 캐릭터"


def production_context(state: Any, prod: ProductionStore, c) -> dict[str, Any]:
    """검토 상세에 보일 그림 칸. 그림 자리가 없는 원고(사진만 쓰는 정보 카드)는 빈 칸."""
    try:
        ctx = _ctx(state)
        _gen, cast, slots = current_slots(ctx, prod, c.id, persist=False)  # 보기만: 저장·복사 없음
    except Exception:  # noqa: BLE001  화면은 그림 칸 없이도 열린다
        return {"production": None}
    latest = prod.latest_jobs(c.id)
    rows = []
    for s in slots:
        act = prod.active(c.id, s.target)
        job = latest.get(s.target)
        others = [i for i in prod.images(c.id, s.target) if i.state != "active"][:6]
        rows.append(
            {
                "target": s.target,
                "role": s.role,
                "label": _label(s.target, s.role),
                "scene": s.scene,
                "active": act,
                "job": job,
                "job_label": JOB_LABELS.get(job.status, ("gray", job.status)) if job else None,
                "in_flight": bool(job and job.status in IN_FLIGHT),
                "others": others,
                "waiting_first": s.chain_from is not None
                and prod.active(c.id, s.chain_from) is None,
            }
        )
    try:
        profiles = characters(state.get_config()).list()
    except Exception:  # noqa: BLE001
        profiles = []
    return {
        "production": {
            "rows": rows,
            "cast": cast,
            "status": production_status(prod, c.id, slots),
            "profiles": profiles,
            "kind_labels": KIND_LABELS,
            "role_labels": ROLE_LABELS,
            "has_studio": state.secrets is not None and bool(state.secrets.get("openai_api_key")),
        }
    }


async def _read_image(file: UploadFile) -> tuple[bytes, str]:
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "이미지는 10MB 이하만 올릴 수 있습니다")
    ext = sniff_image(data[:16])
    if ext is None:
        raise HTTPException(400, "PNG, JPEG, WebP 이미지만 올릴 수 있습니다")
    return data, ext


def install_production_routes(
    app: FastAPI,
    state: Any,
    *,
    check_csrf: Callable[..., Any],
    back: Callable[[str], RedirectResponse],
    act: Callable[[Callable[[], Any]], Any],
    editable: Callable[[str], Any],
    prod: ProductionStore,
) -> None:
    def wake() -> None:
        if state.run_job_now is not None and "images" in state.job_names:
            state.run_job_now("images")

    def here(cid: str) -> RedirectResponse:
        return back(f"/review/{cid}#production")

    def guard(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except (AdapterError, CharacterError) as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/review/{cid}/production/make", dependencies=[Depends(check_csrf)])
    def make_all(cid: str) -> RedirectResponse:
        """그림이 없는 자리를 모두 만든다(유료). 1번째 컷이 확정되면 2번째 컷이 이어서 만들어진다."""
        editable(cid)
        guard(lambda: enqueue_production(_ctx(state), cid))
        wake()
        return here(cid)

    @app.post(
        "/review/{cid}/production/slots/{target}/regenerate", dependencies=[Depends(check_csrf)]
    )
    def regenerate(cid: str, target: str) -> RedirectResponse:
        """이 컷만 다시 만들기(지금 그림은 이력으로 남는다)."""
        editable(cid)
        r = guard(lambda: enqueue_production(_ctx(state), cid, targets=[target], force=True))
        if target in r.problems:
            raise HTTPException(409, r.problems[target])
        wake()
        return here(cid)

    @app.post("/review/{cid}/production/slots/{target}/scene", dependencies=[Depends(check_csrf)])
    def set_scene(cid: str, target: str, scene: str = Form(...)) -> RedirectResponse:
        """장면 지시 수정(그림은 아직 그대로, 다시 만들면 새 장면으로). 만화 컷은 덱의 scene, 스티커는 제작 칸."""
        c = editable(cid)
        scene = scene.strip()[:400]
        if not scene:
            raise HTTPException(400, "장면 지시를 비울 수 없습니다")
        slide_s, part = target.split(".") if "." in target else ("", "")
        if part.startswith("panel"):
            i, j = int(slide_s[1:]), int(part[-1])
            slides = list(c.generated.deck.slides)
            if not (0 <= i < len(slides)) or slides[i].kind != "comic":
                raise HTTPException(404, "그 자리가 없습니다")
            panels = list(slides[i].panels)
            panels[j] = panels[j].model_copy(update={"scene": scene})
            slides[i] = slides[i].model_copy(update={"panels": panels})
            g = c.generated.model_copy(
                update={"deck": c.generated.deck.model_copy(update={"slides": slides})}
            )
            try:
                state.repo.update_generated(cid, g)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(409, str(e)) from e
        else:
            prod.set_scene(cid, target, scene)
        return here(cid)

    @app.post("/review/{cid}/production/images/{image_id}/use", dependencies=[Depends(check_csrf)])
    def reuse(cid: str, image_id: str) -> RedirectResponse:
        editable(cid)
        guard(lambda: use_image(_ctx(state), cid, image_id))
        return here(cid)

    @app.post("/review/{cid}/production/slots/{target}/upload", dependencies=[Depends(check_csrf)])
    async def upload(cid: str, target: str, file: UploadFile = File(...)) -> RedirectResponse:
        editable(cid)
        data, ext = await _read_image(file)
        tmp = content_dir(state.media_dir, cid) / "incoming"
        tmp.mkdir(parents=True, exist_ok=True)
        src = tmp / f"up{ext}"
        src.write_bytes(data)
        try:
            guard(lambda: upload_image(_ctx(state), cid, target, src))
        finally:
            src.unlink(missing_ok=True)
        return here(cid)

    @app.post("/review/{cid}/production/jobs/{jid}/retry", dependencies=[Depends(check_csrf)])
    def retry_job(cid: str, jid: str) -> RedirectResponse:
        """실패·응답 불명 작업을 다시 요청(사람이 결정). 응답 불명이었으면 앞 요청 비용은 미확인으로 남는다."""
        editable(cid)
        j = prod.job(jid)
        if j is None or j.content_id != cid:
            raise HTTPException(404, "작업이 없습니다")
        if not prod.requeue(jid):
            raise HTTPException(409, "다시 요청할 수 있는 상태가 아닙니다")
        wake()
        return here(cid)

    @app.post("/review/{cid}/production/jobs/{jid}/apply", dependencies=[Depends(check_csrf)])
    def reapply(cid: str, jid: str) -> RedirectResponse:
        """보존된 그림을 다시 적용(API를 다시 부르지 않음)."""
        editable(cid)
        j = prod.job(jid)
        if j is None or j.content_id != cid:
            raise HTTPException(404, "작업이 없습니다")
        guard(lambda: apply_saved_result(_ctx(state), jid))
        return here(cid)

    @app.post("/review/{cid}/production/jobs/{jid}/release", dependencies=[Depends(check_csrf)])
    def release_cost(cid: str, jid: str) -> RedirectResponse:
        """응답 불명 요청이 과금되지 않았음을 확인(OpenAI 사용량 화면 등)했을 때 예약액을 푼다."""
        j = prod.job(jid)
        if j is None or j.content_id != cid or j.status != "unknown":
            raise HTTPException(404, "응답 불명 작업이 없습니다")
        if state.cost_ledger is not None:
            state.cost_ledger.release_unknown_by_ref(cost_ref(j))
        prod.finish(
            jid, "failed", error="응답 불명 → 과금 안 됨 확인(사람)", cost_status="released"
        )
        return here(cid)

    @app.post("/review/{cid}/production/cast", dependencies=[Depends(check_csrf)])
    async def set_cast(
        cid: str,
        who: str = Form(...),
        choice: str = Form(...),
        use: str = Form("generate"),
        name: str = Form(""),
        appearance: str = Form(""),
        file: UploadFile | None = File(None),
    ) -> RedirectResponse:
        """출연진 바꾸기: 저장된 캐릭터(계정 기본·다른 캐릭터) / 이 콘텐츠의 캐릭터(전용 참조 업로드) / 없음(보조만)."""
        editable(cid)
        if who not in ("hero", "sidekick") or use not in ("generate", "real_only"):
            raise HTTPException(400, "잘못된 값")
        ctx = _ctx(state)
        raw, _ = prod.get_production(cid)
        cast = Cast.from_json(raw) if raw else current_slots(ctx, prod, cid)[1]
        cast_dir = content_dir(state.media_dir, cid) / "cast"
        ref: CharacterRef | None
        if choice == "none":
            if who == "hero":
                raise HTTPException(400, "만화 주인공은 비울 수 없습니다")
            ref = None
        elif choice.startswith("profile:"):
            pid = choice.split(":", 1)[1]
            kind = (
                "account"
                if pid in (ctx.config.workspace.hero_default, ctx.config.workspace.sidekick_default)
                else "custom"
            )
            ref = guard(lambda: ref_from_profile(characters(ctx.config), pid, kind, cast_dir))
            if use == "real_only":
                ref = CharacterRef(
                    **{**ref.to_dict(), "use": "real_only", "references": tuple(ref.references)}
                )
        elif choice in ("content", "custom_upload"):
            # 이 원고 전용: 업로드한 참조(공식 이미지에서 고른 것 등) + 이름·외형. 정체성 출처와 생성 여부는 따로 정한다
            refs = []
            prev = cast.hero if who == "hero" else cast.sidekick
            if prev is not None and prev.kind == ("content" if choice == "content" else "custom"):
                refs = list(prev.references)
            if file is not None and file.filename:
                data, ext = await _read_image(file)
                cast_dir.mkdir(parents=True, exist_ok=True)
                dst = cast_dir / f"{who}-adhoc-{len(refs)}{ext}"
                dst.write_bytes(data)
                refs.append(str(dst))
            ref = CharacterRef(
                kind="content" if choice == "content" else "custom",
                use=use,  # type: ignore[arg-type]
                name=name.strip()[:30] or (prev.name if prev else ""),
                appearance=appearance.strip()[:500] or (prev.appearance if prev else ""),
                references=tuple(refs),
            )
        else:
            raise HTTPException(400, "알 수 없는 선택")
        cast = (
            Cast(hero=ref, sidekick=cast.sidekick)
            if who == "hero"
            else Cast(hero=cast.hero, sidekick=ref)
        )
        prod.set_cast(cid, cast.to_json(), ctx.config.settings.preset)
        return here(cid)

    @app.get("/production/{cid}/images/{image_id}")
    def image_file(cid: str, image_id: str) -> FileResponse:
        img = prod.image(image_id)
        if img is None or img.content_id != cid:
            raise HTTPException(404)
        p = Path(img.path).resolve()
        if not p.is_file() or content_dir(state.media_dir, cid).resolve() not in p.parents:
            raise HTTPException(404)
        return FileResponse(p)

    # -------------------------------------------------------------- 설정: 캐릭터
    def chars():
        return characters(state.get_config())

    def to_settings(msg: str = "", error: str = "") -> RedirectResponse:
        from urllib.parse import urlencode

        q = urlencode({"char_msg": msg} if msg else {"char_error": error})
        return back(f"/settings?{q}#characters")

    @app.post("/settings/characters", dependencies=[Depends(check_csrf)])
    def create_character(
        name: str = Form(...), appearance: str = Form(""), personality: str = Form("")
    ) -> RedirectResponse:
        try:
            p = chars().create(name, appearance, personality)
        except CharacterError as e:
            return to_settings(error=str(e))
        return to_settings(msg=f"'{p.name}' 캐릭터를 만들었습니다. 참조 이미지를 올리세요")

    @app.post("/settings/characters/{pid}/refs", dependencies=[Depends(check_csrf)])
    async def add_ref(
        pid: str, file: UploadFile = File(...), source: str = Form("")
    ) -> RedirectResponse:
        data, ext = await _read_image(file)
        try:
            chars().add_reference(pid, data, ext, source=source)
        except CharacterError as e:
            return to_settings(error=str(e))
        return to_settings(msg="참조 이미지를 추가했습니다")

    @app.post("/settings/characters/{pid}/refs/{name}/delete", dependencies=[Depends(check_csrf)])
    def del_ref(pid: str, name: str) -> RedirectResponse:
        try:
            chars().remove_reference(pid, name)
        except CharacterError as e:
            return to_settings(error=str(e))
        return to_settings(msg="참조 이미지를 지웠습니다")

    @app.post("/settings/characters/{pid}/generate-ref", dependencies=[Depends(check_csrf)])
    def generate_ref(pid: str) -> RedirectResponse:
        """외형 설명으로 참조 그림 한 장(유료, 투명 PNG). 만들어지면 참조 목록에 들어간다(AI로 만든 그림 표시)."""
        try:
            p = chars().get(pid)
        except CharacterError as e:
            return to_settings(error=str(e))
        if not p.appearance:
            return to_settings(error="외형 설명을 먼저 적어 주세요")
        from nsm.config.characters import MAX_REFS

        if len(p.references) >= MAX_REFS:  # 돈을 쓰기 전에 확인(S15 리뷰)
            return to_settings(
                error=f"참조 이미지는 {MAX_REFS}장까지입니다. 하나를 지운 뒤 만드세요"
            )
        spec = {
            "prompt": (
                f"Character reference sheet of {p.name}: {p.appearance}. Front view, full body, neutral pose, "
                "cute rounded illustration, thick clean outline."
            ),
            "references": list(p.references),
            "negative": "text, letters, logos, watermarks, other copyrighted characters",
            "width": 1024,
            "height": 1024,
            "transparent": True,
            "quality": state.get_config().settings.models.image_quality,
        }
        import hashlib
        import json

        h = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:24]
        prod.enqueue(None, f"char:{pid}", "reference", h, spec, state.get_config().settings.preset)
        wake()
        return to_settings(
            msg="참조 그림을 만드는 중입니다(1~2분, 약 5센트). 다 되면 목록에 나타납니다"
        )

    @app.post("/settings/characters/{pid}/delete", dependencies=[Depends(check_csrf)])
    def delete_character(pid: str) -> RedirectResponse:
        try:
            chars().delete(pid)
        except CharacterError as e:
            return to_settings(error=str(e))
        return to_settings(msg="캐릭터를 지웠습니다(이미 만든 원고에는 복사본이 남아 그대로입니다)")

    @app.post("/settings/characters/defaults", dependencies=[Depends(check_csrf)])
    def set_defaults(hero: str = Form(""), sidekick: str = Form("")) -> RedirectResponse:
        cfg = state.get_config()
        ids = {p.id for p in chars().list()}
        if (hero and hero not in ids) or (sidekick and sidekick not in ids):
            return to_settings(error="없는 캐릭터입니다")
        w = cfg.workspace.model_copy(
            update={"hero_default": hero or None, "sidekick_default": sidekick or None}
        )
        state.set_config(cfg.model_copy(update={"workspace": w}))
        return to_settings(msg="기본 출연진을 저장했습니다(이 작업 공간에만)")

    @app.get("/characters/{pid}/refs/{name}")
    def ref_file(pid: str, name: str) -> FileResponse:
        try:
            p = chars().get(pid)
        except CharacterError as e:
            raise HTTPException(404) from e
        for r in p.references:
            if Path(r).name == Path(name).name:
                return FileResponse(r)
        raise HTTPException(404)
