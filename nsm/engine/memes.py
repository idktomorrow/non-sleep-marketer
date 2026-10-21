"""밈 슬롯 채우기(S14·S16 제안, ADR 0056·0060): 정보 원고와 별도 경로.

흐름: 관심 주제 하나 → 밈 사례 탐색(공개 검색, 근거 기록) → 상황·감정·반전 구조를 참고한 아이디어 →
의미상 같은 농담 제외 → 2컷 대본 → 밈 덱(그림은 캐릭터·참조를 고른 뒤 제작이 채운다).

- 주인공은 **사용자가 고른 캐릭터**(작업 공간 기본 주인공 프로필)만. 없으면 대본은 만들되 주인공 후보를 제안만 하고
  그림은 선택·참조 뒤에. 페르소나 이름·인물상이나 픽스처 캐릭터를 주인공으로 쓰지 않는다(2026-10-21).
- 뉴스 훅이 있으면 그 자료로 claims·출처를 허용하고, 없으면 그 분야의 공감 상황극(출처 없음).
- 사례가 없으면 창작(D). 탐색 출처가 막혀도 계속 간다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from nsm.adapters.generator.prompts import meme_content
from nsm.domain.clock import new_id
from nsm.domain.meme_discovery import CATEGORY_LABELS, MemeCandidate, candidates
from nsm.domain.meme_policy import (
    fact_signals,
    info_overlap,
    meme_fingerprint,
    meme_tokens,
    pick_idea,
)
from nsm.domain.models import Content, SourceItem
from nsm.domain.policy import find_banned
from nsm.domain.production import Cast, CharacterRef
from nsm.domain.quality import rule_violations
from nsm.engine.context import JobContext
from nsm.ports import AdapterError, MemeExample, MemeIdea, MemeRequest, ScenarioWriter
from nsm.store.picks import Pick, PicksStore
from nsm.store.slots import Slot, SlotStore

log = logging.getLogger(__name__)

RECENT_MEME_DAYS = 30
RETRY_IN = timedelta(minutes=15)
MAX_EXAMPLES = 5
CAUTION = (
    "실존 인물(아이돌·배우 등)이 하지 않은 행동·발언을 사실처럼 쓰지 않고 실존 인물을 그리지 않습니다. "
    "실존 인물이 나오는 주제는 팬의 반응이나 명시된 창작 상황극으로 씁니다"
)


@dataclass
class MemeOutcome:
    ok: bool
    content_id: str | None = None
    reason: str = ""  # 사람이 읽는 실패·부족 이유
    retry_later: bool = False  # 장애: 슬롯을 planned로 돌리고 나중에
    needs_character: bool = False  # 주인공·참조가 아직 없음: 그림 작업을 넣지 않는다(S16 리뷰)
    empty: bool = False  # 후보 없음(아이디어가 모두 중복): 재시도하지 않고 빈 슬롯으로


@dataclass
class MemeContext:
    """한 밈 슬롯에 넘기는 재료: 주제, 그 주제의 최근 소식(있으면), 탐색 결과."""

    topic: str
    hook_title: str = ""
    hook_sources: tuple[SourceItem, ...] = ()
    found: list[MemeCandidate] = field(default_factory=list)
    access: dict[str, str] = field(default_factory=dict)


def meme_request(
    ctx: JobContext,
    plans: list[dict],
    topic: str,
    hero: CharacterRef | None,
    mc: MemeContext | None = None,
) -> MemeRequest:
    persona = ctx.config.persona
    recent = tuple(
        f"{p.get('situation', '')} → {p.get('punchline', '')}" for p in plans if p.get("situation")
    )
    tone = ctx.config.workspace.meme_tone or persona.tone
    examples = tuple(
        MemeExample(
            title=c.key,
            snippet=c.sightings[0].snippet,
            url=c.sightings[0].url,
            platform=c.sightings[0].platform,
            category=c.category,
        )
        for c in (mc.found if mc else [])[:MAX_EXAMPLES]
    )
    return MemeRequest(
        topics=(topic,),
        character_name=hero.name if hero else "",
        character_description=hero.appearance if hero else "",
        tone=tone,
        recent=recent[:20],
        hook=mc.hook_title if mc else "",
        hook_sources=mc.hook_sources if mc else (),
        count=3,
        examples=examples,
        caution=CAUTION,
    )


def _recent_fingerprints(plans: list[dict]) -> list[tuple[str, str]]:
    return [
        (str(p.get("fingerprint", "")), str(p.get("contrast", "")))
        for p in plans
        if p.get("fingerprint")
    ]


def _category(idea: MemeIdea, mc: MemeContext | None) -> tuple[str, list[dict]]:
    """참고한 사례 중 가장 강한 근거의 분류(A<B<C), 참고 없으면 D 창작."""
    if not mc or not idea.based_on:
        return "D", []
    used = [mc.found[i] for i in idea.based_on if 0 <= i < len(mc.found[:MAX_EXAMPLES])]
    if not used:
        return "D", []
    best = min(used, key=lambda c: "ABC".index(c.category))
    return best.category, [c.to_dict() for c in used]


def run_meme_slot(
    ctx: JobContext,
    slot: Slot,
    writer: ScenarioWriter,
    store: PicksStore,
    slots: SlotStore,
    info_titles: list[str] | None = None,
    mc: MemeContext | None = None,
) -> MemeOutcome:
    """claim한 슬롯 하나를 밈 원고로 채운다. 실패는 슬롯 상태로 남긴다(호출자가 fail/set_stage)."""
    from nsm.engine.production import default_cast

    topic = mc.topic if mc else (ctx.config.persona.topics or [""])[0]
    plans = slots.recent_meme_plans(ctx.clock.now() - timedelta(days=RECENT_MEME_DAYS))
    cid = new_id()
    # 사용자가 고른 기본 주인공·보조만(없으면 None). 참조 복사는 원고를 저장할 때만(실패 시 고아 폴더 방지, S16 리뷰)
    cast = default_cast(ctx, cid, copy=False)
    req = meme_request(ctx, plans, topic, cast.hero, mc)
    try:
        ideas = writer.propose_meme_ideas(req)
    except AdapterError as e:
        return MemeOutcome(False, reason=f"밈 아이디어 실패: {e}")
    except Exception as e:  # noqa: BLE001  장애: 나중에
        return MemeOutcome(False, reason=f"AI API 장애: {e}", retry_later=True)
    # 모델이 "최근 밈과 의미상 같다"고 한 것은 빼고, 토큰 겹침은 보조 신호로 한 번 더
    # 번호가 실제 최근 목록 안일 때만 중복으로 본다(최근 밈이 없는데 0을 내는 경우 등, S16 리뷰)
    fresh = [i for i in ideas if not (0 <= i.same_as_recent < len(req.recent))]
    idea = pick_idea(fresh, _recent_fingerprints(plans))
    if idea is None:
        return MemeOutcome(False, reason="밈 아이디어가 모두 최근 것과 같은 농담", empty=True)
    try:
        script = writer.write_meme_script(req, idea)
    except AdapterError as e:
        return MemeOutcome(False, reason=f"밈 대본 실패: {e}")
    except Exception as e:  # noqa: BLE001
        return MemeOutcome(False, reason=f"AI API 장애: {e}", retry_later=True)

    persona = ctx.config.persona.model_copy(update={"tone": req.tone})  # 호출별 말투
    category, basis = _category(idea, mc)
    gen = meme_content(
        script, ctx.config.brand, persona, f"밈: {idea.situation}", list(req.hook_sources)
    )
    reasons = [f"오늘의 추천(밈 · {CATEGORY_LABELS[category]} · 주제 {topic})"]
    reasons += rule_violations(gen, persona)
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

    # 출연진: 고른 주인공·보조만 저장한다. 고르지 않았으면 대본의 제안(suggested_character)은 계획에만 남기고
    # 출연진으로 만들지 않는다(사용자가 고르지 않은 주인공 금지, S16 리뷰)
    final_cast = default_cast(ctx, cid) if (cast.hero or cast.sidekick) else Cast()
    content = Content(
        id=cid,
        status="pending_review",
        generated=gen,
        relevance_confidence=1.0,
        is_ad=persona.ad_label,
        created_at=ctx.clock.now(),
        review_reason="; ".join(reasons),
    )
    pick = Pick(
        content_id=cid,
        run_date=slot.run_date,
        rank=slot.slot_no,
        reason=f"밈({CATEGORY_LABELS[category]}) · 주제 '{topic}' · {idea.situation} · 반전: {idea.punchline}",
        freshness="unknown",
        title_fingerprint=meme_fingerprint(idea),
        kind="meme",
        slot_key=slot.key,
    )
    plan = _plan_json(idea, script, topic, category, basis, mc, final_cast)
    store.save(
        content,
        persona_topic=topic,
        item_ids=[s.id for s in req.hook_sources],
        pick=pick,
        slot_stage="images_pending",  # 그림 전엔 "검토 가능"이 아니다
        slot_plan=plan,
    )
    from nsm.store.production import ProductionStore

    ProductionStore(ctx.repo.db, ctx.clock).set_cast(
        cid, final_cast.to_json(), ctx.config.settings.preset
    )
    hero = final_cast.hero
    if hero is None or not hero.references:
        sl = slots.get(slot.run_date, slot.requested_kind, slot.slot_no)
        if sl is not None:
            slots.set_stage(
                sl,
                "images_pending",
                error="대본 완성. 그림 전에 주인공 캐릭터와 참조 이미지를 고르세요"
                f"(제안: {script.suggested_character or '-'})",
            )
        return MemeOutcome(True, content_id=cid, needs_character=True)
    return MemeOutcome(True, content_id=cid)


def _plan_json(idea: MemeIdea, script, topic, category, basis, mc, cast) -> str:
    return json.dumps(
        {
            "topic": topic,
            "category": category,
            "category_label": CATEGORY_LABELS[category],
            "situation": idea.situation,
            "contrast": " ".join(sorted(meme_tokens(idea.contrast))),
            "punchline": idea.punchline,
            "why": idea.why,
            "fingerprint": meme_fingerprint(idea),
            "basis": basis,  # 참고한 사례(URL·플랫폼·게시·관측 시각·반응 수치=null 미확인·작성자 수)
            "access": mc.access if mc else {},
            "hook": {"title": mc.hook_title, "urls": [s.url for s in mc.hook_sources]}
            if mc and mc.hook_title
            else None,
            "character": {
                "name": cast.hero.name if cast.hero else "",
                "kind": cast.hero.kind if cast.hero else "",
                "references": len(cast.hero.references) if cast.hero else 0,
                "chosen": cast.hero is not None,
                "suggested": script.suggested_character,
            },
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


def discover_for(ctx: JobContext, topic: str) -> MemeContext:
    """밈 탐색(있으면). 실패해도 빈 결과와 상태만 돌려준다."""
    mc = MemeContext(topic=topic)
    disc = getattr(ctx, "meme_discovery", None)
    if disc is None:
        mc.access = {
            "밈 탐색": "수집기 없음(웹 수집이 꺼져 있거나 브라우저 드라이버 없음) — 창작 밈으로"
        }
        return mc
    try:
        res = disc.discover(topic)
        mc.access = res.access
        mc.found = candidates(res.sightings, ctx.clock.now())
    except Exception as e:  # noqa: BLE001
        mc.access = {"밈 탐색": f"실패: {type(e).__name__}: {str(e)[:80]}"}
    return mc
