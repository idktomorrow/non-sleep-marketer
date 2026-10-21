"""picks 잡(S11·S14 제안, ADR 0051·0057): 하루 "정보 N + 밈 M" 슬롯을 계획하고 채운다.

- 슬롯(pick_slots)은 계획 단계부터 존재한다. 같은 날 재실행·동시 실행은 슬롯을 claim해 중복 생성하지 않는다.
  목표를 낮추면 아직 시작하지 않은 초과 슬롯만 제외하고 완성된 것은 유지한다. 수동 실행도 슬롯 상한 안에서만.
- 정보 슬롯: 수집 → 사건 묶기 → 순위 → 생성(기존). 밈 슬롯: 별도 경로(memes.py). 실패는 슬롯 상태로 남고
  재시도는 next_attempt_at으로 재예약(slots_retry 잡). 자료 해제는 '관심 없음'에서만.

- 수집 → 사건 묶기 → 순위 → 생성 순서를 이 잡 안에서 지킨다(예약 시각 순서에 기대지 않는다)
- 추천 원고는 전부 검토 대기(pending_review)다. 자동 승인·플랫폼 배정·일일 생성 목표와 섞이지 않는다
- 유료 AI 배경은 만들지 않는다. 기사 대표 이미지(og:image)는 후보로만 받아 두고 사람이 "쓰기"를 눌러야 카드에 들어간다
- "유행 순위"가 아니라 "최근 소식 추천"이다: 인기도 관측은 없고, 공개 시각(published_at)·관련성·자료 수로 고른다
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from nsm.adapters.sources.og_image import PhotoCandidate, fetch_photo_candidate
from nsm.domain.clock import new_id
from nsm.domain.models import Content
from nsm.domain.picks_policy import (
    SAME_EVENT_JACCARD,
    Cluster,
    cluster_candidates,
    freshness,
    jaccard,
    rank_clusters,
)
from nsm.domain.policy import local, local_day_bounds, should_act, under_cost_cap
from nsm.engine.collect import excluded_word, run_collect
from nsm.engine.context import JobContext, JobResult
from nsm.engine.generate import _RETRY_LATER, _SKIP_MATERIAL, SOURCES_PER_CONTENT, build_draft
from nsm.engine.memes import run_meme_slot
from nsm.store.picks import Pick, PicksStore, SlotConflictError
from nsm.store.slots import Slot, SlotStore

log = logging.getLogger(__name__)

RECENT_DAYS = 14  # 최근 이 기간에 다룬 사건은 다시 추천하지 않는다
DISMISSED_DAYS = 30  # '관심 없음'으로 버린 사건을 기억하는 기간
MAX_CANDIDATES = 10  # 생성까지 시도할 사건 수 상한(비용)
PhotoFetcher = Callable[[str, Path], PhotoCandidate | None]


def picks_dir(ctx: JobContext) -> Path:
    return ctx.media_dir / "picks"


def _reason(cluster: Cluster, fresh: str) -> str:
    n = len(cluster.items)
    when = {"fresh": "최근 소식", "stale": "오래된 자료", "unknown": "날짜 확인 필요"}[fresh]
    return f"{when} · 자료 {n}건 · 주제 '{cluster.topic}'"


@dataclass
class SlotPlan:
    info: int
    meme: int
    source: str = "workspace"  # workspace | override

    @property
    def total(self) -> int:
        return self.info + self.meme


PLAN_OVERRIDE_KEY = (
    "picks.plan_override"  # 프리셋 DB settings_kv: {"date": "YYYY-MM-DD", "info": n, "meme": m}
)
RETRY_IN = timedelta(minutes=15)


def effective_plan(ctx: JobContext, today) -> SlotPlan:
    """오늘의 추천 구성: 오늘만 바꾼 값(프리셋 DB)이 있으면 그것, 아니면 작업 공간 설정."""
    ov = ctx.repo.get_setting(PLAN_OVERRIDE_KEY) or {}
    if ov and str(ov.get("date")) == today.isoformat():
        return SlotPlan(int(ov.get("info", 0)), int(ov.get("meme", 0)), "override")
    w = ctx.config.workspace
    return SlotPlan(w.info_count, w.meme_count)


def _ranked_clusters(ctx: JobContext, store: PicksStore, now) -> list[Cluster]:
    s = ctx.config.settings
    words = s.collect.exclude_words
    candidates = [
        (item, rel, topic)
        for item, rel, topic in ctx.repo.unused_relevant_items(s.relevance_threshold)
        if topic and not excluded_word(item, words)
    ]
    clusters = cluster_candidates(candidates, ctx.config.workspace.aliases)
    # 최근 원고 제목은 "대표 주제: 제목" 꼴이라 주제 낱말이 겹침을 부풀린다 → 제목 부분만 비교(S11 리뷰)
    recent = [
        t.split(": ", 1)[-1] for t in store.recent_topics_text(now - timedelta(days=RECENT_DAYS))
    ]
    exclude = recent + store.dismissed_fingerprints(now - timedelta(days=DISMISSED_DAYS))
    return rank_clusters(clusters, exclude, now, s.picks.fresh_hours, ctx.config.workspace.aliases)


def _fill_info_slot(
    ctx: JobContext,
    slot: Slot,
    ranked: list[Cluster],
    made_tokens: list[set[str]],
    store: PicksStore,
    result: JobResult,
    fetch_photo: PhotoFetcher,
    now,
) -> tuple[str, str]:
    """정보 슬롯 하나를 채운다. 돌려주는 값: (결과, 이유). 결과 ∈ made | empty | retry | skip_all."""
    s = ctx.config.settings
    tried = 0
    while ranked and tried < MAX_CANDIDATES:
        cluster = ranked.pop(0)
        tried += 1
        if any(jaccard(cluster.tokens, t) >= SAME_EVENT_JACCARD for t in made_tokens):
            continue
        chosen = cluster.sorted_items()[:SOURCES_PER_CONTENT]
        d = build_draft(ctx, (cluster.topic, chosen), result, with_background=False)
        if d == _RETRY_LATER:
            return "retry", "AI API 장애로 중단(다음 실행에 다시)"
        if d == _SKIP_MATERIAL or isinstance(d, str):
            continue
        fresh = freshness(cluster.published_at, now, s.picks.fresh_hours)
        content = Content(
            id=new_id(),
            status="pending_review",  # 추천은 늘 사람이 고른다
            generated=d.gen,
            relevance_confidence=d.relevance,
            is_ad=d.is_ad,
            created_at=now,
            review_reason="; ".join(filter(None, ["오늘의 추천", d.reason])),
        )
        pick = Pick(
            content_id=content.id,
            run_date=slot.run_date,
            rank=slot.slot_no,
            reason=_reason(cluster, fresh),
            freshness=fresh,
            published_at=cluster.published_at,
            title_fingerprint=cluster.fingerprint,
            kind="info",
            slot_key=slot.key,
        )
        store.save(
            content, persona_topic=cluster.topic, item_ids=d.item_ids, pick=pick, slot_stage="ready"
        )
        made_tokens.append(cluster.tokens)
        result.counts.setdefault("templates", []).append(d.template)
        if s.picks.photo_candidates:
            _attach_photo(ctx, store, content.id, chosen[0][0].url, fetch_photo)
        return "made", ""
    return "empty", "쓸 만한 새 자료가 없음" if not ranked else "서로 다른 소재가 모자람"


def run_picks(
    ctx: JobContext,
    manual: bool = False,
    fetch_photo: PhotoFetcher = fetch_photo_candidate,
    retry_only: bool = False,
) -> JobResult:
    """오늘의 추천(S14: 슬롯 기반). retry_only=True면 계획·수집 없이 재시도 시각이 된 슬롯만 채운다."""
    result = JobResult("picks" if not retry_only else "slots_retry")
    now = ctx.clock.now()
    ok, why = should_act(now, ctx.config.persona, ctx.tz)
    if not ok:
        result.skipped = why
        return result
    s = ctx.config.settings
    if not s.picks.enabled and not manual and not retry_only:
        result.skipped = "추천 모드가 꺼져 있음(설정에서 켜거나 '지금 실행'으로)"
        return result
    if ctx.text_generator is None:
        result.skipped = (
            ctx.text_generator_reason
            or "AI 텍스트 공급자 API 키가 없어 생성할 수 없음(설정 → AI 공급자·키)"
        )
        return result
    store = PicksStore(ctx.repo.db, ctx.clock)
    slots = SlotStore(ctx.repo.db, ctx.clock)
    preset = s.preset
    today = local(now, ctx.tz).date()
    run_id = new_id()

    if retry_only:
        due = slots.due(now, preset)
        if not due:
            result.skipped = "다시 시도할 슬롯이 없음"
            return result
        dates = sorted({d.run_date for d in due})
    else:
        plan = effective_plan(ctx, today)
        if plan.total <= 0:
            result.skipped = "추천 개수가 0(설정에서 정보·밈 추천 수를 정하세요)"
            return result
        planned = slots.plan(today, plan.info, plan.meme, preset)
        result.counts["planned_info"], result.counts["planned_meme"] = plan.info, plan.meme
        if planned["cancelled"]:
            result.counts["cancelled"] = planned["cancelled"]
        pending = [x for x in slots.list_for_date(today) if x.stage == "planned"]
        if not pending:
            result.skipped = (
                "오늘 슬롯이 모두 찼음(정보·밈 계획만큼 이미 만들었거나 실패·비어 있음)"
            )
            return result
        # 1) 수집을 먼저(순서 의존). 이미 모은 글은 중복 제거되므로 두 번 돌아도 안전하다
        collected = run_collect(ctx)
        result.counts["collected"] = collected.counts.get("new", 0)
        result.errors.extend(f"수집: {e}" for e in collected.errors)
        dates = [today]

    ranked = _ranked_clusters(ctx, store, now)
    total_ranked = len(ranked)
    result.counts["candidates"] = total_ranked
    info_titles = [c.sorted_items()[0][0].title for c in ranked[:10]]
    made_tokens: list[set[str]] = []
    start, end = local_day_bounds(now, ctx.tz)
    made = {"info": 0, "meme": 0}
    failed = empty = 0
    stop_reason = ""

    def over_cap() -> bool:
        if under_cost_cap(ctx.spent_between(start, end), s.daily_cost_cap_usd):
            return False
        result.counts["cost_cap_hit"] = True
        return True

    for run_date in dates:
        for kind in ("info", "meme"):
            while not stop_reason:
                if over_cap():
                    stop_reason = f"비용 상한 ${s.daily_cost_cap_usd:.2f} 도달"
                    break
                slot = slots.claim(run_date, kind, run_id, now)
                if slot is None:
                    break
                args = (ranked, made_tokens, store, slots, result, fetch_photo, now, info_titles)
                outcome, reason = _guarded_fill(ctx, slot, kind, *args)
                filled_kind = kind
                if outcome == "empty" and ctx.config.workspace.allow_substitute:
                    other = "meme" if kind == "info" else "info"
                    alt, alt_reason = _guarded_fill(ctx, slot, other, *args)
                    if alt != "empty":  # 대체 결과(성공·실패·장애)를 그대로 따른다(S14 리뷰)
                        outcome, reason, filled_kind = alt, alt_reason, other
                if outcome == "made":
                    made[filled_kind] += 1
                elif outcome == "cancelled":
                    continue  # 저장은 되돌려졌고 슬롯은 취소 상태 그대로
                elif outcome in ("retry", "failed"):
                    slots.fail(slot, "failed_text", reason, RETRY_IN)
                    failed += 1
                    if outcome == "retry":
                        stop_reason = reason
                else:  # empty
                    empty += 1
                    slots.set_stage(slot, "empty", error=reason)
                    break  # 이 종류는 더 못 채운다

    result.counts["made_info"], result.counts["made_meme"] = made["info"], made["meme"]
    result.counts["made"] = made["info"] + made["meme"]
    if failed:
        result.counts["failed"] = failed
    if empty:
        result.counts["empty"] = empty
    if stop_reason:
        result.counts["reason"] = stop_reason
    elif empty and "reason" not in result.counts:
        result.counts["reason"] = (
            "쓸 만한 새 자료가 없음" if not total_ranked else f"서로 다른 소재가 {total_ranked}개뿐"
        )
    if result.counts["made"]:
        ctx.notifier.notify(
            "오늘의 추천",
            f"원고 {result.counts['made']}건(정보 {made['info']}·밈 {made['meme']})을 추천합니다. 오늘 화면에서 고르세요",
            "info",
        )
    result.ok = not (result.errors and not result.counts["made"])
    return result


def _guarded_fill(ctx, slot, kind, *args) -> tuple[str, str]:
    """예상 못 한 예외(검증·DB·사진 코드)가 잡 전체를 끊고 슬롯을 '쓰는 중'에 남기지 않게(S14 리뷰)."""
    try:
        return _fill_slot(ctx, slot, kind, *args)
    except SlotConflictError as e:  # 실행 중에 목표가 줄어 슬롯이 취소됐다: 원고는 되돌려졌다
        log.info("%s", e)
        return "cancelled", str(e)
    except Exception as e:  # noqa: BLE001
        log.exception("추천 슬롯 %s 채우기 실패", slot.key)
        return "failed", f"{type(e).__name__}: {str(e)[:160]}"


def _fill_slot(
    ctx, slot, kind, ranked, made_tokens, store, slots, result, fetch_photo, now, info_titles
) -> tuple[str, str]:
    """종류에 따라 정보/밈 경로로. 돌려주는 값: (made|empty|failed|retry, 이유)."""
    if kind == "info":
        return _fill_info_slot(ctx, slot, ranked, made_tokens, store, result, fetch_photo, now)
    writer = ctx.scenario_writer
    if writer is None:
        return "failed", "밈 대본을 쓸 공급자가 없음(OpenAI 또는 Claude 키)"
    out = run_meme_slot(ctx, slot, writer, store, slots, info_titles)
    if out.ok:
        if ctx.config.workspace.production_mode == "full" and out.content_id:
            # 완성본 모드(S15): 대본까지 만들었으면 그림 작업을 넣는다(예산은 images 잡이 장마다 확인)
            from nsm.engine.production import enqueue_production

            enqueue_production(ctx, out.content_id)
        return "made", ""
    if out.retry_later:
        return "retry", out.reason
    return ("empty" if out.empty else "failed"), out.reason


def run_slots_retry(ctx: JobContext) -> JobResult:
    """재시도 시각이 된 슬롯만(15분마다). 계획·수집은 하지 않는다."""
    return run_picks(ctx, retry_only=True)


def _attach_photo(
    ctx: JobContext, store: PicksStore, cid: str, url: str | None, fetch_photo: PhotoFetcher
) -> None:
    if not url or url.startswith("file:"):
        return
    try:
        cand = fetch_photo(url, picks_dir(ctx) / cid)
    except Exception as e:  # noqa: BLE001  후보는 있으면 좋고 없어도 된다
        log.info("사진 후보 수집 실패(%s): %s", url, e)
        return
    if cand is None:
        return
    store.set_photo(
        cid, "candidate", image_url=cand.image_url, source_url=cand.source_url, path=str(cand.path)
    )
