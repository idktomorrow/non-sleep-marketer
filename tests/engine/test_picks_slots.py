"""추천 슬롯 엔진(S14 제안, ADR 0057): 정보 N + 밈 M 계획, 밈 경로 분리, 실패 격리·재시도, 대체 종류 기록, 오늘만 변경."""

from __future__ import annotations

from datetime import date

import pytest

from nsm.engine.picks import PLAN_OVERRIDE_KEY, run_picks, run_slots_retry
from nsm.store.picks import PicksStore
from nsm.store.slots import SlotStore
from tests.engine.test_picks_job import _enable, _fake_photo, _item, ctx  # noqa: F401
from tests.fakes import FakeScenarioWriter, FakeSource


def _workspace(c, **kw):
    c.config = c.config.model_copy(update={"workspace": c.config.workspace.model_copy(update=kw)})


def test_info_two_plus_meme_one(ctx) -> None:  # noqa: F811
    _workspace(ctx, info_count=2, meme_count=1, meme_tone="밈 말투")
    ctx.scenario_writer = FakeScenarioWriter()
    r = run_picks(ctx, fetch_photo=_fake_photo)
    assert (r.counts["made_info"], r.counts["made_meme"]) == (2, 1)
    slots = SlotStore(ctx.repo.db, ctx.clock)
    today = ctx.clock.now().date()
    stages = {(s.requested_kind, s.slot_no): s.stage for s in slots.list_for_date(today)}
    assert stages == {("info", 1): "ready", ("info", 2): "ready", ("meme", 1): "images_pending"}
    store = PicksStore(ctx.repo.db, ctx.clock)
    meme = next(p for p in store.list(run_date=today) if p.kind == "meme")
    c = ctx.repo.get_content(meme.content_id)
    assert c.generated.template == "meme" and c.generated.deck.slides[0].kind == "comic"
    assert c.generated.deck.labels.ai_slides == [] and c.status == "pending_review"
    assert "오늘의 추천(밈" in c.review_reason and meme.slot_key == "2026-10-11:meme:1"
    req = ctx.scenario_writer.requests[0]
    assert (
        req.tone == "밈 말투" and ctx.config.persona.tone != "밈 말투"
    )  # 호출별 말투, 전역은 그대로
    hook_ids = {i.id for i in req.hook_sources}
    assert all(
        cl.source_item_id in hook_ids for cl in c.generated.claims
    )  # 사실은 그 주제 소식(훅) 자료에만 근거
    # 밈 원고는 그림이 끝나기 전엔 게시로 보낼 수 없다
    from nsm.app.review_actions import ActionError, ReviewActions

    actions = ReviewActions(ctx.repo, store, ctx.clock)
    with pytest.raises(ActionError):
        actions.approve(meme.content_id, persona=ctx.config.persona, platforms=["threads"])


def test_second_run_fills_only_remaining_and_lowering_keeps_results(ctx) -> None:  # noqa: F811
    _workspace(ctx, info_count=3, meme_count=0)
    run_picks(ctx, fetch_photo=_fake_photo)
    assert ctx.repo.count_contents() == 3
    _workspace(ctx, info_count=1, meme_count=0)
    r = run_picks(ctx, fetch_photo=_fake_photo)
    assert r.skipped.startswith("오늘 슬롯") and ctx.repo.count_contents() == 3  # 완성된 것은 유지
    slots = SlotStore(ctx.repo.db, ctx.clock)
    assert all(s.stage == "ready" for s in slots.list_for_date(ctx.clock.now().date()))


def test_meme_failure_does_not_block_info_and_is_retried(ctx) -> None:  # noqa: F811
    _workspace(ctx, info_count=1, meme_count=1)
    ctx.scenario_writer = FakeScenarioWriter(fail=True)
    r = run_picks(ctx, fetch_photo=_fake_photo)
    assert r.counts["made_info"] == 1 and r.counts["made_meme"] == 0 and r.counts["failed"] == 1
    slots = SlotStore(ctx.repo.db, ctx.clock)
    meme = slots.get(ctx.clock.now().date(), "meme", 1)
    assert meme.stage == "planned" and meme.attempts == 1 and "밈 아이디어 실패" in meme.last_error
    assert "다시 시도할 슬롯이 없음" in run_slots_retry(ctx).skipped  # 아직 재시도 시각 전
    ctx.clock.advance(minutes=20)
    ctx.scenario_writer = FakeScenarioWriter()
    r2 = run_slots_retry(ctx)
    assert r2.counts["made_meme"] == 1 and ctx.repo.count_contents() == 2
    # 정보 원고의 자료는 밈 실패로 풀리지 않았다
    used = ctx.repo.db.execute(
        "SELECT count(*) FROM source_items WHERE used_in_content_id IS NOT NULL"
    )
    assert used[0][0] >= 1


def test_no_writer_marks_meme_failed_with_reason(ctx) -> None:  # noqa: F811
    _workspace(ctx, info_count=0, meme_count=1)
    ctx.scenario_writer = None
    r = run_picks(ctx, fetch_photo=_fake_photo)
    assert r.counts["made"] == 0
    slot = SlotStore(ctx.repo.db, ctx.clock).get(ctx.clock.now().date(), "meme", 1)
    assert "공급자" in slot.last_error


def test_duplicate_ideas_leave_slot_empty_without_forcing(ctx) -> None:  # noqa: F811
    from nsm.ports import MemeIdea

    _workspace(ctx, info_count=0, meme_count=2)
    one = MemeIdea("극중 월급 vs 현실 연봉", "시무룩 vs 느긋", "'시러~'는 무슨.", "")
    ctx.scenario_writer = FakeScenarioWriter(ideas=[one])
    r = run_picks(ctx, fetch_photo=_fake_photo)
    # 아이디어가 하나뿐이라 두 번째 슬롯은 중복 → 비어 있음(억지로 채우지 않는다)
    assert r.counts["made_meme"] == 1 and r.counts["empty"] == 1
    slots = SlotStore(ctx.repo.db, ctx.clock)
    assert slots.get(ctx.clock.now().date(), "meme", 2).stage == "empty"


def test_substitute_records_requested_and_actual_kind(ctx) -> None:  # noqa: F811
    ctx.sources = [FakeSource("naver", [])]  # 정보 후보 없음
    _workspace(ctx, info_count=1, meme_count=0, allow_substitute=True)
    ctx.scenario_writer = FakeScenarioWriter()
    r = run_picks(ctx, fetch_photo=_fake_photo)
    assert r.counts["made_meme"] == 1 and r.counts["made_info"] == 0
    slot = SlotStore(ctx.repo.db, ctx.clock).get(ctx.clock.now().date(), "info", 1)
    assert (slot.requested_kind, slot.actual_kind) == ("info", "meme")
    _workspace(ctx, allow_substitute=False)
    ctx.clock.advance(days=1)
    r = run_picks(ctx, fetch_photo=_fake_photo)
    assert r.counts["made"] == 0 and r.counts["empty"] == 1  # 기본은 대체하지 않는다


def test_today_override_beats_workspace(ctx) -> None:  # noqa: F811
    _workspace(ctx, info_count=3, meme_count=0)
    today = ctx.clock.now().date()
    ctx.repo.set_setting(PLAN_OVERRIDE_KEY, {"date": today.isoformat(), "info": 1, "meme": 0})
    r = run_picks(ctx, fetch_photo=_fake_photo)
    assert r.counts["made"] == 1 and r.counts["planned_info"] == 1
    ctx.repo.set_setting(
        PLAN_OVERRIDE_KEY, {"date": date(2020, 1, 1).isoformat(), "info": 5, "meme": 0}
    )
    assert run_picks(ctx, fetch_photo=_fake_photo).counts["planned_info"] == 3  # 날짜가 다르면 무시


def test_zero_plan_skips(ctx) -> None:  # noqa: F811
    _workspace(ctx, info_count=0, meme_count=0)
    assert "추천 개수가 0" in run_picks(ctx, fetch_photo=_fake_photo).skipped


def test_unexpected_error_marks_slot_failed_not_stuck(ctx, monkeypatch) -> None:  # noqa: F811
    import nsm.engine.picks as picks_mod

    _workspace(ctx, info_count=0, meme_count=1)
    ctx.scenario_writer = FakeScenarioWriter()

    def boom(*a, **k):
        raise ValueError("덱 검증 실패")

    monkeypatch.setattr(picks_mod, "run_meme_slot", boom)
    r = run_picks(ctx, fetch_photo=_fake_photo)
    slot = SlotStore(ctx.repo.db, ctx.clock).get(ctx.clock.now().date(), "meme", 1)
    assert r.counts["failed"] == 1 and slot.stage == "planned" and "ValueError" in slot.last_error


def test_substitute_outage_is_recorded_not_hidden(ctx) -> None:  # noqa: F811
    ctx.sources = [FakeSource("naver", [])]
    _workspace(ctx, info_count=1, meme_count=0, allow_substitute=True)

    class Down(FakeScenarioWriter):
        def propose_meme_ideas(self, req):
            raise RuntimeError("503")

    ctx.scenario_writer = Down()
    r = run_picks(ctx, fetch_photo=_fake_photo)
    slot = SlotStore(ctx.repo.db, ctx.clock).get(ctx.clock.now().date(), "info", 1)
    assert slot.stage == "planned" and slot.attempts == 1 and "장애" in slot.last_error
    assert "장애" in r.counts["reason"]


def test_cancelled_slot_rejects_late_save(ctx) -> None:  # noqa: F811
    from nsm.store.picks import SlotConflictError

    _workspace(ctx, info_count=1, meme_count=0)
    slots = SlotStore(ctx.repo.db, ctx.clock)
    today = ctx.clock.now().date()
    slots.plan(today, 1, 0, "기본")
    sl = slots.claim(today, "info", "r", ctx.clock.now())
    assert (
        slots.plan(today, 0, 0, "기본")["cancelled"] == 0
    )  # 쓰는 중인 슬롯은 축소로 취소하지 않는다
    ctx.repo.db.execute("UPDATE pick_slots SET stage = 'cancelled'")
    from nsm.domain.models import Content, ContentBrief
    from nsm.store.picks import Pick
    from tests.fakes import FakeTextGenerator, load_source_items

    gen = FakeTextGenerator().generate_content(
        ContentBrief(topic="t", template="info", sources=load_source_items()[:1]),
        ctx.config.persona,
        ctx.config.brand,
    )
    c = Content(
        id="late",
        status="pending_review",
        generated=gen,
        relevance_confidence=1.0,
        created_at=ctx.clock.now(),
    )
    with pytest.raises(SlotConflictError):
        PicksStore(ctx.repo.db, ctx.clock).save(
            c,
            persona_topic=None,
            item_ids=[],
            pick=Pick(content_id="late", run_date=today, rank=1, reason="r", slot_key=sl.key),
        )
    assert ctx.repo.get_content("late") is None and slots.get(today, "info", 1).stage == "cancelled"


def test_meme_topic_rotates_by_recent_history_with_one_meme_per_day(ctx) -> None:  # noqa: F811
    persona = ctx.config.persona.model_copy(update={"topics": ["굿즈", "행사"]})
    ctx.config = ctx.config.model_copy(update={"persona": persona})
    _workspace(ctx, info_count=0, meme_count=1)
    ctx.scenario_writer = FakeScenarioWriter()
    run_picks(ctx, fetch_photo=_fake_photo)
    ctx.clock.advance(days=1)
    from nsm.ports import MemeIdea

    ctx.scenario_writer = FakeScenarioWriter(
        ideas=[MemeIdea("팝업 줄서기", "아침 vs 저녁", "아직도 줄?", "")]
    )
    run_picks(ctx, fetch_photo=_fake_photo)
    store = PicksStore(ctx.repo.db, ctx.clock)
    topics = [ctx.repo.content_topic(p.content_id) for p in store.list() if p.kind == "meme"]
    assert sorted(topics) == ["굿즈", "행사"]  # 이틀 동안 두 주제를 번갈아(첫 주제만 반복하지 않음)


def test_meme_request_uses_topic_material_not_persona(ctx) -> None:  # noqa: F811
    from nsm.adapters.generator.prompts import meme_ideas_user, meme_system

    _workspace(ctx, info_count=0, meme_count=1)
    ctx.scenario_writer = FakeScenarioWriter()
    run_picks(ctx, fetch_photo=_fake_photo)
    req = ctx.scenario_writer.requests[0]
    assert (
        req.topics in (("굿즈",), ("행사",)) and req.character_name == ""
    )  # 캐릭터 미선택: 페르소나 이름 아님
    assert req.hook and req.hook_sources  # 그 주제의 최근 소식이 훅으로
    text = meme_system(req) + meme_ideas_user(req)
    for leak in ("루나", "커피", "바리스타", "원두", "핸드드립"):
        assert leak not in text  # 페르소나·픽스처 어휘가 밈 요청에 섞이지 않는다
    # 캐릭터 미선택이어도 대본은 저장되고, 그림은 참조 선택 전까지 대기
    store = PicksStore(ctx.repo.db, ctx.clock)
    meme = next(p for p in store.list() if p.kind == "meme")
    slot = SlotStore(ctx.repo.db, ctx.clock).by_content(meme.content_id)
    assert slot.stage == "images_pending" and "참조" in slot.last_error
    from nsm.engine.production import enqueue_production

    r = enqueue_production(ctx, meme.content_id)
    problem = r.problems["s0.panel0"]
    assert r.queued == [] and (
        "참조" in problem or "정해지지" in problem
    )  # 임의 캐릭터로 대체하지 않는다


def test_selected_hero_is_used_and_copied(ctx, tmp_path) -> None:  # noqa: F811
    from nsm.engine.production import characters
    from nsm.store.production import ProductionStore

    monkey = characters(ctx.config)
    p = monkey.create("치이카와", "하얀 몸, 분홍 볼")
    p = monkey.add_reference(p.id, b"\x89PNG ref", ".png")
    _workspace(ctx, info_count=0, meme_count=1, hero_default=p.id)
    ctx.scenario_writer = FakeScenarioWriter()
    run_picks(ctx, fetch_photo=_fake_photo)
    req = ctx.scenario_writer.requests[0]
    assert req.character_name == "치이카와" and "분홍 볼" in req.character_description
    meme = next(x for x in PicksStore(ctx.repo.db, ctx.clock).list() if x.kind == "meme")
    raw, _ = ProductionStore(ctx.repo.db, ctx.clock).get_production(meme.content_id)
    import json

    hero = json.loads(raw)["hero"]
    assert (
        hero["profile_id"] == p.id
        and len(hero["references"]) == 1
        and "content" in hero["references"][0]
    )


def test_model_marked_same_joke_is_dropped_and_discovery_failure_still_creates(
    ctx,  # noqa: F811
    monkeypatch,
) -> None:
    from nsm.ports import MemeIdea

    # 최근 밈 하나(0번)가 있을 때만 "0번과 같다"가 유효하다(S16 리뷰: 범위 밖 번호는 중복으로 보지 않음)
    monkeypatch.setattr(
        SlotStore,
        "recent_meme_plans",
        lambda self, since: [{"situation": "옛 농담", "punchline": "옛 반전"}],
    )

    class Broken:
        def discover(self, topic):
            raise RuntimeError("blocked")

    ctx.meme_discovery = Broken()
    _workspace(ctx, info_count=0, meme_count=1)
    ideas = [
        MemeIdea("같은 농담", "a vs b", "반전", "", (), 0),  # 모델: 최근 0번과 같은 농담
        MemeIdea("새 상황", "c vs d", "새 반전", "", (), -1),
    ]
    ctx.scenario_writer = FakeScenarioWriter(ideas=ideas)
    r = run_picks(ctx, fetch_photo=_fake_photo)
    assert r.counts["made_meme"] == 1
    assert ctx.scenario_writer.scripts[0].idea.situation == "새 상황"
    access = r.counts["meme_access"]
    assert any("실패" in v for v in access.values())  # 탐색이 막힌 것을 기록하고 창작으로 진행
    meme = next(x for x in PicksStore(ctx.repo.db, ctx.clock).list() if x.kind == "meme")
    plan = SlotStore(ctx.repo.db, ctx.clock).by_content(meme.content_id).plan
    assert plan["category"] == "D" and plan["basis"] == []


def test_discovered_examples_feed_ideas_and_category(ctx) -> None:  # noqa: F811
    from datetime import timedelta

    from nsm.adapters.sources.meme_discovery import DiscoveryResult
    from nsm.domain.meme_discovery import Sighting
    from nsm.ports import MemeIdea

    now = ctx.clock.now()

    class Found:
        def discover(self, topic):
            s = [
                Sighting(
                    f"https://blog.naver.com/u{i}/1{i}",
                    "굿즈 품절 짤 밈 모음",
                    "다 팔렸다",
                    "네이버 블로그",
                    now - timedelta(days=1),
                    now,
                    f"blog.naver.com/u{i}",
                )
                for i in range(3)
            ]
            return DiscoveryResult(
                sightings=s, access={"네이버 블로그": "ok 3건", "X": "수집 안 함"}
            )

    ctx.meme_discovery = Found()
    _workspace(ctx, info_count=0, meme_count=1)
    ctx.scenario_writer = FakeScenarioWriter(
        ideas=[MemeIdea("품절 직전", "설렘 vs 절망", "품절?", "", (0,), -1)]
    )
    run_picks(ctx, fetch_photo=_fake_photo)
    req = ctx.scenario_writer.requests[0]
    assert req.examples and req.examples[0].category == "A"
    meme = next(x for x in PicksStore(ctx.repo.db, ctx.clock).list() if x.kind == "meme")
    plan = SlotStore(ctx.repo.db, ctx.clock).by_content(meme.content_id).plan
    assert plan["category"] == "A" and plan["basis"][0]["authors"] == 3
    assert all(s["metrics"] is None for s in plan["basis"][0]["sightings"])  # 반응 수치는 미확인


def test_same_topic_is_discovered_once_per_run(ctx) -> None:  # noqa: F811
    """밈 슬롯이 둘이어도 같은 주제는 한 번만 탐색한다(실검증에서 같은 검색을 두 번 하던 것)."""
    from nsm.adapters.sources.meme_discovery import DiscoveryResult
    from nsm.ports import MemeIdea

    calls: list[str] = []

    class Counting:
        def discover(self, topic):
            calls.append(topic)
            return DiscoveryResult(sightings=[], access={"네이버 블로그": "ok 0건"})

    ctx.meme_discovery = Counting()
    ctx.config = ctx.config.model_copy(
        update={"persona": ctx.config.persona.model_copy(update={"topics": ["치이카와"]})}
    )
    _workspace(ctx, info_count=0, meme_count=2)
    ctx.scenario_writer = FakeScenarioWriter(
        ideas=[
            MemeIdea("간식 앞에서 고민", "설렘 vs 망설임", "다 먹었어요", "", (), -1),
            MemeIdea("풀 뽑기 시험", "자신감 vs 당황", "이름부터요?", "", (), -1),
        ]
    )
    run_picks(ctx, fetch_photo=_fake_photo)
    assert calls == ["치이카와"]


def test_model_judged_same_event_is_not_recommended_twice(ctx) -> None:  # noqa: F811
    """제목 낱말이 달라도 모델이 같은 사건이라고 하면 두 번째 정보 원고를 만들지 않는다(S16 실검증)."""
    from nsm.domain.models import Classification

    class SameEvent:
        def __init__(self) -> None:
            self.asked: list[str] = []

        def classify(self, text, labels, instruction):
            if labels != ["same_event", "different_event"]:  # 수집 때 관련성 판정
                return Classification(label="relevant", confidence=0.9)
            self.asked.append(instruction)
            return Classification(label="same_event", confidence=0.9)

    judge = SameEvent()
    ctx.relevance_classifier = judge
    ctx.classifier_name = "openai"
    _workspace(ctx, info_count=2, meme_count=0)
    r = run_picks(ctx, fetch_photo=_fake_photo)
    assert r.counts["made_info"] == 1 and r.counts["same_event_skipped"] >= 1
    assert judge.asked and "이미 만든 소식" in judge.asked[0]


def test_keyword_classifier_does_not_judge_same_event(ctx) -> None:  # noqa: F811
    ctx.classifier_name = "keyword"
    _workspace(ctx, info_count=2, meme_count=0)
    assert run_picks(ctx, fetch_photo=_fake_photo).counts["made_info"] == 2


def test_same_as_recent_out_of_range_is_not_a_duplicate(ctx) -> None:  # noqa: F811
    """최근 밈이 없는데 모델이 0을 내도 아이디어를 버리지 않는다(S16 리뷰)."""
    from nsm.ports import MemeIdea

    _workspace(ctx, info_count=0, meme_count=1)
    ctx.scenario_writer = FakeScenarioWriter(ideas=[MemeIdea("첫 밈", "a vs b", "반전", "", (), 0)])
    assert run_picks(ctx, fetch_photo=_fake_photo).counts["made_meme"] == 1


def test_unchosen_hero_is_not_saved_as_cast_and_full_mode_does_not_enqueue(ctx) -> None:  # noqa: F811
    """고르지 않은 주인공(대본 제안)을 출연진으로 저장하지 않고, 완성본 모드여도 그림 작업을 넣지 않는다(S16 리뷰)."""
    from nsm.store.production import ProductionStore

    _workspace(ctx, info_count=0, meme_count=1, production_mode="full")
    ctx.scenario_writer = FakeScenarioWriter()
    run_picks(ctx, fetch_photo=_fake_photo)
    meme = next(x for x in PicksStore(ctx.repo.db, ctx.clock).list() if x.kind == "meme")
    sl = SlotStore(ctx.repo.db, ctx.clock).by_content(meme.content_id)
    assert sl.stage == "images_pending" and "고르세요" in sl.last_error
    prod = ProductionStore(ctx.repo.db, ctx.clock)
    assert prod.jobs_for(meme.content_id) == []
    import json

    cast_json, _ = prod.get_production(meme.content_id)
    assert cast_json is None or json.loads(cast_json).get("hero") is None


def test_meme_topic_does_not_count_for_post_rotation(ctx) -> None:  # noqa: F811
    """밈 원고의 주제는 일반 원고 주제 순환(persona_topic_last_used)에 세지 않는다(S16 리뷰)."""
    _workspace(ctx, info_count=0, meme_count=1)
    ctx.scenario_writer = FakeScenarioWriter()
    run_picks(ctx, fetch_photo=_fake_photo)
    assert ctx.repo.topic_last_used("meme")
    assert ctx.repo.persona_topic_last_used() == {}
