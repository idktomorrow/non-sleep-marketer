"""picks 잡(S11 제안, ADR 0050): 자료를 모은 뒤 최근 소식 중 서로 다른 소재 최대 N개를 완성 원고로 추천한다.

- 수집 → 사건 묶기 → 순위 → 생성 순서를 이 잡 안에서 지킨다(예약 시각 순서에 기대지 않는다)
- 추천 원고는 전부 검토 대기(pending_review)다. 자동 승인·플랫폼 배정·일일 생성 목표와 섞이지 않는다
- 유료 AI 배경은 만들지 않는다. 기사 대표 이미지(og:image)는 후보로만 받아 두고 사람이 "쓰기"를 눌러야 카드에 들어간다
- "유행 순위"가 아니라 "최근 소식 추천"이다: 인기도 관측은 없고, 공개 시각(published_at)·관련성·자료 수로 고른다
"""

from __future__ import annotations

import logging
from collections.abc import Callable
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
from nsm.store.picks import Pick, PicksStore

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


def run_picks(
    ctx: JobContext, manual: bool = False, fetch_photo: PhotoFetcher = fetch_photo_candidate
) -> JobResult:
    result = JobResult("picks")
    now = ctx.clock.now()
    ok, why = should_act(now, ctx.config.persona, ctx.tz)
    if not ok:
        result.skipped = why
        return result
    s = ctx.config.settings
    if not s.picks.enabled and not manual:
        result.skipped = "추천 모드가 꺼져 있음(설정에서 켜거나 '지금 실행'으로)"
        return result
    if ctx.text_generator is None:
        result.skipped = "Claude API 키가 없어 생성할 수 없음 (`nsm init`)"
        return result
    store = PicksStore(ctx.repo.db, ctx.clock)
    today = local(now, ctx.tz).date()
    already = store.count_for_date(today)
    need = s.picks.count - already if not manual else s.picks.count
    if need <= 0:
        result.skipped = f"오늘 추천 {already}건을 이미 만들었음"
        return result

    # 1) 수집을 먼저(순서 의존). 이미 모은 글은 중복 제거되므로 두 번 돌아도 안전하다
    collected = run_collect(ctx)
    result.counts["collected"] = collected.counts.get("new", 0)
    result.errors.extend(f"수집: {e}" for e in collected.errors)

    # 2) 후보 → 사건 묶음 → 순위
    words = s.collect.exclude_words
    candidates = [
        (item, rel, topic)
        for item, rel, topic in ctx.repo.unused_relevant_items(s.relevance_threshold)
        if topic and not excluded_word(item, words)
    ]
    clusters = cluster_candidates(candidates, s.picks.aliases)
    # 최근 원고 제목은 "대표 주제: 제목" 꼴이라 주제 낱말이 겹침을 부풀린다 → 제목 부분만 비교(S11 리뷰)
    recent = [
        t.split(": ", 1)[-1] for t in store.recent_topics_text(now - timedelta(days=RECENT_DAYS))
    ]
    exclude = recent + store.dismissed_fingerprints(now - timedelta(days=DISMISSED_DAYS))
    ranked = rank_clusters(clusters, exclude, now, s.picks.fresh_hours, s.picks.aliases)
    result.counts["candidates"] = len(ranked)

    # 3) 서로 다른 사건을 위에서부터 완성 원고로
    start, end = local_day_bounds(now, ctx.tz)
    made: list[str] = []
    made_tokens: list[
        set[str]
    ] = []  # 같은 사건이 다른 주제로 분류돼 두 묶음이 됐어도 한 번만(S11 리뷰)
    rank = already
    for cluster in ranked[:MAX_CANDIDATES]:
        if len(made) >= need:
            break
        if any(jaccard(cluster.tokens, t) >= SAME_EVENT_JACCARD for t in made_tokens):
            continue
        spent = ctx.repo.cost_between(start, end)
        if not under_cost_cap(spent, s.daily_cost_cap_usd):
            result.counts["cost_cap_hit"] = True
            result.counts["reason"] = f"비용 상한 ${s.daily_cost_cap_usd:.2f} 도달"
            break
        chosen = cluster.sorted_items()[:SOURCES_PER_CONTENT]
        d = build_draft(ctx, (cluster.topic, chosen), result, with_background=False)
        if d == _RETRY_LATER:
            result.counts["reason"] = "Claude API 장애로 중단(다음 실행에 다시)"
            break
        if d == _SKIP_MATERIAL or isinstance(d, str):
            continue
        fresh = freshness(cluster.published_at, now, s.picks.fresh_hours)
        rank += 1
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
            run_date=today,
            rank=rank,
            reason=_reason(cluster, fresh),
            freshness=fresh,
            published_at=cluster.published_at,
            title_fingerprint=cluster.fingerprint,
        )
        store.save(content, persona_topic=cluster.topic, item_ids=d.item_ids, pick=pick)
        made.append(content.id)
        made_tokens.append(cluster.tokens)
        result.counts.setdefault("templates", []).append(d.template)
        if s.picks.photo_candidates:
            _attach_photo(ctx, store, content.id, chosen[0][0].url, fetch_photo)

    result.counts["made"] = len(made)
    if len(made) < need and "reason" not in result.counts:
        result.counts["reason"] = (
            "쓸 만한 새 자료가 없음" if not ranked else f"서로 다른 소재가 {len(ranked)}개뿐"
        )
    if made:
        ctx.notifier.notify(
            "오늘의 추천",
            f"최근 소식으로 원고 {len(made)}건을 추천합니다. 오늘 화면에서 고르세요",
            "info",
        )
    result.ok = not (result.errors and not made)
    return result


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
