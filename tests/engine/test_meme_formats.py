"""밈 형식 재해석(S17 제안, ADR 0061): 유명·유행 밈을 자동으로 골라 구도·레이아웃까지 다시 연출한 한 장.

- 덱 계약: 밈 한 장(meme_image)은 마무리 장 없이 완결
- 제작: 캐릭터 참조와 밈 참조를 따로(밈 참조는 마지막), 역할을 프롬프트에 밝힘, 글자 자리 비우기
- 추천: 형식 후보 → 자동 선택 → 완성본(완성본 모드면 그림 작업까지), 근거·출처를 구분해 저장, 다른 밈으로 다시 만들기
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nsm.domain.models import CardDeck, DeckBrand, DeckPersona, MemeImageSlide, MemeOverlay
from nsm.engine.picks import run_picks
from nsm.ports import MemeFormatCandidate
from nsm.store.picks import PicksStore
from nsm.store.slots import SlotStore
from tests.engine.test_picks_job import _fake_photo, ctx  # noqa: F401
from tests.engine.test_picks_slots import _workspace
from tests.fakes import FakeImageStudio, FakeScenarioWriter

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15"
    b"\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x35\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _deck(slides):
    return CardDeck(
        template="meme",
        brand=DeckBrand(name="b", primary_color="#112233", accent_color="#445566"),
        persona=DeckPersona(name="p", handle="@p"),
        slides=slides,
    )


def test_single_meme_image_deck_is_complete_without_outro() -> None:
    d = _deck([MemeImageSlide(overlays=[MemeOverlay(kind="caption", text="괜찮아", x=5, y=5)])])
    assert len(d.slides) == 1
    with pytest.raises(ValueError):
        CardDeck(
            template="info",
            brand=d.brand,
            persona=d.persona,
            slides=[MemeImageSlide()],
        )


class Formats:
    def __init__(self, tmp: Path) -> None:
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / "fine.png").write_bytes(PNG)
        self.calls = 0
        self.cands = [
            MemeFormatCandidate(
                key="this-is-fine",
                title="This Is Fine",
                url="https://knowyourmeme.com/memes/this-is-fine",
                image_path=str(tmp / "fine.png"),
                image_url="https://i.example/fine.png",
                era="classic",
                evidence="2013 Gunshow #648, 4,300 upvotes on Imgur",
                about="dog in burning room",
                site="Know Your Meme",
                checked_at="2026-10-11T09:00:00+09:00",
            ),
            MemeFormatCandidate(
                key="daily-struggle",
                title="Daily Struggle / Two Buttons",
                url="https://knowyourmeme.com/memes/daily-struggle",
                era="classic",
                site="Know Your Meme",
            ),
        ]

    def discover(self):
        from nsm.adapters.sources.meme_formats import FormatDiscovery

        self.calls += 1
        return FormatDiscovery(list(self.cands), {"Know Your Meme 항목": "ok 2/2건"})


def _hero(ctx, source="https://news.example.com/poster") -> None:  # noqa: F811
    from nsm.engine.production import characters

    store = characters(ctx.config)
    p = store.create("치이카와", "하얀 동그란 몸")
    store.add_reference(p.id, PNG, ".png", source=source)
    ctx.config = ctx.config.model_copy(
        update={
            "workspace": ctx.config.workspace.model_copy(
                update={"hero_default": p.id, "production_mode": "full"}
            )
        }
    )


@pytest.fixture
def fctx(ctx, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("NSM_HOME", str(tmp_path / "home"))
    ctx.meme_formats = Formats(tmp_path / "fmt")
    ctx.scenario_writer = FakeScenarioWriter()
    ctx.image_studio = FakeImageStudio(tmp_path / "studio")
    _workspace(ctx, info_count=0, meme_count=1)
    return ctx


def _meme(c):
    pick = next(x for x in PicksStore(c.repo.db, c.clock).list() if x.kind == "meme")
    return (
        pick,
        c.repo.get_content(pick.content_id),
        SlotStore(c.repo.db, c.clock).by_content(pick.content_id),
    )


def test_auto_picks_format_and_builds_single_meme_with_separate_sources(fctx) -> None:
    _hero(fctx)
    run_picks(fctx, fetch_photo=_fake_photo)
    _pick, c, sl = _meme(fctx)
    deck = c.generated.deck
    assert [s.kind for s in deck.slides] == ["meme_image"]  # 출처 전용 마지막 장 없음
    s = deck.slides[0]
    assert s.format_key == "this-is-fine" and {o.kind for o in s.overlays} == {"caption", "bubble"}
    assert any("참고 밈" in x for x in s.credits) and any("캐릭터 참조" in x for x in s.credits)
    plan = sl.plan
    assert plan["mode"] == "format" and plan["format"]["url"].endswith("this-is-fine")
    assert plan["sources"]["character"] == ["https://news.example.com/poster"]
    assert plan["sources"]["meme_ref"][0].endswith("this-is-fine")
    assert [r["key"] for r in plan["ranking"]] == ["this-is-fine", "daily-struggle"]
    assert "참고 밈: This Is Fine — https://knowyourmeme.com" in c.generated.instagram_caption
    # 캐릭터 참조와 밈 참조는 다른 폴더
    root = fctx.media_dir / "content" / c.id
    assert list((root / "meme_ref").glob("this-is-fine.*")) and list((root / "cast").glob("*"))
    # 완성본 모드: 그림 작업이 들어가 있다
    from nsm.store.production import ProductionStore

    jobs = ProductionStore(fctx.repo.db, fctx.clock).jobs_for(c.id)
    assert [j.target for j in jobs] == ["s0.image"]


def test_image_spec_puts_meme_reference_last_with_roles(fctx) -> None:
    from nsm.engine.production import run_images

    _hero(fctx)
    run_picks(fctx, fetch_photo=_fake_photo)
    run_images(fctx)
    spec = fctx.image_studio.specs[0]
    assert spec.width == 1024 and spec.height == 1280
    assert Path(spec.references[-1]).parent.name == "meme_ref"
    assert Path(spec.references[0]).parent.name == "cast"
    assert "CHARACTER reference" in spec.prompt and "MEME FORMAT reference" in spec.prompt
    assert "bubble at" in spec.prompt  # 글자 자리를 비워 두라고
    _pick, c, _sl = _meme(fctx)
    img = c.generated.deck.slides[0].image
    assert img is not None and img.ai is True


def test_no_character_plans_but_does_not_enqueue(fctx) -> None:
    run_picks(fctx, fetch_photo=_fake_photo)
    _pick, c, sl = _meme(fctx)
    from nsm.store.production import ProductionStore

    assert ProductionStore(fctx.repo.db, fctx.clock).jobs_for(c.id) == []
    assert "고르세요" in sl.last_error and sl.plan["character"]["chosen"] is False


def test_no_format_candidates_falls_back_to_two_panel_script(fctx) -> None:
    fctx.meme_formats.cands = []
    run_picks(fctx, fetch_photo=_fake_photo)
    _pick, c, _sl = _meme(fctx)
    assert c.generated.deck.slides[0].kind == "comic"


def test_remake_with_other_candidate(fctx) -> None:
    from nsm.engine.memes import remake_with_format

    _hero(fctx)
    run_picks(fctx, fetch_photo=_fake_photo)
    _pick, c, _sl = _meme(fctx)
    title = remake_with_format(fctx, c.id, "daily-struggle")
    _pick, c2, sl2 = _meme(fctx)
    assert title.startswith("Daily Struggle")
    assert c2.generated.deck.slides[0].format_key == "daily-struggle"
    assert sl2.plan["format"]["key"] == "daily-struggle"
    assert fctx.scenario_writer.format_requests[-1].force_key == "daily-struggle"
    assert not list((fctx.media_dir / "content" / c.id / "meme_ref").glob("this-is-fine.*"))
    from nsm.ports import AdapterError

    with pytest.raises(AdapterError):
        remake_with_format(fctx, c.id, "no-such-meme")


def test_recent_formats_are_passed_to_avoid_repeats(fctx) -> None:
    _hero(fctx)
    run_picks(fctx, fetch_photo=_fake_photo)
    plan = _meme(fctx)[2].plan
    from nsm.engine.memes import format_request

    plans = SlotStore(fctx.repo.db, fctx.clock).recent_meme_plans(
        fctx.clock.now().replace(year=2000)
    )
    req = format_request(fctx, "치이카와", None, fctx.meme_formats.cands, plans, None)
    assert req.recent_formats == (plan["format"]["key"],)


def test_profile_reference_source_round_trips(fctx) -> None:
    from nsm.engine.production import characters, default_cast

    _hero(fctx, source="https://example.org/official")
    cast = default_cast(fctx, "x1")
    assert cast.hero.sources == ("https://example.org/official",)
    p = characters(fctx.config).list()[0]
    assert p.sources() == ["https://example.org/official"]
    from nsm.domain.production import Cast

    assert Cast.from_json(cast.to_json()).hero.sources == cast.hero.sources


def test_remake_job_reports_reason_instead_of_crashing(fctx) -> None:
    from nsm.engine.runner import run_meme_remake

    r = run_meme_remake(fctx, "missing|daily-struggle")
    assert r.ok is False and r.errors
    _hero(fctx)
    run_picks(fctx, fetch_photo=_fake_photo)
    cid = _meme(fctx)[1].id
    ok = run_meme_remake(fctx, f"{cid}|daily-struggle")
    assert ok.ok and ok.counts["format"].startswith("Daily Struggle")


def test_remake_replaces_picture_scene_and_review_reason(fctx) -> None:
    """다시 만들기는 예전 그림·장면 지시를 남기지 않고, 새 계획으로 검토 사유·추천 이유를 다시 쓴다(S17 리뷰)."""
    from nsm.engine.memes import remake_with_format
    from nsm.engine.production import run_images
    from nsm.store.production import ProductionStore

    _hero(fctx)
    run_picks(fctx, fetch_photo=_fake_photo)
    run_images(fctx)
    pick, c, _sl = _meme(fctx)
    prod = ProductionStore(fctx.repo.db, fctx.clock)
    assert prod.active(c.id, "s0.image") is not None
    prod.set_scene(c.id, "s0.image", "예전 장면")
    remake_with_format(fctx, c.id, "daily-struggle")
    assert prod.active(c.id, "s0.image") is None
    assert prod.get_production(c.id)[1].get("s0.image") is None
    jobs = [j for j in prod.jobs_for(c.id) if j.status == "queued"]
    assert [j.target for j in jobs] == ["s0.image"]  # 새 그림 작업
    pick2, c2, _ = _meme(fctx)
    assert "Daily Struggle" in c2.review_reason and "Daily Struggle" in pick2.reason
    assert pick2.title_fingerprint.startswith("format:daily-struggle")


def test_format_meme_gets_fact_signal_and_card_text_rules(fctx, monkeypatch) -> None:
    from nsm.domain.quality import slide_texts
    from nsm.ports import MemeOverlaySpec

    orig = fctx.scenario_writer.plan_meme_format

    def with_price(req):
        from dataclasses import replace

        p = orig(req)
        return replace(
            p, overlays=(MemeOverlaySpec("caption", "오늘 3시 판매 12,000원", 5, 5, 60),)
        )

    monkeypatch.setattr(fctx.scenario_writer, "plan_meme_format", with_price)
    _hero(fctx)
    run_picks(fctx, fetch_photo=_fake_photo)
    _pick, c, _ = _meme(fctx)
    assert "사실 확인 신호" in c.review_reason
    assert "오늘 3시 판매 12,000원" in slide_texts(c.generated.deck.slides[0])


def test_format_source_is_off_without_web_collection(app_config, tmp_path, monkeypatch) -> None:
    from contextlib import ExitStack

    from nsm.runtime import _meme_formats

    monkeypatch.setenv("NSM_HOME", str(tmp_path))
    w = app_config.workspace.model_copy(update={"meme_count": 1})
    s = app_config.settings
    off = app_config.model_copy(
        update={
            "workspace": w,
            "settings": s.model_copy(
                update={
                    "collect": s.collect.model_copy(update={"naver": False, "community": False})
                }
            ),
        }
    )
    with ExitStack() as st:
        assert _meme_formats(off, None, st) is None
        on = off.model_copy(
            update={
                "settings": s.model_copy(
                    update={"collect": s.collect.model_copy(update={"naver": True})}
                )
            }
        )
        assert _meme_formats(on, None, st) is not None
