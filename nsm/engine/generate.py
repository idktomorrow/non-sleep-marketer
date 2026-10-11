"""generate 잡: 모은 자료로 콘텐츠를 만들고, 정책으로 자동 게시/검토를 판정한다.

하루 목표(플랫폼 쿼터 중 최댓값)만큼만 만든다. n번째 콘텐츠는 쿼터가 남은 플랫폼에만 배정한다.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

from nsm.domain.clock import new_id
from nsm.domain.models import Content, ContentBrief, GeneratedContent, SourceItem
from nsm.domain.policy import (
    assign_platforms,
    choose_template,
    daily_content_target,
    decide_publish,
    find_banned,
    local_day_bounds,
    should_act,
    under_cost_cap,
)
from nsm.engine.context import JobContext, JobResult

log = logging.getLogger(__name__)

SOURCES_PER_CONTENT = 3
AUTO_PUBLISH_KEY = "auto_publish_enabled"


def _strings(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _strings(v)]
    return []


def all_texts(gen: GeneratedContent) -> list[str]:
    deck = gen.deck.model_dump(exclude={"brand", "persona", "labels"})
    return [
        gen.instagram_caption,
        gen.threads_text,
        gen.blog_title,
        gen.blog_markdown,
        *_strings(deck),
    ]


def _pick_material(
    ctx: JobContext,
) -> tuple[str, list[tuple[SourceItem, float]]] | None:
    """쓸 자료 묶음: 가장 오래 안 쓴 페르소나 주제 → 그 주제의 관련성 높은 자료 3건."""
    groups: dict[str, list[tuple[SourceItem, float]]] = defaultdict(list)
    for item, rel, topic in ctx.repo.unused_relevant_items(ctx.config.settings.relevance_threshold):
        if topic:
            groups[topic].append((item, rel))
    if not groups:
        return None
    last_used = ctx.repo.persona_topic_last_used()
    epoch = datetime.min.replace(tzinfo=ctx.clock.now().tzinfo)
    topic = min(groups, key=lambda t: (last_used.get(t, epoch), -len(groups[t])))
    chosen = sorted(groups[topic], key=lambda x: -x[1])[:SOURCES_PER_CONTENT]
    return topic, chosen


def _add_background(ctx: JobContext, gen: GeneratedContent, result: JobResult) -> GeneratedContent:
    if ctx.image_generator is None:
        return gen
    b = ctx.config.brand
    try:
        bg = ctx.image_generator.generate_background(
            f"{gen.topic}. Color palette based on {b.primary_color} and {b.accent_color}.",
            1080,
            1350,
        )
    except Exception as e:  # noqa: BLE001  배경이 없어도 템플릿 배경으로 진행
        result.errors.append(f"배경 이미지 생성 실패: {e}")
        return gen
    if bg is None:
        return gen
    slides = list(gen.deck.slides)
    slides[0] = slides[0].model_copy(update={"background": str(bg)})
    deck = gen.deck.model_copy(
        update={
            "slides": slides,
            "labels": gen.deck.labels.model_copy(update={"ai_generated_image": True}),
        }
    )
    return gen.model_copy(update={"deck": deck})


def run_generate(ctx: JobContext) -> JobResult:
    result = JobResult("generate")
    now = ctx.clock.now()
    ok, why = should_act(now, ctx.config.persona, ctx.tz)
    if not ok:
        result.skipped = why
        return result
    s = ctx.config.settings
    if ctx.text_generator is None:
        result.skipped = "Claude API 키가 없어 생성할 수 없음 (`nsm init`)"
        return result

    start, end = local_day_bounds(now, ctx.tz)
    target = daily_content_target(s.enabled_platforms, s.quotas)
    created: list[str] = []
    while True:
        made_today = ctx.repo.count_contents_between(start, end)
        if made_today >= target:
            result.skipped = "" if created else "오늘 생성 목표 달성"
            break
        spent = ctx.repo.cost_between(start, end)
        if not under_cost_cap(spent, s.daily_cost_cap_usd):
            msg = f"오늘 비용 ${spent:.2f}가 상한 ${s.daily_cost_cap_usd:.2f}에 도달해 생성을 멈춥니다"
            ctx.notifier.notify("비용 상한 도달", msg, "warn")
            result.skipped = "" if created else "비용 상한"
            result.counts["cost_cap_hit"] = True
            break
        material = _pick_material(ctx)
        if material is None:
            result.skipped = "" if created else "쓸 자료 없음(관련 자료 수집 필요)"
            break
        cid = _make_one(ctx, material, made_today, now, result)
        if cid is None:
            break
        created.append(cid)

    result.counts["created"] = len(created)
    pending = [
        c for c in created if (x := ctx.repo.get_content(c)) and x.status == "pending_review"
    ]
    result.counts["pending_review"] = len(pending)
    result.counts["approved"] = len(created) - len(pending)
    if pending:
        ctx.notifier.notify(
            "검토 대기 콘텐츠", f"새 콘텐츠 {len(pending)}건이 검토를 기다립니다", "info"
        )
    result.ok = not (result.errors and not created)
    return result


def _make_one(
    ctx: JobContext,
    material: tuple[str, list[tuple[SourceItem, float]]],
    index_today: int,
    now: datetime,
    result: JobResult,
) -> str | None:
    s = ctx.config.settings
    persona, brand = ctx.config.persona, ctx.config.brand
    persona_topic, chosen = material
    items = [i for i, _ in chosen]
    template = choose_template(ctx.repo.recent_templates(len(("info", "list", "compare"))))
    brief = ContentBrief(
        topic=f"{persona_topic}: {items[0].title}",
        template=template,
        sources=items,
        is_ad=persona.ad_label,
    )
    try:
        gen = ctx.text_generator.generate_content(brief, persona, brand)  # type: ignore[union-attr]
    except Exception as e:  # noqa: BLE001
        log.warning("콘텐츠 생성 실패: %s", e)
        result.errors.append(f"생성 실패: {e}")
        return None
    gen = _add_background(ctx, gen, result)

    claim_ids = [c.source_item_id for c in gen.claims]
    decision = decide_publish(
        claim_source_ids=claim_ids,
        existing_source_ids=ctx.repo.existing_source_ids(claim_ids),
        relevance=min(r for _, r in chosen),
        threshold=s.relevance_threshold,
        banned_hits=find_banned(all_texts(gen), persona),
        auto_publish_enabled=bool(ctx.repo.get_setting(AUTO_PUBLISH_KEY, False)),
        human_approved=ctx.repo.count_human_approved(),
        protect_count=s.protect_count,
    )
    content = Content(
        id=new_id(),
        # 승인 건은 draft로 넣고 바로 approved로 전이해 '정책이 승인함'을 기록한다
        status="draft" if decision.status == "approved" else "pending_review",
        generated=gen,
        relevance_confidence=min(r for _, r in chosen),
        is_ad=brief.is_ad,
        created_at=now,
        review_reason=decision.reason_text,
    )
    ctx.repo.insert_content(content, persona_topic=persona_topic)
    if decision.status == "approved":
        ctx.repo.transition_content(content.id, "approved", approved_by="policy")
    ctx.repo.mark_items_used([i.id for i in items], content.id)
    platforms = assign_platforms(index_today, s.enabled_platforms, s.quotas)
    ctx.repo.create_publications(content.id, platforms)
    result.counts.setdefault("templates", []).append(template)
    return content.id
