"""그림 제작(S15 제안, ADR 0058): 출연진 → 슬롯별 spec → 영속 이미지 작업 → 결과 반영.

- 작업은 호출 **전에** DB에 있다. 한 번 실행에 몇 장만 처리해 작업 스레드(게시·답글)를 오래 막지 않는다.
- 유효성은 슬롯별 spec 지문: 결과가 왔을 때 그 슬롯의 지금 spec과 같으면 덱에 적용, 다르면(장면·캐릭터가 바뀜) 후보로만.
- 2번째 컷은 1번째 컷 결과가 확정된 뒤에 그 파일 지문을 참조로 넣어 만든다.
- 실패는 대체하지 않는다: 참조 없음·키 없음·정책 차단·한도·응답 불명을 그대로 기록하고, 미완성 만화는 게시 준비가 아니다.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from nsm.config.characters import CharacterError, CharacterStore
from nsm.config.presets import PresetStore
from nsm.domain.clock import new_id
from nsm.domain.models import GeneratedContent, ImageAsset
from nsm.domain.policy import local_day_bounds, under_cost_cap
from nsm.domain.production import (
    Cast,
    CharacterRef,
    MissingReference,
    SlotPlan,
    build_spec,
    file_sha,
    image_slots,
)
from nsm.engine.context import JobContext, JobResult
from nsm.ports import AdapterError, ImageRefused, ImageSpec, ImageUnknown
from nsm.store.production import IN_FLIGHT, ImageJob, ProductionStore
from nsm.store.slots import SlotStore

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
JOBS_PER_RUN = 3  # 한 번 실행에 그릴 장 수(작업 스레드를 오래 잡지 않게)
RETRY_IN = timedelta(minutes=10)


def characters(cfg) -> CharacterStore:
    """작업 공간(프리셋)의 캐릭터 폴더."""
    return CharacterStore(PresetStore().dir(cfg.settings.preset) / "characters")


def content_dir(media_dir: Path, cid: str) -> Path:
    return media_dir / "content" / cid


def ref_from_profile(
    store: CharacterStore, profile_id: str, kind: str, cast_dir: Path
) -> CharacterRef:
    """프로필 → 출연 캐릭터. 참조 파일은 원고 폴더로 **복사**한다(프로필을 고쳐도 이 원고는 그대로)."""
    p = store.get(profile_id)
    cast_dir.mkdir(parents=True, exist_ok=True)
    copies = []
    for r in p.references:
        dst = cast_dir / f"{profile_id}-{Path(r).name}"
        if not dst.is_file():
            shutil.copyfile(r, dst)
        copies.append(str(dst))
    return CharacterRef(
        kind=kind,  # type: ignore[arg-type]
        use="generate",
        name=p.name,
        appearance=p.appearance,
        profile_id=p.id,
        profile_revision=p.revision,
        references=tuple(copies),
        sources=tuple(p.sources()),
    )


def default_cast(ctx: JobContext, cid: str, copy: bool = True) -> Cast:
    """작업 공간 기본값: 주인공(hero_default)·보조(sidekick_default). **없으면 None** — 페르소나 이름·인물상이나
    설정의 캐릭터 그림으로 주인공을 만들지 않는다(사용자가 고르지 않은 캐릭터 금지, 2026-10-21).
    copy=False(화면 조회)면 파일을 복사하지 않고 프로필 경로를 그대로 보여 준다."""
    w = ctx.config.workspace
    store = characters(ctx.config)
    cast_dir = content_dir(ctx.media_dir, cid) / "cast"

    def load(pid: str | None, role: str) -> CharacterRef | None:
        if not pid:
            return None
        try:
            if copy:
                return ref_from_profile(store, pid, "account", cast_dir)
            p = store.get(pid)
            return CharacterRef(
                kind="account",
                name=p.name,
                appearance=p.appearance,
                profile_id=p.id,
                profile_revision=p.revision,
                references=tuple(p.references),
                sources=tuple(p.sources()),
            )
        except CharacterError as e:  # 주인공이 없어도 보조는 따로 불러온다(S15 리뷰)
            log.warning("기본 %s 캐릭터를 불러오지 못함: %s", role, e)
            return None

    return Cast(hero=load(w.hero_default, "주인공"), sidekick=load(w.sidekick_default, "보조"))


def ensure_cast(ctx: JobContext, prod: ProductionStore, cid: str, persist: bool = True) -> Cast:
    """저장된 출연진. 없으면 기본값 — persist=False(화면 조회)면 저장·복사하지 않는다(GET 부작용 없음)."""
    raw, _ = prod.get_production(cid)
    if raw:
        return Cast.from_json(raw)
    cast = default_cast(ctx, cid, copy=persist)
    if persist:
        prod.set_cast(cid, cast.to_json(), ctx.config.settings.preset)
    return cast


def current_slots(
    ctx: JobContext, prod: ProductionStore, cid: str, persist: bool = True
) -> tuple[GeneratedContent, Cast, list[SlotPlan]]:
    c = ctx.repo.get_content(cid)
    if c is None:
        raise AdapterError("원고가 없습니다")
    cast = ensure_cast(ctx, prod, cid, persist)
    _raw, scenes = prod.get_production(cid)
    from nsm.store.slots import SlotStore

    sl = SlotStore(ctx.repo.db, ctx.clock).by_content(cid)
    plan = (sl.plan or {}) if sl else {}
    return (
        c.generated,
        cast,
        image_slots(
            c.generated.deck,
            cast,
            scenes,
            meme_ref_path(ctx.media_dir, cid),
            str(plan.get("make", "restage")),
            str(plan.get("core", "")),
        ),
    )


def meme_ref_path(media_dir: Path, cid: str) -> str | None:
    """원고의 밈 참조 그림(S17). 캐릭터 참조(cast/)와 다른 폴더(meme_ref/)에 둔다."""
    d = content_dir(media_dir, cid) / "meme_ref"
    files = sorted(p for p in d.glob("*") if p.is_file()) if d.is_dir() else []
    return str(files[0]) if files else None


def spec_for(ctx: JobContext, prod: ProductionStore, cid: str, slot: SlotPlan, cast: Cast):
    chain = None
    if slot.chain_from:
        first = prod.active(cid, slot.chain_from)
        if first is None:
            raise MissingReference("1번째 컷이 아직 없습니다")
        chain = first.path
    return build_spec(slot, cast, ctx.config.settings.models.image_quality, chain)


@dataclass
class EnqueueResult:
    queued: list[str]
    waiting: list[str]  # 1번째 컷을 기다리는 2번째 컷
    problems: dict[str, str]


def enqueue_production(
    ctx: JobContext, cid: str, targets: list[str] | None = None, force: bool = False
) -> EnqueueResult:
    """그림이 필요한 슬롯의 작업을 만든다. targets를 주면 그 슬롯만(이 컷만 다시 만들기), force면 활성 그림이 있어도."""
    prod = ProductionStore(ctx.repo.db, ctx.clock)
    _gen, cast, slots = current_slots(ctx, prod, cid)
    latest = prod.latest_jobs(cid)
    res = EnqueueResult([], [], {})
    for slot in slots:
        if targets is not None and slot.target not in targets:
            continue
        if not force and prod.active(cid, slot.target) is not None:
            continue
        if slot.chain_from:
            first_job = latest.get(slot.chain_from)
            pending_first = first_job is not None and first_job.status in IN_FLIGHT
            if pending_first or prod.active(cid, slot.chain_from) is None:
                res.waiting.append(slot.target)  # 1번째 컷 결과가 확정되면 그때 만든다
                continue
        try:
            spec = spec_for(ctx, prod, cid, slot, cast)
        except MissingReference as e:
            res.problems[slot.target] = str(e)
            continue
        job = prod.enqueue(
            cid, slot.target, slot.role, spec.hash, _spec_json(spec), ctx.config.settings.preset
        )
        if job is not None:
            res.queued.append(slot.target)
    _sync_slot_stage(ctx, prod, cid, res.problems)
    return res


def _spec_json(spec) -> dict:
    return {
        **spec.detail,
        "prompt": spec.prompt,
        "references": list(spec.references),
        "negative": spec.negative,
        "width": spec.width,
        "height": spec.height,
    }


# ------------------------------------------------------------------ 실행


def run_images(ctx: JobContext, max_jobs: int = JOBS_PER_RUN) -> JobResult:
    """대기 중인 그림 작업을 몇 장 처리한다(images 잡, 1분마다 + 제작 버튼 직후)."""
    result = JobResult("images")
    prod = ProductionStore(ctx.repo.db, ctx.clock)
    preset = ctx.config.settings.preset
    if prod.next_due(ctx.clock.now(), preset) is None:
        result.skipped = "그릴 그림 작업이 없음"
        return result
    studio = ctx.image_studio
    if studio is None:
        # 키가 없으면 대체하지 않고 작업마다 이유를 남긴다(도형 캐릭터로 바꾸지 않는다)
        n = 0
        while (job := prod.next_due(ctx.clock.now(), preset)) is not None and n < 50:
            prod.finish(
                job.id,
                "failed",
                error="OpenAI API 키가 없어 그림을 만들 수 없습니다(설정 → API 키)",
            )
            if job.content_id:
                _sync_slot_stage(ctx, prod, job.content_id)
            n += 1
        result.counts["failed"] = n
        result.counts["reason"] = "그림 공급자 키 없음"
        return result
    run_id = new_id()
    done = failed = 0
    start, end = local_day_bounds(ctx.clock.now(), ctx.tz)
    for _ in range(max_jobs):
        job = prod.next_due(ctx.clock.now(), preset)
        if job is None:
            break
        if not under_cost_cap(
            ctx.spent_between(start, end), ctx.config.settings.daily_cost_cap_usd
        ):
            result.counts["cost_cap_hit"] = True
            result.counts["reason"] = "비용 상한에 닿아 그림 제작을 멈춤(남은 작업은 그대로 대기)"
            break
        if not prod.claim(job.id, run_id):
            continue
        ok = _run_one(ctx, prod, job, studio, result)
        done += ok
        failed += not ok
    result.counts["made"] = done
    if failed:
        result.counts["failed"] = failed
    return result


def cost_ref(job: ImageJob) -> str:
    """비용 원장 ref: 원고 id까지 넣는다("과금 안 됨 확인"이 다른 원고의 같은 자리를 풀지 않게, S15 리뷰)."""
    return f"{job.role}:{job.content_id or '-'}:{job.target}"


def _run_one(
    ctx: JobContext, prod: ProductionStore, job: ImageJob, studio, result: JobResult
) -> bool:
    cid = job.content_id
    # 보내기 직전에 spec을 다시 본다: 그 사이 장면이 바뀌었으면 돈을 쓰지 않고 취소
    if cid and job.role in ("panel", "sticker"):
        try:
            _gen, cast, slots = current_slots(ctx, prod, cid)
            slot = next((s for s in slots if s.target == job.target), None)
            now_hash = spec_for(ctx, prod, cid, slot, cast).hash if slot else None
        except (MissingReference, AdapterError) as e:
            prod.finish(job.id, "failed", error=str(e))
            _sync_slot_stage(ctx, prod, cid)
            return False
        if now_hash != job.spec_hash:
            prod.finish(
                job.id, "cancelled", error="보내기 전에 장면·캐릭터가 바뀌어 취소(비용 없음)"
            )
            _sync_slot_stage(ctx, prod, cid)
            return False
    s = job.spec
    refs = tuple(Path(r) for r in s.get("references") or [])
    spec = ImageSpec(
        prompt=s["prompt"],
        width=int(s["width"]),
        height=int(s["height"]),
        quality=s.get("quality"),
        transparent=bool(s.get("transparent")),
        references=refs,
        negative=s.get("negative", ""),
        ref=cost_ref(job),
    )
    prod.mark_sent(job.id)
    attempts = job.attempts + 1
    try:
        out = studio.make(spec)
    except ImageUnknown as e:
        prod.finish(
            job.id,
            "unknown",
            error=f"응답을 받지 못해 생성·과금 여부를 알 수 없습니다: {e}",
            cost_status="unknown",
        )
        result.errors.append(f"{job.target}: 응답 불명")
        _after(ctx, prod, job)
        return False
    except ImageRefused as e:
        cats = ", ".join(e.categories) or "정책"
        prod.finish(
            job.id,
            "failed",
            error=f"그림 요청이 차단됐습니다({cats}). 장면 지시를 바꿔 다시 요청하세요",
        )
        _after(ctx, prod, job)
        return False
    except FileNotFoundError as e:
        prod.finish(
            job.id,
            "failed",
            error=f"참조 이미지 파일이 없습니다: {Path(str(e.filename or e)).name}",
        )
        _after(ctx, prod, job)
        return False
    except Exception as e:  # noqa: BLE001  한도·과부하(재예약), 키·크레딧(영구), 형식 오류
        name = type(e).__name__
        if name == "ImageTransient" and attempts < MAX_ATTEMPTS:
            wait = getattr(e, "retry_after", None)
            prod.finish(
                job.id,
                "queued",
                error=f"잠깐 실패, 다시 예약: {e}",
                next_attempt_at=ctx.clock.now() + (timedelta(seconds=wait) if wait else RETRY_IN),
            )
            return False
        if name == "ImageUnavailable":
            ctx.notifier.notify("그림 제작 불가", str(e)[:200], "warn")
        prod.finish(job.id, "failed", error=f"{name}: {str(e)[:200]}")
        _after(ctx, prod, job)
        return False
    try:
        _apply_result(ctx, prod, job, out)
    except Exception as e:  # noqa: BLE001  파일 이동·기록 실패: 비용은 이미 확정, 작업은 실패로 남겨 사람에게
        log.exception("그림 결과 저장 실패: %s", job.id)
        prod.finish(
            job.id, "failed", error=f"결과 저장 실패(과금됨, 파일: {out.path}): {str(e)[:120]}"
        )
        _after(ctx, prod, job)
        return False
    return True


def _apply_result(ctx: JobContext, prod: ProductionStore, job: ImageJob, out) -> None:
    """유료 결과를 먼저 **보존**(파일 이동 + 이력·작업에 기록)하고, 덱 적용은 따로 한다.
    적용이 실패해도 파일·메타데이터는 남고, API를 다시 부르지 않고 "적용 다시 시도"로 이어 간다(S15 리뷰)."""
    if job.content_id is None:
        keep = ctx.media_dir / "studio" / "references"
        keep.mkdir(parents=True, exist_ok=True)
        dst = keep / f"{job.id}{out.path.suffix or '.png'}"
        shutil.move(str(out.path), dst)
        prod.save_result(job.id, None, str(dst), out.request_id, out.cost_status)
    else:
        cid = job.content_id
        dst_dir = content_dir(ctx.media_dir, cid) / "images"
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / f"{job.target}-{job.id[-8:]}{out.path.suffix}"
        shutil.move(str(out.path), dst)
        with ctx.repo.db.transaction() as cur:
            iid = prod.add_image(
                cur,
                cid=cid,
                target=job.target,
                role=job.role,
                source="ai",
                path=str(dst),
                sha=file_sha(dst),
                state="candidate",
                provider="openai",
                model=out.model,
                prompt=job.spec.get("prompt"),
                references=job.spec.get("references"),
                spec_hash=job.spec_hash,
                job_id=job.id,
                cost_usd=out.usd,
                cost_status=out.cost_status,
            )
            prod.save_result(job.id, iid, str(dst), out.request_id, out.cost_status, cur=cur)
    apply_saved_result(ctx, job.id)


def apply_saved_result(ctx: JobContext, job_id: str) -> str:
    """보존된 결과를 덱(또는 캐릭터 참조)에 적용. 실패하면 apply_failed로 남기고 이유를 기록한다. API 호출 없음."""
    prod = ProductionStore(ctx.repo.db, ctx.clock)
    job = prod.job(job_id)
    if job is None or job.status not in ("saved", "apply_failed") or not job.result_path:
        raise AdapterError("적용할 결과가 없습니다")
    try:
        status = (
            _apply_reference(ctx, job) if job.content_id is None else _apply_to_deck(ctx, prod, job)
        )
    except Exception as e:  # noqa: BLE001  파일·메타데이터는 남긴다
        log.exception("그림 결과 적용 실패: %s", job.id)
        prod.finish(
            job.id,
            "apply_failed",
            error=f"결과 적용 실패(그림은 보존, 다시 적용 가능): {str(e)[:160]}",
        )
        if job.content_id:
            _sync_slot_stage(ctx, prod, job.content_id)
        return "apply_failed"
    if job.content_id:
        if status == "succeeded" and job.role == "meme":
            review_meme_image(ctx, prod, job)
        if status == "succeeded":
            # 이 그림을 참조로 기다리던 자리(2번째 컷, 여러 장 밈의 다음 장)를 이어서 만든다(S18: 컷 이름에 묶지 않음)
            try:
                _g, _c, slots = current_slots(ctx, prod, job.content_id)
                waiting = [x.target for x in slots if x.chain_from == job.target]
            except AdapterError:
                waiting = []
            if waiting:
                enqueue_production(ctx, job.content_id, targets=waiting)
        _sync_slot_stage(ctx, prod, job.content_id)
    return status


def _apply_to_deck(ctx: JobContext, prod: ProductionStore, job: ImageJob) -> str:
    cid = job.content_id
    c = ctx.repo.get_content(cid)
    try:
        _gen, cast, slots = current_slots(ctx, prod, cid)
        slot = next((s for s in slots if s.target == job.target), None)
        current = spec_for(ctx, prod, cid, slot, cast).hash if slot else None
    except (MissingReference, AdapterError):
        slot, current = None, None
    apply = c is not None and c.status == "pending_review" and current == job.spec_hash and slot
    with ctx.repo.db.transaction() as cur:
        status = "succeeded_stale"
        if apply:
            gen = _with_asset(c.generated, slot, job.result_path, ai=True)
            if ctx.repo.update_generated_in(cur, cid, gen, prod._now()):
                prod.activate(cur, cid, job.target, job.result_image_id)
                status = "succeeded"
        cur.execute(
            "UPDATE image_jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
            [
                status,
                None
                if status == "succeeded"
                else "장면·캐릭터가 바뀐 뒤(또는 승인 뒤) 도착: 후보로 보관",
                prod._now(),
                job.id,
            ],
        )
    return status


def _apply_reference(ctx: JobContext, job: ImageJob) -> str:
    store = characters(ctx.config)
    pid = job.target.split(":", 1)[1]
    path = Path(job.result_path)
    store.add_reference(pid, path.read_bytes(), path.suffix or ".png", ai_made=True)
    path.unlink(missing_ok=True)
    prod = ProductionStore(ctx.repo.db, ctx.clock)
    prod.finish(job.id, "succeeded")
    return "succeeded"


def _with_asset(
    gen: GeneratedContent, slot: SlotPlan | None, path: str, ai: bool
) -> GeneratedContent:
    if slot is None:
        return gen
    slides = list(gen.deck.slides)
    s = slides[slot.slide]
    asset = ImageAsset(path=path, ai=ai)
    if slot.role == "panel":
        panels = list(s.panels)
        panels[slot.panel] = panels[slot.panel].model_copy(update={"image": asset})
        slides[slot.slide] = s.model_copy(update={"panels": panels})
    elif slot.role == "meme":
        slides[slot.slide] = s.model_copy(update={"image": asset})
    else:
        slides[slot.slide] = s.model_copy(update={"sticker": asset})
    deck = gen.deck
    labels = deck.labels
    if labels.ai_slides is None:
        # 레거시 덱(필드 없음)을 자산별 표기로 바꿀 때: 그때까지의 레거시 규칙(덱 플래그 + 배경 있는 장)을 장 번호로
        # 옮겨 둔다. 이미 [](새 콘텐츠)인 덱에는 레거시 플래그를 합치지 않는다(S15 리뷰 정정)
        legacy = [i for i, x in enumerate(slides) if x.background and labels.ai_generated_image]
        labels = labels.model_copy(update={"ai_slides": legacy})
    return gen.model_copy(
        update={"deck": deck.model_copy(update={"slides": slides, "labels": labels})}
    )


def _after(ctx: JobContext, prod: ProductionStore, job: ImageJob) -> None:
    if job.content_id:
        _sync_slot_stage(ctx, prod, job.content_id)


# ------------------------------------------------------------------ 사람이 고르는 것


def use_image(ctx: JobContext, cid: str, image_id: str) -> None:
    """이전 결과·후보를 이 슬롯 그림으로(재사용). 검토 대기 중에만."""
    prod = ProductionStore(ctx.repo.db, ctx.clock)
    img = prod.image(image_id)
    if img is None or img.content_id != cid:
        raise AdapterError("그림이 없습니다")
    c = ctx.repo.get_content(cid)
    if c is None or c.status != "pending_review":
        raise AdapterError("검토 대기 중인 원고만 고칠 수 있습니다")
    _gen, cast, slots = current_slots(ctx, prod, cid)
    slot = next((s for s in slots if s.target == img.target), None)
    if slot is None:
        raise AdapterError("이 원고에 그 자리가 없습니다")
    gen = _with_asset(c.generated, slot, img.path, ai=img.source == "ai")
    with ctx.repo.db.transaction() as cur:
        if not ctx.repo.update_generated_in(cur, cid, gen, prod._now()):
            raise AdapterError("검토 대기 중인 원고만 고칠 수 있습니다")
        prod.activate(cur, cid, img.target, image_id)
    _sync_slot_stage(ctx, prod, cid)


def upload_image(ctx: JobContext, cid: str, target: str, src: Path, source: str = "upload") -> None:
    """직접 올린 그림(실제 자료·직접 그린 그림)을 이 슬롯에. AI 표기 대상이 아니다."""
    prod = ProductionStore(ctx.repo.db, ctx.clock)
    c = ctx.repo.get_content(cid)
    if c is None or c.status != "pending_review":
        raise AdapterError("검토 대기 중인 원고만 고칠 수 있습니다")
    _gen, cast, slots = current_slots(ctx, prod, cid)
    slot = next((s for s in slots if s.target == target), None)
    if slot is None:
        raise AdapterError("이 원고에 그 자리가 없습니다")
    dst_dir = content_dir(ctx.media_dir, cid) / "images"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{target}-up-{new_id()[-8:]}{src.suffix}"
    shutil.copyfile(src, dst)
    gen = _with_asset(c.generated, slot, str(dst), ai=False)
    with ctx.repo.db.transaction() as cur:
        if not ctx.repo.update_generated_in(cur, cid, gen, prod._now()):
            raise AdapterError("검토 대기 중인 원고만 고칠 수 있습니다")
        prod.add_image(
            cur,
            cid=cid,
            target=target,
            role=slot.role,
            source=source,
            path=str(dst),
            sha=file_sha(dst),
            state="active",
        )
    _sync_slot_stage(ctx, prod, cid)


# ------------------------------------------------------------------ 슬롯 단계(추천)


def production_status(prod: ProductionStore, cid: str, slots: list[SlotPlan]) -> str:
    """ready | producing | unknown_image | failed_image | images_pending — 그림 자리 전체로 정한다."""
    if not slots:
        return "ready"
    latest = prod.latest_jobs(cid)
    if all(prod.active(cid, s.target) is not None for s in slots):
        return "ready"
    jobs = [latest[s.target] for s in slots if s.target in latest]
    if any(j.status in IN_FLIGHT for j in jobs):
        return "producing"
    if any(j.status == "unknown" for j in jobs):
        return "unknown_image"
    if any(j.status == "failed" for j in jobs):
        return "failed_image"
    return "images_pending"


def _sync_slot_stage(
    ctx: JobContext, prod: ProductionStore, cid: str, problems: dict[str, str] | None = None
) -> None:
    slots_store = SlotStore(ctx.repo.db, ctx.clock)
    sl = slots_store.by_content(cid)
    if sl is None:
        return
    try:
        _gen, _cast, slots = current_slots(ctx, prod, cid)
    except AdapterError:
        return
    stage = production_status(prod, cid, slots)
    if problems and stage == "images_pending":
        stage, err = "failed_image", "; ".join(f"{k}: {v}" for k, v in problems.items())
    else:
        latest = prod.latest_jobs(cid)
        errs = [
            latest[s.target].error for s in slots if s.target in latest and latest[s.target].error
        ]
        err = "; ".join(e for e in errs if e)[:300] or None
    if sl.stage != stage or (err or None) != sl.last_error:
        slots_store.set_stage(sl, stage, error=err if stage != "ready" else None)


def recover_image_jobs(ctx: JobContext) -> tuple[int, int]:
    """재시작 복구: 안 보낸 작업은 다시 대기, 보냈는데 결과를 모르는 작업은 응답 불명(자동 재시도 금지)."""
    prod = ProductionStore(ctx.repo.db, ctx.clock)
    return prod.recover()


MEME_REVIEW_TAG = "완성 그림 검토"
_PART_LABEL = {"scene": "구도", "composite": "합성", "text": "글자", "idea": "발상"}


def review_meme_image(ctx: JobContext, prod: ProductionStore, job) -> None:
    """완성된 밈 그림을 보고 재미의 핵심이 살아 있는지 검토한다(S18). 문제는 검토 사유에 단계별로 남기고,
    그림 쪽 문제(구도·합성)면 그 지적을 장면 지시에 붙여 **한 번만** 다시 만든다. 글·발상 문제는 사람이 고친다.
    검토 공급자가 없거나 실패하면 조용히 넘어간다(사람 검토는 그대로)."""
    from nsm.ports import MemeImageReviewer
    from nsm.store.slots import SlotStore

    gen, cid = ctx.text_generator, job.content_id
    if not isinstance(gen, MemeImageReviewer) or not cid or not job.result_path:
        return
    sl = SlotStore(ctx.repo.db, ctx.clock).by_content(cid)
    plan = (sl.plan or {}) if sl else {}
    try:
        _g, _cast, slots = current_slots(ctx, prod, cid)
        slot = next(s for s in slots if s.target == job.target)
        c = ctx.repo.get_content(cid)
        page = c.generated.deck.slides[slot.slide] if c else None
        overlays = "; ".join(
            f"{o.kind} '{o.text}' at ({int(o.x)}%,{int(o.y)}%, w {int(o.w)}%)"
            for o in getattr(page, "overlays", [])
        )
        brief = (
            f"재미의 핵심(core): {plan.get('core') or '-'}\n웃음 방식: {plan.get('mechanism') or '-'}\n"
            f"형식: {plan.get('form') or '-'} · 만드는 방법: {plan.get('make', 'restage')}\n"
            f"장면 지시: {slot.scene}\n얹을 글: {overlays or '없음'}"
        )
        out = gen.review_meme_image(job.result_path, brief)
        problems = [
            (str(p.get("part")), str(p.get("note", "")).strip()[:120])
            for p in out.get("problems") or []
            if p.get("part") in _PART_LABEL and str(p.get("note", "")).strip()
        ][:3]
    except Exception as e:  # noqa: BLE001  검토는 있으면 좋고 없으면 사람이 본다
        log.info("완성 밈 그림 검토 실패(넘어감): %s", e)
        return
    c = ctx.repo.get_content(cid)
    if c is None:
        return
    kept = [
        x for x in (c.review_reason or "").split("; ") if x and not x.startswith(MEME_REVIEW_TAG)
    ]
    if out.get("ok") and not problems:
        ctx.repo.set_review_reason(cid, "; ".join(kept))
        return
    note = " / ".join(f"{_PART_LABEL[p]}: {n}" for p, n in problems) or "핵심이 잘 안 보임"
    ctx.repo.set_review_reason(cid, "; ".join([*kept, f"{MEME_REVIEW_TAG}: {note}"]))
    fix = [n for p, n in problems if p in ("scene", "composite")]
    # 같은 자리에 그림이 이미 둘 이상이면(자동 수정을 한 번 했으면) 더 만들지 않는다: 비용·반복 방지
    if fix and len(prod.images(cid, job.target)) < 2:
        prod.set_scene(cid, job.target, f"{slot.scene} (고칠 점: {' / '.join(fix)})"[:900])
        enqueue_production(ctx, cid, targets=[job.target], force=True)
