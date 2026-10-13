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
    TEMPLATES,
    assign_platforms,
    choose_template,
    daily_content_target,
    decide_publish,
    find_banned,
    local_day_bounds,
    should_act,
    under_cost_cap,
)
from nsm.domain.quality import normalize_bullets, rule_violations
from nsm.engine.context import JobContext, JobResult
from nsm.ports import AdapterError

log = logging.getLogger(__name__)

SOURCES_PER_CONTENT = 3
AUTO_PUBLISH_KEY = "auto_publish_enabled"
GENERATION_FAILED = "generation_failed"  # source_items.used_in_content_id 표시값
_RETRY_LATER = "__retry_later__"
_SKIP_MATERIAL = "__skip_material__"


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
    # 이미지도 돈이 든다: 텍스트 생성 후 상한을 넘었으면 배경 없이 간다
    start, end = local_day_bounds(ctx.clock.now(), ctx.tz)
    if not under_cost_cap(
        ctx.repo.cost_between(start, end), ctx.config.settings.daily_cost_cap_usd
    ):
        result.errors.append("비용 상한에 도달해 배경 이미지를 건너뜀")
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


def _check_rules(
    ctx: JobContext, brief: ContentBrief, gen: GeneratedContent, result: JobResult
) -> tuple[GeneratedContent, list[str]]:
    """프롬프트에만 있던 규칙(이모지 수, 딱딱한 표현)을 검사하고, 어기면 한 번 다시 만든다."""
    persona = ctx.config.persona
    violations = rule_violations(gen, persona)
    if not violations:
        return gen, []
    log.info("페르소나 규칙 위반, 한 번 다시 생성: %s", violations)
    start, end = local_day_bounds(ctx.clock.now(), ctx.tz)
    if not under_cost_cap(
        ctx.repo.cost_between(start, end), ctx.config.settings.daily_cost_cap_usd
    ):
        return gen, violations  # 비용 상한: 다시 만들지 않고 검토로
    try:
        again = normalize_bullets(
            ctx.text_generator.generate_content(brief, persona, ctx.config.brand)  # type: ignore[union-attr]
        )
    except Exception as e:  # noqa: BLE001  다시 만들기가 실패하면 처음 것을 검토로 보낸다
        result.errors.append(f"규칙 위반 재생성 실패: {e}")
        return gen, violations
    result.counts["regenerated"] = result.counts.get("regenerated", 0) + 1
    v2 = rule_violations(again, persona)
    return (again, v2) if len(v2) <= len(violations) else (gen, violations)


def run_generate(ctx: JobContext, manual: bool = False) -> JobResult:
    """자동 실행은 하루 목표(플랫폼 쿼터 중 최대)까지 만든다. 사람이 "지금 실행"으로 부르면(manual)
    목표와 상관없이 1건을 더 만든다(비용 상한·쓸 자료는 그대로 지킨다, ADR 0030)."""
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
    if manual:
        target = ctx.repo.count_contents_between(start, end) + 1
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
        cid = _make_one(ctx, material, made_today, now, result, manual=manual)
        if cid == _RETRY_LATER:
            break  # 네트워크·API 장애: 자료는 그대로 두고 다음 실행에 다시
        if cid == _SKIP_MATERIAL:
            continue  # 이 자료 묶음은 버리고 다음 자료로
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
    manual: bool = False,
) -> str:
    s = ctx.config.settings
    persona, brand = ctx.config.persona, ctx.config.brand
    persona_topic, chosen = material
    items = [i for i, _ in chosen]
    template = choose_template(ctx.repo.recent_templates(len(TEMPLATES)))
    brief = ContentBrief(
        topic=f"{persona_topic}: {items[0].title}",
        template=template,
        sources=items,
        is_ad=persona.ad_label,
    )
    try:
        gen = ctx.text_generator.generate_content(brief, persona, brand)  # type: ignore[union-attr]
    except AdapterError as e:
        # 거절·잘림·형식 오류: 이 자료 묶음이 문제일 가능성이 높다. 표시해 두고 다음 자료로.
        log.warning("콘텐츠 생성 실패(자료 건너뜀): %s", e)
        result.errors.append(f"생성 실패: {e}")
        ctx.repo.mark_items_used([i.id for i in items], GENERATION_FAILED)
        return _SKIP_MATERIAL
    except Exception as e:  # noqa: BLE001  네트워크·API 장애
        log.warning("콘텐츠 생성 실패(다음에 재시도): %s", e)
        result.errors.append(f"생성 실패: {e}")
        return _RETRY_LATER
    gen, violations = _check_rules(ctx, brief, normalize_bullets(gen), result)
    gen = _add_background(ctx, gen, result)

    claim_ids = [c.source_item_id for c in gen.claims]
    brief_ids = {i.id for i in items}
    # 출처는 "DB 어딘가에 있음"이 아니라 "이번에 준 자료 중 하나"여야 한다
    verified = ctx.repo.existing_source_ids([i for i in claim_ids if i in brief_ids])
    rel_by_id = {i.id: r for i, r in chosen}
    cited_rel = [rel_by_id[i] for i in claim_ids if i in rel_by_id]
    relevance = min(cited_rel) if cited_rel else 0.0
    decision = decide_publish(
        claim_source_ids=claim_ids,
        existing_source_ids=verified,
        relevance=relevance,
        threshold=s.relevance_threshold,
        banned_hits=find_banned(all_texts(gen), persona),
        auto_publish_enabled=bool(ctx.repo.get_setting(AUTO_PUBLISH_KEY, False)),
        human_approved=ctx.repo.count_human_approved(),
        protect_count=s.protect_count,
    )
    reason = decision.reason_text
    approved = decision.status == "approved"
    if violations:
        # 다시 만들어도 규칙을 어기면 자동 게시하지 않고 사람에게 넘긴다
        approved = False
        reason = "; ".join(filter(None, [f"페르소나 규칙 위반: {', '.join(violations)}", reason]))
    content = Content(
        id=new_id(),
        status="draft" if approved else "pending_review",
        generated=gen,
        relevance_confidence=relevance,
        is_ad=brief.is_ad,
        created_at=now,
        review_reason=reason,
    )
    ctx.repo.save_generated(
        content,
        persona_topic=persona_topic,
        approved_by_policy=approved,
        item_ids=[i.id for i in items],
        # 하루 쿼터를 넘겨 사람이 더 만든 원고는 모든 플랫폼에 배정한다(쿼터가 남는 날 차례로 올라감)
        platforms=assign_platforms(index_today, s.enabled_platforms, s.quotas)
        or (list(s.enabled_platforms) if manual else []),
    )
    result.counts.setdefault("templates", []).append(template)
    return content.id
