"""그림 제작 엔진(S15 제안, ADR 0058): 영속 작업·슬롯별 spec·2컷 의존·실패 격리·비용·복구·출연진 흐름."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nsm.domain.models import CardDeck, Content
from nsm.domain.production import Cast, CharacterRef
from nsm.engine.context import JobContext
from nsm.engine.production import (
    enqueue_production,
    recover_image_jobs,
    run_images,
    upload_image,
    use_image,
)
from nsm.engine.publish import labeled_deck
from nsm.ports import ImageRefused, ImageUnknown
from nsm.store.production import ProductionStore
from nsm.store.repo import Repository
from tests.fakes import FakeImageStudio, FakeNotifier, FakeTextGenerator, load_source_items
from tests.render.test_html_renderer import FIXTURES

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8ffff3f0005fe02fea7d6a4b00000000049454e44ae426082"
)


@pytest.fixture
def ctx(db, clock, app_config, tmp_path: Path, monkeypatch) -> JobContext:
    monkeypatch.setenv("NSM_HOME", str(tmp_path / "home"))
    c = JobContext(
        config=app_config,
        repo=Repository(db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        media_dir=tmp_path / "media",
    )
    c.image_studio = FakeImageStudio(tmp_path / "studio")
    return c


def _meme_content(ctx: JobContext, cid: str = "m1") -> str:
    deck = CardDeck.model_validate(json.loads((FIXTURES / "meme.json").read_text("utf-8")))
    raw = deck.model_dump(mode="json")
    for p in raw["slides"][0]["panels"]:
        p["image"] = None  # 그림은 제작이 채운다
    raw["labels"]["ai_slides"] = []
    from nsm.domain.models import ContentBrief

    gen = FakeTextGenerator().generate_content(
        ContentBrief(topic="밈", template="info", sources=load_source_items()[:1]),
        ctx.config.persona,
        ctx.config.brand,
    )
    gen = gen.model_copy(
        update={"template": "meme", "deck": CardDeck.model_validate(raw), "claims": []}
    )
    ctx.repo.insert_content(
        Content(
            id=cid,
            status="pending_review",
            generated=gen,
            relevance_confidence=1.0,
            created_at=ctx.clock.now(),
        )
    )
    return cid


def _cast(
    ctx: JobContext, cid: str, tmp_path: Path, kind: str = "account", use: str = "generate"
) -> None:
    ref = tmp_path / f"ref-{kind}.png"
    ref.write_bytes(PNG + kind.encode())
    cast = Cast(
        hero=CharacterRef(
            kind=kind, use=use, name="루나", appearance="하얀 몸", references=(str(ref),)
        )
    )
    ProductionStore(ctx.repo.db, ctx.clock).set_cast(cid, cast.to_json(), "기본")


def _deck(ctx, cid):
    return ctx.repo.get_content(cid).generated.deck


def test_two_panels_both_activate_and_second_uses_first(ctx, tmp_path) -> None:
    cid = _meme_content(ctx)
    _cast(ctx, cid, tmp_path)
    r = enqueue_production(ctx, cid)
    assert r.queued == ["s0.panel0"] and r.waiting == ["s0.panel1"]  # 2번째는 1번째 확정 뒤
    run_images(ctx)
    deck = _deck(ctx, cid)
    assert all(p.image and p.image.ai for p in deck.slides[0].panels)
    studio = ctx.image_studio
    first_path = deck.slides[0].panels[0].image.path
    assert str(studio.specs[1].references[-1]) == first_path  # 2번째 컷이 1번째 결과를 참조
    jobs = ProductionStore(ctx.repo.db, ctx.clock).jobs_for(cid)
    assert [j.status for j in jobs] == ["succeeded", "succeeded"]
    assert labeled_deck(ctx.repo.get_content(cid)).labels.ai_slides == [0]  # 자산 기준 AI 표기


def test_dialogue_edit_while_in_flight_still_applies(ctx, tmp_path) -> None:
    cid = _meme_content(ctx)
    _cast(ctx, cid, tmp_path)

    def edit_bubble(spec):
        c = ctx.repo.get_content(cid)
        raw = c.generated.deck.model_dump(mode="json")
        raw["slides"][0]["panels"][0]["bubble"] = "대사만 바꿈"
        ctx.repo.update_generated(
            cid, c.generated.model_copy(update={"deck": CardDeck.model_validate(raw)})
        )

    ctx.image_studio.on_make = edit_bubble
    enqueue_production(ctx, cid, targets=["s0.panel0"])
    run_images(ctx, max_jobs=1)
    deck = _deck(ctx, cid)
    assert (
        deck.slides[0].panels[0].image is not None
        and deck.slides[0].panels[0].bubble == "대사만 바꿈"
    )


def test_scene_change_makes_late_result_a_candidate_only(ctx, tmp_path) -> None:
    cid = _meme_content(ctx)
    _cast(ctx, cid, tmp_path)

    def change_scene(spec):
        c = ctx.repo.get_content(cid)
        raw = c.generated.deck.model_dump(mode="json")
        raw["slides"][0]["panels"][0]["scene"] = "완전히 다른 장면"
        ctx.repo.update_generated(
            cid, c.generated.model_copy(update={"deck": CardDeck.model_validate(raw)})
        )

    ctx.image_studio.on_make = change_scene
    enqueue_production(ctx, cid, targets=["s0.panel0"])
    run_images(ctx, max_jobs=1)
    prod = ProductionStore(ctx.repo.db, ctx.clock)
    assert _deck(ctx, cid).slides[0].panels[0].image is None
    imgs = prod.images(cid, "s0.panel0")
    assert [i.state for i in imgs] == ["candidate"] and prod.jobs_for(cid)[
        0
    ].status == "succeeded_stale"
    # 장면이 바뀐 걸 보내기 전에 알면 돈을 쓰지 않고 취소
    ctx.image_studio.on_make = None
    enqueue_production(ctx, cid, targets=["s0.panel0"], force=True)
    change_scene(None)
    c = ctx.repo.get_content(cid)
    raw = c.generated.deck.model_dump(mode="json")
    raw["slides"][0]["panels"][0]["scene"] = "또 다른 장면"
    ctx.repo.update_generated(
        cid, c.generated.model_copy(update={"deck": CardDeck.model_validate(raw)})
    )
    calls = len(ctx.image_studio.specs)
    run_images(ctx, max_jobs=1)
    assert len(ctx.image_studio.specs) == calls and prod.jobs_for(cid)[-1].status == "cancelled"
    # 후보를 골라 쓰기(재사용)
    use_image(ctx, cid, imgs[0].id)
    assert _deck(ctx, cid).slides[0].panels[0].image.path == imgs[0].path


def test_failed_panel_retries_only_that_panel(ctx, tmp_path) -> None:
    cid = _meme_content(ctx)
    _cast(ctx, cid, tmp_path)
    ctx.image_studio.fail = {"s0.panel0": ImageRefused("blocked", ("violence",), "input")}
    enqueue_production(ctx, cid)
    run_images(ctx)
    prod = ProductionStore(ctx.repo.db, ctx.clock)
    j = prod.jobs_for(cid)[0]
    assert j.status == "failed" and "violence" in j.error
    from nsm.store.slots import SlotStore  # noqa: F401  (슬롯이 없는 원고도 동작)

    assert prod.requeue(j.id)
    run_images(ctx)
    deck = _deck(ctx, cid)
    assert all(p.image for p in deck.slides[0].panels) and len(ctx.image_studio.specs) == 3


def test_unknown_is_not_retried_and_restart_recovery(ctx, tmp_path) -> None:
    cid = _meme_content(ctx)
    _cast(ctx, cid, tmp_path)
    ctx.image_studio.fail = {"s0.panel0": ImageUnknown("timeout")}
    enqueue_production(ctx, cid)
    run_images(ctx)
    prod = ProductionStore(ctx.repo.db, ctx.clock)
    assert prod.jobs_for(cid)[0].status == "unknown" and len(ctx.image_studio.specs) == 1
    run_images(ctx)
    assert len(ctx.image_studio.specs) == 1  # 자동으로 다시 보내지 않는다
    # 재시작: 안 보낸 작업은 대기로, 보낸 작업은 응답 불명으로
    ctx.repo.db.execute("UPDATE image_jobs SET status = 'claimed'")
    job = prod.enqueue(
        cid, "s0.panel0", "panel", "h2", {"prompt": "x", "width": 1536, "height": 1024}, "기본"
    )
    ctx.repo.db.execute("UPDATE image_jobs SET status = 'sent' WHERE id = ?", [job.id])
    queued, unknown = recover_image_jobs(ctx)
    assert (queued, unknown) == (1, 1) and prod.job(job.id).status == "unknown"


def test_cost_cap_and_duplicate_enqueue(ctx, tmp_path) -> None:
    cid = _meme_content(ctx)
    _cast(ctx, cid, tmp_path)
    enqueue_production(ctx, cid)
    assert enqueue_production(ctx, cid).queued == []  # 같은 spec이 진행 중이면 또 만들지 않는다
    ctx.repo.record_cost("llm", "openai", 99.0)
    r = run_images(ctx)
    assert r.counts.get("cost_cap_hit") and ctx.image_studio.specs == []
    assert (
        ProductionStore(ctx.repo.db, ctx.clock).jobs_for(cid)[0].status == "queued"
    )  # 대기 그대로


def test_no_key_and_missing_reference_fail_with_reason(ctx, tmp_path) -> None:
    cid = _meme_content(ctx)
    ProductionStore(ctx.repo.db, ctx.clock).set_cast(
        cid, Cast(hero=CharacterRef(kind="content", name="치이카와")).to_json(), "기본"
    )
    r = enqueue_production(ctx, cid)
    assert r.queued == [] and "참조" in r.problems["s0.panel0"]
    _cast(ctx, cid, tmp_path)
    enqueue_production(ctx, cid)
    ctx.image_studio = None
    run_images(ctx)
    j = ProductionStore(ctx.repo.db, ctx.clock).jobs_for(cid)[0]
    assert j.status == "failed" and "API 키" in j.error
    assert _deck(ctx, cid).slides[0].panels[0].image is None  # 도형 등으로 대체하지 않는다


@pytest.mark.parametrize("kind", ["account", "content", "custom"])
def test_each_cast_kind_produces_with_its_own_references(ctx, tmp_path, kind) -> None:
    cid = _meme_content(ctx, f"m-{kind}")
    _cast(ctx, cid, tmp_path, kind=kind)
    enqueue_production(ctx, cid)
    run_images(ctx)
    spec = ctx.image_studio.specs[0]
    assert str(spec.references[0]).endswith(f"ref-{kind}.png")
    assert all(p.image and p.image.ai for p in _deck(ctx, cid).slides[0].panels)


def test_real_only_content_character_is_not_generated(ctx, tmp_path) -> None:
    cid = _meme_content(ctx)
    _cast(ctx, cid, tmp_path, kind="content", use="real_only")
    r = enqueue_production(ctx, cid)
    assert r.queued == [] and "실제 자료만" in r.problems["s0.panel0"]
    # 실제 자료(업로드)는 원본으로 들어가고 AI 표기가 붙지 않는다
    up = tmp_path / "real.png"
    up.write_bytes(PNG)
    upload_image(ctx, cid, "s0.panel0", up)
    deck = labeled_deck(ctx.repo.get_content(cid))
    assert deck.slides[0].panels[0].image.ai is False and deck.labels.ai_slides == []


def test_profile_cast_copies_references(ctx, tmp_path) -> None:
    from nsm.engine.production import characters, default_cast

    store = characters(ctx.config)
    p = store.create("루나", "하얀 몸")
    p = store.add_reference(p.id, PNG, ".png")
    ctx.config = ctx.config.model_copy(
        update={"workspace": ctx.config.workspace.model_copy(update={"hero_default": p.id})}
    )
    cid = _meme_content(ctx)
    cast = default_cast(ctx, cid)
    assert cast.hero.profile_id == p.id and cast.hero.profile_revision == p.revision
    copied = Path(cast.hero.references[0])
    assert "content" in copied.parts and copied.read_bytes() == PNG  # 원고 폴더 복사본
    store.remove_reference(p.id, Path(p.references[0]).name)
    assert copied.is_file()  # 프로필을 고쳐도 이 원고의 참조는 그대로


def test_apply_failure_keeps_paid_image_and_reapplies_without_api(
    ctx, tmp_path, monkeypatch
) -> None:
    import nsm.engine.production as prodmod
    from nsm.engine.production import apply_saved_result

    cid = _meme_content(ctx)
    _cast(ctx, cid, tmp_path)
    enqueue_production(ctx, cid, targets=["s0.panel0"])
    real = prodmod._apply_to_deck
    monkeypatch.setattr(
        prodmod, "_apply_to_deck", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db busy"))
    )
    run_images(ctx, max_jobs=1)
    prod = ProductionStore(ctx.repo.db, ctx.clock)
    job = prod.jobs_for(cid)[0]
    assert job.status == "apply_failed" and Path(job.result_path).is_file()
    assert [i.state for i in prod.images(cid, "s0.panel0")] == ["candidate"]  # 이력·파일 보존
    calls = len(ctx.image_studio.specs)
    monkeypatch.setattr(prodmod, "_apply_to_deck", real)
    assert apply_saved_result(ctx, job.id) == "succeeded"
    assert len(ctx.image_studio.specs) == calls  # API를 다시 부르지 않았다
    assert _deck(ctx, cid).slides[0].panels[0].image is not None


def test_default_cast_never_invents_a_hero(ctx, tmp_path) -> None:
    from nsm.engine.production import default_cast

    b = tmp_path / "brand.png"
    b.write_bytes(PNG)
    ctx.config = ctx.config.model_copy(
        update={
            "brand": ctx.config.brand.model_copy(
                update={"character": "custom", "character_cover": str(b)}
            )
        }
    )
    cast = default_cast(ctx, "x")
    assert (
        cast.hero is None and cast.sidekick is None
    )  # 페르소나·브랜드 그림으로 주인공을 만들지 않는다


def test_cost_ref_includes_content_id(ctx, tmp_path) -> None:
    cid = _meme_content(ctx)
    _cast(ctx, cid, tmp_path)
    enqueue_production(ctx, cid, targets=["s0.panel0"])
    run_images(ctx, max_jobs=1)
    assert ctx.image_studio.specs[0].ref == f"panel:{cid}:s0.panel0"


def test_new_content_ai_slides_ignore_legacy_flag(ctx, tmp_path) -> None:
    cid = _meme_content(ctx)
    c = ctx.repo.get_content(cid)
    deck = c.generated.deck.model_copy(
        update={"labels": c.generated.deck.labels.model_copy(update={"ai_generated_image": True})}
    )
    raw = deck.model_dump(mode="json")
    raw["slides"][-1]["background"] = str(tmp_path / "real.jpg")
    (tmp_path / "real.jpg").write_bytes(PNG)
    ctx.repo.update_generated(
        cid, c.generated.model_copy(update={"deck": CardDeck.model_validate(raw)})
    )
    # ai_slides=[](새 콘텐츠): 배경이 있어도 레거시 플래그로 AI 표기를 붙이지 않는다
    assert labeled_deck(ctx.repo.get_content(cid)).labels.ai_slides == []
