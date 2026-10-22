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
from pathlib import Path

from nsm.adapters.generator.prompts import meme_content, meme_format_content
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
from nsm.ports import (
    AdapterError,
    MemeExample,
    MemeFormatPlanner,
    MemeFormatRequest,
    MemeIdea,
    MemeRequest,
    ScenarioWriter,
)
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
    """claim한 슬롯 하나를 밈 원고로 채운다. 실패는 슬롯 상태로 남긴다(호출자가 fail/set_stage).

    S17: 밈 형식 탐색이 있으면 유명·유행 밈을 골라 구도·레이아웃까지 재해석한 한 장(기본). 형식 후보를 하나도
    못 찾았을 때만 예전 2컷 대본 경로로 간다."""
    if getattr(ctx, "meme_formats", None) is not None and isinstance(writer, MemeFormatPlanner):
        out = run_format_meme(ctx, slot, writer, store, slots, mc, info_titles)
        if out is not None:
            return out
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


# ---------------------------------------------------------------- 밈 형식 재해석(S17 제안, ADR 0061)

MAX_FORMATS = 10


def discover_formats(ctx: JobContext):
    """밈 형식 후보(같은 날은 캐시). 실패해도 빈 결과와 접근 상태를 돌려준다."""
    from nsm.adapters.sources.meme_formats import FormatDiscovery

    src = getattr(ctx, "meme_formats", None)
    if src is None:
        return FormatDiscovery(access={"밈 형식 탐색": "꺼짐"})
    try:
        return src.discover()
    except Exception as e:  # noqa: BLE001
        return FormatDiscovery(access={"밈 형식 탐색": f"실패: {type(e).__name__}: {str(e)[:80]}"})


ERA_LABELS = {
    "recent": "최근 확산 기록 있는 밈",
    "listed": "인기 목록 밈(최근 기록 미확인)",
    "classic": "오래됐지만 유명한 형식",
    "local": "국내 해설 사이트 유행 표시",
    "fan": "팬 커뮤니티 사례(근거 부족)",
    "original": "창작",
}


def with_fan_and_original(candidates, mc) -> list:
    """후보 = 밈 형식(Know Your Meme·캐릿) + 이 주제 팬 커뮤니티 사례(S16 탐색) + 창작(S18).
    팬 사례는 반응 수치를 못 가져와 '근거 부족'으로, 창작은 근거 없음으로 둔다."""
    from nsm.ports import MemeFormatCandidate

    out = list(candidates)
    for k, c in enumerate((mc.found if mc else [])[:4]):
        sg = c.sightings[0]
        out.append(
            MemeFormatCandidate(
                key=f"fan-{k}",
                title=c.key[:60],
                url=sg.url,
                era="fan",
                evidence=(
                    f"{sg.platform} 게시 {(sg.posted_at.date().isoformat() if sg.posted_at else '날짜 미상')}, "
                    f"서로 다른 작성자 {c.authors}명, 반응 수치 미확인, 검색 결과 요약만 봄"
                ),
                about=sg.snippet[:300],
                site=sg.platform,
                checked_at=sg.observed_at.isoformat(),
                latest_evidence=sg.posted_at.date().isoformat() if sg.posted_at else "",
            )
        )
    out.append(
        MemeFormatCandidate(
            key="original",
            title="새로 만드는 창작 밈",
            url="",
            era="original",
            evidence="",
            about="참고 사례 없이 이 주제의 팬이 공감할 소재와 웃음 방식을 새로 고른다",
            site="창작",
        )
    )
    return out


def format_request(ctx, topic, hero, candidates, plans, mc, force_key="") -> MemeFormatRequest:
    tone = ctx.config.workspace.meme_tone or ctx.config.persona.tone
    return MemeFormatRequest(
        topic=topic,
        character_name=hero.name if hero else "",
        character_description=hero.appearance if hero else "",
        tone=tone,
        candidates=tuple(candidates[:MAX_FORMATS]),
        recent_formats=tuple(
            str(p.get("format", {}).get("key", "")) for p in plans if p.get("format")
        ),
        recent_jokes=tuple(
            f"{p.get('situation', '')} → {p.get('punchline', '')}"
            for p in plans
            if p.get("situation")
        )[:15],
        hook=mc.hook_title if mc else "",
        hook_sources=mc.hook_sources if mc else (),
        caution=CAUTION,
        force_key=force_key,
        recent_mechanisms=tuple(str(p["mechanism"]) for p in plans if p.get("mechanism"))[:10],
    )


def _credits(hero, cand) -> tuple[list[str], list[str]]:
    """(장 안 작은 표기, 캡션 끝 출처 줄). 캐릭터 출처·참고 밈·(사실 근거는 claims)를 구분한다."""
    small, lines = [], []
    if hero is not None and hero.sources:
        small.append(f"캐릭터 참조 {_host(hero.sources[0])}")
        lines.append("캐릭터 참조 출처: " + " · ".join(hero.sources[:2]))
    if cand.era == "original":
        return small, lines  # 참고한 밈 없음(창작)
    label = "참고 사례" if cand.era == "fan" else "참고 밈"
    small.append(f"{label} {cand.title[:28]}({cand.site})")
    lines.append(f"{label}: {cand.title} — {cand.url}")
    return small, lines


def _host(src: str) -> str:
    from urllib.parse import urlsplit

    h = urlsplit(src).netloc
    return (h[4:] if h.startswith("www.") else h) or src[:30]


def format_reasons(ctx, plan, cand, topic, gen, persona, info_titles=None) -> list[str]:
    """검토 사유: 2컷 경로와 같은 검사(규칙·금칙어·사실 신호·정보 겹침)를 한 장 밈에도(S17 리뷰)."""
    from nsm.domain.meme_policy import fact_signals_in, text_overlap

    texts = [plan.caption, plan.threads_text, *(o.text for o in plan.overlays)]
    reasons = [f"오늘의 추천(밈 · {cand.title} 재해석 · 주제 {topic})"]
    reasons += rule_violations(gen, persona)
    reasons += fact_signals_in(texts, bool(plan.claims))
    banned = find_banned(texts, persona)
    if banned:
        reasons.append(f"금칙어: {', '.join(banned)}")
    overlap = text_overlap(
        f"{plan.reinterpretation} {' '.join(o.text for o in plan.overlays)}",
        info_titles or [],
        ctx.config.workspace.aliases,
    )
    if overlap >= 0.5:
        reasons.append(f"정보 소재와 겹침 {int(overlap * 100)}%(검토 참고)")
    return reasons


def pick_reason(cand, topic, plan) -> str:
    return f"밈 · '{cand.title}' 재해석 · 주제 '{topic}' · {plan.why[:120]}"


def build_format_content(ctx, plan, cand, topic, hero, persona, hook_sources):
    small, lines = _credits(hero, cand)
    return meme_format_content(
        plan, ctx.config.brand, persona, f"밈: {cand.title}", small, lines, list(hook_sources)
    )


def format_plan_json(plan, cand, candidates, topic, access, cast, mc) -> dict:
    by_key = {c.key: c for c in candidates}
    return {
        "mode": "format",
        "topic": topic,
        # 형식 재해석은 사례 탐색 분류(A~D)와 다른 축이라 category를 두지 않고 시기만 남긴다(S17 리뷰)
        "era": cand.era,
        "category_label": ERA_LABELS.get(cand.era, "밈"),
        "source_kind": plan.source_kind,
        "mechanism": plan.mechanism,
        "form": plan.form,
        "pages": len(plan.pages) or 1,
        # 팬 사례 후보는 다시 만들 때 같은 글을 가리키도록 계획에 저장한다(재검색하면 순서가 바뀐다, S18 리뷰)
        "fan_candidates": [
            {
                k: getattr(c, k)
                for k in (
                    "key",
                    "title",
                    "url",
                    "era",
                    "evidence",
                    "about",
                    "site",
                    "checked_at",
                    "latest_evidence",
                )
            }
            for c in candidates
            if c.era == "fan"
        ],
        "format": {
            "key": cand.key,
            "title": cand.title,
            "url": cand.url,
            "site": cand.site,
            "era": cand.era,
            "evidence": cand.evidence[:700],
            "latest_evidence": cand.latest_evidence,
            "image_url": cand.image_url,
            "checked_at": cand.checked_at,
        },
        "why": plan.why,
        "funny_point": plan.funny_point,
        "structure": plan.structure,
        "reinterpretation": plan.reinterpretation,
        "situation": plan.reinterpretation,
        "punchline": " / ".join(o.text for o in plan.overlays)[:120],
        "fingerprint": f"format:{cand.key}:{plan.reinterpretation[:60]}",
        "ranking": [
            {
                "key": r.key,
                "title": by_key[r.key].title if r.key in by_key else r.key,
                "url": by_key[r.key].url if r.key in by_key else "",
                "era": by_key[r.key].era if r.key in by_key else "",
                "score": r.score,
                "reason": r.reason,
            }
            for r in sorted(plan.ranking, key=lambda r: -r.score)
        ],
        "access": access,
        "hook": {"title": mc.hook_title, "urls": [s.url for s in mc.hook_sources]}
        if mc and mc.hook_title
        else None,
        "character": {
            "name": cast.hero.name if cast.hero else "",
            "kind": cast.hero.kind if cast.hero else "",
            "references": len(cast.hero.references) if cast.hero else 0,
            "chosen": cast.hero is not None,
            "suggested": plan.suggested_character,
        },
        "sources": {
            "character": list(cast.hero.sources) if cast.hero else [],
            "meme_ref": ([cand.url] if cand.url else [])
            + ([cand.image_url] if cand.image_url else []),
            "facts": [s.url for s in (mc.hook_sources if mc else ()) if s.url],
        },
        "overlays": [o.__dict__ for o in plan.overlays],
        "scene": plan.scene,
    }


def copy_meme_ref(ctx, cid: str, cand) -> None:
    import shutil

    from nsm.engine.production import content_dir

    d = content_dir(ctx.media_dir, cid) / "meme_ref"
    if d.is_dir():
        shutil.rmtree(d)
    if cand.image_path and Path(cand.image_path).is_file():
        d.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cand.image_path, d / f"{cand.key}{Path(cand.image_path).suffix}")


def run_format_meme(
    ctx: JobContext,
    slot: Slot,
    writer,
    store: PicksStore,
    slots: SlotStore,
    mc: MemeContext | None,
    info_titles: list[str] | None = None,
) -> MemeOutcome | None:
    """밈 형식을 골라 한 장을 기획·저장한다. 형식 후보가 없으면 None(호출자가 2컷 경로로)."""
    from nsm.engine.production import default_cast

    topic = mc.topic if mc else (ctx.config.persona.topics or [""])[0]
    fd = discover_formats(ctx)
    # 후보 = 밈 형식 + 팬 커뮤니티 사례 + 창작(S18). 탐색이 다 막혀도 창작으로 기획은 계속한다
    cands = with_fan_and_original(fd.candidates, mc)
    plans = slots.recent_meme_plans(ctx.clock.now() - timedelta(days=RECENT_MEME_DAYS))
    cid = new_id()
    cast = default_cast(ctx, cid, copy=False)
    req = format_request(ctx, topic, cast.hero, cands, plans, mc)
    try:
        plan = writer.plan_meme_format(req)
    except AdapterError as e:
        return MemeOutcome(False, reason=f"밈 형식 기획 실패: {e}")
    except Exception as e:  # noqa: BLE001  장애: 나중에
        return MemeOutcome(False, reason=f"AI API 장애: {e}", retry_later=True)
    cand = next(c for c in cands if c.key == plan.key)
    persona = ctx.config.persona.model_copy(update={"tone": req.tone})
    final_cast = default_cast(ctx, cid) if (cast.hero or cast.sidekick) else Cast()
    try:
        gen = build_format_content(
            ctx, plan, cand, topic, final_cast.hero, persona, req.hook_sources
        )
    except Exception as e:  # noqa: BLE001  계약에 안 맞는 계획(좌표 등)
        return MemeOutcome(False, reason=f"밈 형식 계획이 카드 계약에 맞지 않음: {str(e)[:160]}")
    access = {**(mc.access if mc else {}), **fd.access}
    reasons = format_reasons(ctx, plan, cand, topic, gen, persona, info_titles)
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
        reason=pick_reason(cand, topic, plan),
        freshness="unknown",
        title_fingerprint=f"format:{cand.key}:{topic}",
        kind="meme",
        slot_key=slot.key,
    )
    plan_json = json.dumps(
        format_plan_json(plan, cand, cands, topic, access, final_cast, mc),
        ensure_ascii=False,
    )
    store.save(
        content,
        persona_topic=topic,
        item_ids=[s.id for s in req.hook_sources],
        pick=pick,
        slot_stage="images_pending",
        slot_plan=plan_json,
    )
    copy_meme_ref(ctx, cid, cand)
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
                error="기획 완성. 그림 전에 주인공 캐릭터와 참조 이미지를 고르세요"
                f"(제안: {plan.suggested_character or '-'})",
            )
        return MemeOutcome(True, content_id=cid, needs_character=True)
    return MemeOutcome(True, content_id=cid)


def remake_with_format(ctx: JobContext, cid: str, key: str) -> str:
    """'다른 밈으로 다시 만들기'(S17): 같은 원고·같은 슬롯을 다른 형식으로 다시 기획한다. 그림은 새로 만든다."""
    from nsm.engine.production import ensure_cast
    from nsm.store.production import ProductionStore

    store = PicksStore(ctx.repo.db, ctx.clock)
    slots = SlotStore(ctx.repo.db, ctx.clock)
    c = ctx.repo.get_content(cid)
    sl = slots.by_content(cid)
    writer = ctx.scenario_writer
    if c is None or sl is None or writer is None or not isinstance(writer, MemeFormatPlanner):
        raise AdapterError("다시 만들 수 있는 밈 원고가 아닙니다")
    fd = discover_formats(ctx)
    old = sl.plan or {}
    topic = old.get("topic") or (ctx.config.persona.topics or [""])[0]
    found: list = []  # 팬 사례는 다시 검색하지 않고 계획에 저장된 그대로 쓴다(S18 리뷰)
    prod = ProductionStore(ctx.repo.db, ctx.clock)
    cast = ensure_cast(ctx, prod, cid)
    plans = slots.recent_meme_plans(ctx.clock.now() - timedelta(days=RECENT_MEME_DAYS))
    hook_sources = tuple(ctx.repo.sources_for_content(cid)) if old.get("hook") else ()
    mc = MemeContext(
        topic=topic,
        hook_title=(old.get("hook") or {}).get("title", ""),
        hook_sources=hook_sources,
        found=found,
    )
    from nsm.ports import MemeFormatCandidate

    stored_fans = [MemeFormatCandidate(**f) for f in old.get("fan_candidates") or []]
    cands = [
        *fd.candidates,
        *stored_fans,
        *[c for c in with_fan_and_original([], mc) if c.era == "original"],
    ]
    if key not in {x.key for x in cands}:
        raise AdapterError("그 밈 후보를 오늘 탐색 결과에서 찾지 못했습니다")
    req = format_request(ctx, topic, cast.hero, cands, plans, mc, force_key=key)
    plan = writer.plan_meme_format(req)
    cand = next(x for x in cands if x.key == plan.key)
    persona = ctx.config.persona.model_copy(update={"tone": req.tone})
    gen = build_format_content(ctx, plan, cand, topic, cast.hero, persona, hook_sources)
    ctx.repo.update_generated(cid, gen)
    ctx.repo.set_review_reason(cid, "; ".join(format_reasons(ctx, plan, cand, topic, gen, persona)))
    store.set_reason(cid, pick_reason(cand, topic, plan), f"format:{cand.key}:{topic}")
    copy_meme_ref(ctx, cid, cand)
    # 예전 형식의 그림·장면 지시가 새 형식에 남지 않게(S17 리뷰)
    for s_ in ("s0.image",):
        prod.archive_active(cid, s_)
        prod.clear_scene(cid, s_)
    plan_json = format_plan_json(
        plan, cand, cands, topic, {**old.get("access", {}), **fd.access}, cast, mc
    )
    slots.set_stage(sl, "images_pending", error=None, plan=plan_json)
    store.set_render_checked(cid, None)
    if ctx.config.workspace.production_mode == "full" and cast.hero and cast.hero.references:
        from nsm.engine.production import enqueue_production

        enqueue_production(ctx, cid, force=True)
    return cand.title
