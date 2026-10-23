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

import dataclasses
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from nsm.adapters.sources.og_image import PhotoCandidate
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
from nsm.domain.topics import interleave_by_topic, topic_order
from nsm.engine.collect import excluded_word, run_collect
from nsm.engine.context import JobContext, JobResult
from nsm.engine.generate import _RETRY_LATER, _SKIP_MATERIAL, SOURCES_PER_CONTENT, build_draft
from nsm.engine.memes import discover_for, run_meme_slot
from nsm.store.picks import Pick, PicksStore, SlotConflictError
from nsm.store.slots import Slot, SlotStore

log = logging.getLogger(__name__)

RECENT_DAYS = 14  # 최근 이 기간에 다룬 사건은 다시 추천하지 않는다
DISMISSED_DAYS = 30  # '관심 없음'으로 버린 사건을 기억하는 기간
MAX_CANDIDATES = 10  # 생성까지 시도할 사건 수 상한(비용)
# 사진 수집기: 원문 URL → 후보 한 장(예전 형태) 또는 여러 장(S18 기본: 대표·본문 이미지)
PhotoFetcher = Callable[[str, Path], "PhotoCandidate | list[PhotoCandidate] | None"]


MAX_PHOTOS = 10  # 원고당 사진 후보(사진 위주 카드: 상품마다 사진이 필요할 수 있다, S18)


def fetch_photo_candidates(url: str, dest: Path) -> list[PhotoCandidate]:
    from nsm.adapters.sources.og_image import fetch_article_images

    return fetch_article_images(url, dest, max_images=8)


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


SAME_EVENT_LABELS = ["same_event", "different_event"]
SAME_EVENT_CONFIDENCE = 0.7


def _same_event_as_made(ctx: JobContext, title: str, text: str, made_titles: list[str]) -> bool:
    """제목 낱말 겹침이 낮아도 같은 사건인지 모델에 묻는다(S16: "200만 눈앞"과 "200만 육박"이 둘 다 추천됨).
    낱말 규칙 분류기뿐이거나 호출이 실패하면 묻지 않고 통과(다른 사건으로 본다)."""
    clf = ctx.relevance_classifier
    if not made_titles or clf is None or ctx.classifier_name == "keyword":
        return False
    made = "\n".join(f"- {t}" for t in made_titles)
    instruction = (
        "오늘 이미 만든 소식 목록과 새 자료가 **같은 사건·발표**를 다루면 same_event, 다른 사건이면 "
        "different_event. 같은 작품·인물이라도 다른 발표(예: 새 굿즈 vs 관객 수)는 다른 사건이다. "
        "같은 기록·발표를 다른 언론사가 쓴 것은 같은 사건이다.\n\n이미 만든 소식:\n" + made
    )
    try:
        c = clf.classify(f"{title}\n{text[:800]}", SAME_EVENT_LABELS, instruction)
    except Exception:  # noqa: BLE001  확인 실패가 추천을 막지 않게
        log.warning("같은 사건 확인 실패(통과): %s", title[:40])
        return False
    return c.label == "same_event" and c.confidence >= SAME_EVENT_CONFIDENCE


def _fill_info_slot(
    ctx: JobContext,
    slot: Slot,
    ranked: list[Cluster],
    made_tokens: list[set[str]],
    store: PicksStore,
    result: JobResult,
    fetch_photo: PhotoFetcher,
    now,
    made_titles: list[str] | None = None,
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
        lead = chosen[0][0]
        if made_titles is not None and _same_event_as_made(ctx, lead.title, lead.text, made_titles):
            result.counts["same_event_skipped"] = result.counts.get("same_event_skipped", 0) + 1
            continue
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
        if made_titles is not None:
            made_titles.append(lead.title)
        result.counts.setdefault("templates", []).append(d.template)
        if s.picks.photo_candidates:
            _attach_photo(
                ctx, store, content.id, [i.url for i, _ in chosen if i.url], fetch_photo, d.gen.deck
            )
        return "made", ""
    return "empty", "쓸 만한 새 자료가 없음" if not ranked else "서로 다른 소재가 모자람"


def run_picks(
    ctx: JobContext,
    manual: bool = False,
    fetch_photo: PhotoFetcher = fetch_photo_candidates,
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

    # 관심 주제 순서: 종류별로 최근에 가장 오래 안 다룬 주제부터(슬롯 번호로 돌리면 하루 1개일 때 첫 주제만 반복)
    topics = list(ctx.config.persona.topics)
    info_order = topic_order(topics, ctx.repo.topic_last_used("info"))
    meme_order = topic_order(topics, ctx.repo.topic_last_used("meme"))
    ranked = interleave_by_topic(_ranked_clusters(ctx, store, now), info_order)
    total_ranked = len(ranked)
    result.counts["candidates"] = total_ranked
    info_titles = [c.sorted_items()[0][0].title for c in ranked[:10]]
    made_tokens: list[set[str]] = []
    discovered: dict = {}  # 주제 → 밈 탐색 결과(이번 실행 동안만)
    made_titles: list[str] = []  # 이번 실행에서 만든 정보 원고의 대표 자료 제목(같은 사건 확인용)
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
                args = (
                    ranked,
                    made_tokens,
                    store,
                    slots,
                    result,
                    fetch_photo,
                    now,
                    info_titles,
                    meme_order,
                    discovered,
                    made_titles,
                )
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
    ctx,
    slot,
    kind,
    ranked,
    made_tokens,
    store,
    slots,
    result,
    fetch_photo,
    now,
    info_titles,
    meme_order=None,
    discovered: dict | None = None,
    made_titles: list[str] | None = None,
) -> tuple[str, str]:
    """종류에 따라 정보/밈 경로로. 돌려주는 값: (made|empty|failed|retry, 이유)."""
    if kind == "info":
        return _fill_info_slot(
            ctx, slot, ranked, made_tokens, store, result, fetch_photo, now, made_titles
        )
    writer = ctx.scenario_writer
    if writer is None:
        return "failed", "밈 대본을 쓸 공급자가 없음(OpenAI 또는 Claude 키)"
    order = meme_order if meme_order is not None else list(ctx.config.persona.topics)
    if not order:
        return "empty", "관심 주제가 없음(설정 → 페르소나 → 관심 주제)"
    topic = order.pop(0)  # 이번 실행에서 다음 밈은 다음 주제로
    order.append(topic)
    # 같은 실행에서 같은 주제는 한 번만 탐색한다(밈 슬롯이 둘이면 같은 검색을 두 번 하지 않게)
    cache = discovered if discovered is not None else {}
    if topic not in cache:
        cache[topic] = discover_for(ctx, topic)
    mc = dataclasses.replace(cache[topic], hook_title="", hook_sources=())
    # 그 주제의 최근 소식 하나를 훅으로(오늘 정보 슬롯이 쓴 사건은 빼서 같은 사건 중복 추천을 피한다)
    for cl in ranked:
        if cl.topic == topic and not any(
            jaccard(cl.tokens, t) >= SAME_EVENT_JACCARD for t in made_tokens
        ):
            items = cl.sorted_items()[:SOURCES_PER_CONTENT]
            mc.hook_title = items[0][0].title
            mc.hook_sources = tuple(i for i, _ in items)
            break
    result.counts.setdefault("meme_access", {}).update(
        {f"{topic}·{k}": v for k, v in mc.access.items()}
    )
    out = run_meme_slot(ctx, slot, writer, store, slots, info_titles, mc)
    if out.ok:
        # 주인공·참조가 없으면 그림 작업을 넣지 않는다: "캐릭터를 고르세요" 안내를 실패로 덮지 않게(S16 리뷰)
        if (
            ctx.config.workspace.production_mode == "full"
            and out.content_id
            and not out.needs_character
        ):
            # 완성본 모드(S15): 대본까지 만들었으면 그림 작업을 넣는다(예산은 images 잡이 장마다 확인)
            from nsm.engine.production import enqueue_production

            try:
                enqueue_production(ctx, out.content_id)
            except Exception as e:  # noqa: BLE001  원고는 저장됐다: 슬롯에 이유를 남기고 화면에서 다시 시도
                log.exception("그림 예약 실패: %s", out.content_id)
                sl = slots.get(slot.run_date, slot.requested_kind, slot.slot_no)
                if sl is not None:
                    slots.set_stage(sl, "failed_image", error=f"그림 예약 실패: {str(e)[:160]}")
        return "made", ""
    if out.retry_later:
        return "retry", out.reason
    return ("empty" if out.empty else "failed"), out.reason


def run_slots_retry(ctx: JobContext) -> JobResult:
    """재시도 시각이 된 슬롯만(15분마다). 계획·수집은 하지 않는다."""
    return run_picks(ctx, retry_only=True)


def deck_shape(deck) -> list | None:
    """배치를 계산한 때의 덱 모양(장 종류·상품 이름). 그 뒤 덱이 바뀌면 어긋난 자리에 사진을 넣지 않게(S18 리뷰)."""
    if deck is None:
        return None
    return [[s.kind, [it.name for it in getattr(s, "items", None) or []]] for s in deck.slides]


def _attach_photo(
    ctx: JobContext,
    store: PicksStore,
    cid: str,
    urls,
    fetch_photo: PhotoFetcher,
    deck=None,
) -> None:
    """사진 후보(S11) → 여러 장 + 장별 배치 제안(S18). 기본 수집기면 자료 기사마다 대표·본문 이미지를 모으고,
    내용으로 짝지은 배치를 candidates.json에 둔다. 사진이 하나도 없으면 검토 사유에 남긴다(조용히 완성 처리 안 함)."""
    import json

    if isinstance(urls, str):
        urls = [urls]
    urls = [u for u in urls if u and not u.startswith("file:")][:3]
    dest = picks_dir(ctx) / cid
    cands = []
    for k, u in enumerate(urls):
        try:
            # 수집기는 한 장(예전 형태) 또는 여러 장(S18 기본)을 돌려준다. 자료마다 폴더를 나눠 파일 이름이 겹치지 않게
            got = fetch_photo(u, dest if k == 0 else dest / f"src{k}")
            cands += [c for c in (got if isinstance(got, list) else [got]) if c is not None]
        except Exception as e:  # noqa: BLE001  후보는 있으면 좋고 없어도 된다
            log.info("사진 후보 수집 실패(%s): %s", u, e)
        if len(cands) >= MAX_PHOTOS:
            break
    for extra in cands[MAX_PHOTOS:]:  # 쓰지 않을 후보 파일은 남기지 않는다(S18 리뷰)
        Path(extra.path).unlink(missing_ok=True)
    if not cands:
        if urls:
            c = ctx.repo.get_content(cid)
            if c is not None:
                ctx.repo.set_review_reason(
                    cid,
                    "; ".join(
                        filter(
                            None,
                            [
                                c.review_reason,
                                "소재 사진 없음: 직접 올리거나 다른 자료로 보강하세요",
                            ],
                        )
                    ),
                )
        return
    rows = [
        {
            "path": str(c.path),
            "image_url": c.image_url,
            "source_url": c.source_url,
            "alt": getattr(c, "alt", ""),
            "kind": getattr(c, "kind", "og"),
            "size": [c.width, c.height],
        }
        for c in cands[:MAX_PHOTOS]
    ]
    placement = None
    if deck is not None:
        from nsm.engine.photo_match import match_photos

        try:
            p = match_photos(ctx, deck, rows)
            placement = {
                "cover": p.cover,
                "slides": {str(k): v for k, v in p.slides.items()},
                "items": {f"{i}.{j}": v for (i, j), v in p.items.items()},
                "notes": {str(k): v for k, v in p.notes.items()},
                "method": p.method,
            }
        except Exception as e:  # noqa: BLE001
            log.info("사진 배치 제안 실패: %s", e)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "candidates.json").write_text(
        json.dumps(
            {"candidates": rows, "placement": placement, "shape": deck_shape(deck)},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    first = (
        rows[placement["cover"]] if placement and placement.get("cover") is not None else rows[0]
    )
    store.set_photo(
        cid,
        "candidate",
        image_url=first["image_url"],
        source_url=first["source_url"],
        path=first["path"],
    )
