"""밈 슬롯 채우기(S14 제안, ADR 0056·0057): 정보 원고와 별도 경로.

관심 분야·캐릭터·최근 밈 목록으로 아이디어 N개 → 중복 아닌 것 하나 → 2컷 대본 → 밈 덱(그림은 S15 제작이 채운다).
뉴스가 없어도 창작 가능하다. 창작 대사에 가짜 출처를 붙이지 않고, 사실처럼 보이는 표현은 검토 신호만 남긴다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from nsm.adapters.generator.prompts import meme_content
from nsm.domain.clock import new_id
from nsm.domain.meme_policy import fact_signals, info_overlap, meme_fingerprint, pick_idea
from nsm.domain.models import Content, SourceItem
from nsm.domain.policy import find_banned
from nsm.domain.quality import rule_violations
from nsm.engine.context import JobContext
from nsm.ports import AdapterError, MemeIdea, MemeRequest, ScenarioWriter
from nsm.store.picks import Pick, PicksStore
from nsm.store.slots import Slot, SlotStore

log = logging.getLogger(__name__)

RECENT_MEME_DAYS = 30
RETRY_IN = timedelta(minutes=15)


@dataclass
class MemeOutcome:
    ok: bool
    content_id: str | None = None
    reason: str = ""  # 사람이 읽는 실패·부족 이유
    retry_later: bool = False  # 장애: 슬롯을 planned로 돌리고 나중에


def meme_request(
    ctx: JobContext,
    slots: SlotStore,
    hook: str = "",
    hook_sources: tuple[SourceItem, ...] = (),
) -> MemeRequest:
    persona = ctx.config.persona
    since = ctx.clock.now() - timedelta(days=RECENT_MEME_DAYS)
    recent = tuple(
        f"{p.get('situation', '')} → {p.get('punchline', '')}"
        for p in slots.recent_meme_plans(since)
        if p.get("situation")
    )
    tone = ctx.config.workspace.meme_tone or persona.tone
    return MemeRequest(
        topics=tuple(persona.topics),
        character_name=persona.name,
        character_description=persona.character,
        tone=tone,
        recent=recent[:20],
        hook=hook,
        hook_sources=hook_sources,
        count=3,
    )


def _recent_fingerprints(ctx: JobContext, slots: SlotStore) -> list[tuple[str, str]]:
    since = ctx.clock.now() - timedelta(days=RECENT_MEME_DAYS)
    return [
        (str(p.get("fingerprint", "")), str(p.get("contrast", "")))
        for p in slots.recent_meme_plans(since)
        if p.get("fingerprint")
    ]


def run_meme_slot(
    ctx: JobContext,
    slot: Slot,
    writer: ScenarioWriter,
    store: PicksStore,
    slots: SlotStore,
    info_titles: list[str] | None = None,
) -> MemeOutcome:
    """claim한 슬롯 하나를 밈 원고로 채운다. 실패는 슬롯 상태로 남긴다(호출자가 fail/set_stage)."""
    req = meme_request(ctx, slots)
    try:
        ideas = writer.propose_meme_ideas(req)
    except AdapterError as e:
        return MemeOutcome(False, reason=f"밈 아이디어 실패: {e}")
    except Exception as e:  # noqa: BLE001  장애: 나중에
        return MemeOutcome(False, reason=f"AI API 장애: {e}", retry_later=True)
    idea = pick_idea(ideas, _recent_fingerprints(ctx, slots))
    if idea is None:
        return MemeOutcome(False, reason="밈 아이디어가 모두 최근 것과 겹침")
    try:
        script = writer.write_meme_script(req, idea)
    except AdapterError as e:
        return MemeOutcome(False, reason=f"밈 대본 실패: {e}")
    except Exception as e:  # noqa: BLE001
        return MemeOutcome(False, reason=f"AI API 장애: {e}", retry_later=True)

    persona = ctx.config.persona.model_copy(
        update={"tone": req.tone}
    )  # 호출별 말투(전역 덮어쓰기 없음)
    topic = f"밈: {idea.situation}"
    gen = meme_content(script, ctx.config.brand, persona, topic, list(req.hook_sources))
    reasons = ["오늘의 추천(밈)"]
    violations = rule_violations(gen, persona)
    reasons += violations
    reasons += fact_signals(script)
    banned = find_banned(
        [
            script.caption,
            script.threads_text,
            *(p.label for p in script.panels),
            *(p.bubble for p in script.panels),
        ],
        persona,
    )
    if banned:
        reasons.append(f"금칙어: {', '.join(banned)}")
    overlap = info_overlap(idea, info_titles or [], ctx.config.workspace.aliases)
    if overlap >= 0.5:
        reasons.append(
            f"정보 소재와 겹침 {int(overlap * 100)}%(검토 참고)"
        )  # 자동 제외는 하지 않는다

    content = Content(
        id=new_id(),
        status="pending_review",
        generated=gen,
        relevance_confidence=1.0,
        is_ad=persona.ad_label,
        created_at=ctx.clock.now(),
        review_reason="; ".join(reasons),
    )
    pick = Pick(
        content_id=content.id,
        run_date=slot.run_date,
        rank=slot.slot_no,
        reason=f"밈 · {idea.situation} · 반전: {idea.punchline}",
        freshness="unknown",
        title_fingerprint=meme_fingerprint(idea),
        kind="meme",
        slot_key=slot.key,
    )
    plan = _plan_json(idea, script)
    store.save(
        content,
        persona_topic=None,
        item_ids=[s.id for s in req.hook_sources],
        pick=pick,
        slot_stage="images_pending",  # 그림은 S15 제작이 채운다. 그 전엔 "검토 가능"이 아니다
        slot_plan=plan,
    )
    return MemeOutcome(True, content_id=content.id)


def _plan_json(idea: MemeIdea, script) -> str:
    import json

    from nsm.domain.meme_policy import meme_tokens

    return json.dumps(
        {
            "situation": idea.situation,
            "contrast": " ".join(sorted(meme_tokens(idea.contrast))),
            "punchline": idea.punchline,
            "why": idea.why,
            "fingerprint": meme_fingerprint(idea),
            "panels": [
                {
                    "label": p.label,
                    "bubble": p.bubble,
                    "scene": p.scene,
                    "bubble_side": p.bubble_side,
                }
                for p in script.panels
            ],
        },
        ensure_ascii=False,
    )
