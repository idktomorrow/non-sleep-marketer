"""그림 제작·캐릭터 화면과 승인 산출물(S15 제안, ADR 0058·0059).

- 제작 버튼은 작업만 넣는다(요청 안에서 그리지 않음). 장면 수정·출연진 바꾸기·업로드·재사용·다시 요청·과금 안 됨 확인
- 승인 = 사용자가 본 미리보기 판을 굳히기. 미리보기 뒤에 원고·그림·설정이 바뀌면 다시 보여 주고 승인받는다
- 게시는 굳힌 PNG·캡션 그대로. 산출물이 사라지면 다시 그리지 않고 검토로
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nsm.app.server import AppState, create_app
from nsm.config.secrets import MemorySecretStore
from nsm.domain.models import CardDeck, Content, ContentBrief
from nsm.store.production import ProductionStore
from nsm.store.repo import Repository
from tests.app.test_dashboard import _run_now
from tests.fakes import FakeRenderer, FakeTextGenerator, load_source_items
from tests.render.test_html_renderer import FIXTURES

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8ffff3f0005fe02fea7d6a4b00000000049454e44ae426082"
)


@pytest.fixture
def env(db, clock, app_config, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NSM_HOME", str(tmp_path / "home"))
    repo = Repository(db, clock)
    holder = {"cfg": app_config}
    ran: list[str] = []
    renderer = FakeRenderer()
    renderer.version = "v1"
    state = AppState(
        repo=repo,
        clock=clock,
        get_config=lambda: holder["cfg"],
        set_config=lambda c: holder.__setitem__("cfg", c),
        submit=_run_now,
        adapters={},
        media_dir=tmp_path / "media",
        run_job_now=lambda name: _run_now(lambda: ran.append(name)),
        job_names=frozenset({"images", "publish"}),
        renderer=renderer,
        secrets=MemorySecretStore({"openai_api_key": "sk-test-000000000000000000"}),
    )
    deck = CardDeck.model_validate(json.loads((FIXTURES / "meme.json").read_text("utf-8")))
    raw = deck.model_dump(mode="json")
    for p in raw["slides"][0]["panels"]:
        p["image"] = None
    raw["labels"]["ai_slides"] = []
    gen = FakeTextGenerator().generate_content(
        ContentBrief(topic="밈", template="info", sources=load_source_items()[:1]),
        app_config.persona,
        app_config.brand,
    )
    gen = gen.model_copy(
        update={"template": "meme", "deck": CardDeck.model_validate(raw), "claims": []}
    )
    repo.insert_content(
        Content(
            id="m1",
            status="pending_review",
            generated=gen,
            relevance_confidence=1.0,
            created_at=clock.now(),
        )
    )
    return {
        "client": TestClient(create_app(state)),
        "state": state,
        "repo": repo,
        "ran": ran,
        "renderer": renderer,
        "holder": holder,
    }


def _post(env, url, files=None, **data):
    return env["client"].post(
        url, data={"csrf": env["state"].csrf_token, **data}, files=files, follow_redirects=False
    )


def _png(name="r.png"):
    return {"file": (name, io.BytesIO(PNG), "image/png")}


def test_cast_with_content_character_upload_then_make_enqueues_without_drawing(env) -> None:
    r = _post(
        env,
        "/review/m1/production/cast",
        files=_png(),
        who="hero",
        choice="content",
        use="generate",
        name="치이카와",
        appearance="하얀 몸",
    )
    assert r.status_code == 303
    prod = ProductionStore(env["repo"].db, env["state"].clock)
    raw, _ = prod.get_production("m1")
    cast = json.loads(raw)["hero"]
    assert cast["kind"] == "content" and cast["name"] == "치이카와" and len(cast["references"]) == 1
    assert _post(env, "/review/m1/production/make").status_code == 303
    jobs = prod.jobs_for("m1")
    assert [j.status for j in jobs] == ["queued"] and env["ran"] == [
        "images"
    ]  # 그리기는 작업 스레드에서
    html = env["client"].get("/review/m1").text
    assert "1번째 컷이 확정되면" in html and "출연진" in html and "치이카와" in html


def test_scene_edit_upload_reuse_and_retry_routes(env) -> None:
    _post(
        env,
        "/review/m1/production/cast",
        files=_png(),
        who="hero",
        choice="content",
        use="generate",
        name="치이카와",
    )
    assert (
        _post(
            env, "/review/m1/production/slots/s0.panel0/scene", scene="바닷가, 들뜬 표정"
        ).status_code
        == 303
    )
    assert (
        env["repo"].get_content("m1").generated.deck.slides[0].panels[0].scene
        == "바닷가, 들뜬 표정"
    )
    assert _post(env, "/review/m1/production/slots/s0.panel0/scene", scene=" ").status_code == 400
    r = _post(env, "/review/m1/production/slots/s0.panel0/upload", files=_png("real.png"))
    assert r.status_code == 303
    prod = ProductionStore(env["repo"].db, env["state"].clock)
    img = prod.active("m1", "s0.panel0")
    assert (
        img.source == "upload"
        and env["repo"].get_content("m1").generated.deck.slides[0].panels[0].image.ai is False
    )
    assert env["client"].get(f"/production/m1/images/{img.id}").status_code == 200
    # 다시 만들기 → 실패로 만든 뒤 다시 요청
    assert _post(env, "/review/m1/production/slots/s0.panel0/regenerate").status_code == 303
    job = prod.jobs_for("m1")[-1]
    prod.finish(job.id, "unknown", error="응답 불명", cost_status="unknown")
    assert _post(env, f"/review/m1/production/jobs/{job.id}/release").status_code == 303
    assert prod.job(job.id).status == "failed"
    assert _post(env, f"/review/m1/production/jobs/{job.id}/retry").status_code == 303
    assert prod.job(job.id).status == "queued"
    # 이전 그림 재사용
    assert _post(env, f"/review/m1/production/images/{img.id}/use").status_code == 303
    assert _post(env, "/review/m1/production/images/nope/use").status_code == 409


def test_production_routes_require_csrf_and_editable(env) -> None:
    c = env["client"]
    assert c.post("/review/m1/production/make", follow_redirects=False).status_code == 403
    env["repo"].transition_content("m1", "discarded")
    assert _post(env, "/review/m1/production/make").status_code == 409


def test_character_settings_flow(env) -> None:
    assert (
        _post(env, "/settings/characters", name="루나", appearance="하얀 몸, 분홍 볼").status_code
        == 303
    )
    from nsm.engine.production import characters

    p = characters(env["holder"]["cfg"]).list()[0]
    assert _post(env, f"/settings/characters/{p.id}/refs", files=_png()).status_code == 303
    assert _post(env, "/settings/characters/defaults", hero=p.id, sidekick="").status_code == 303
    assert env["holder"]["cfg"].workspace.hero_default == p.id
    html = env["client"].get("/settings").text
    assert 'id="characters"' in html and "루나" in html and "기본 주인공" in html
    assert _post(env, f"/settings/characters/{p.id}/generate-ref").status_code == 303
    prod = ProductionStore(env["repo"].db, env["state"].clock)
    assert prod.next_due(env["state"].clock.now(), None).target == f"char:{p.id}"
    assert _post(env, "/settings/characters/defaults", hero="nope").status_code == 303
    assert env["holder"]["cfg"].workspace.hero_default == p.id  # 없는 캐릭터는 저장 안 함


def test_approve_freezes_the_previewed_cards_and_publish_uses_them(env, app_config) -> None:
    repo = env["repo"]
    # 그림이 없는 밈이라도 슬롯이 없으면 게시 관문은 통과(이 테스트는 승인 산출물만 본다)
    r = _post(env, "/review/m1/approve")
    assert (
        "approve_blocked" in r.headers["location"]
        and repo.get_content("m1").status == "pending_review"
    )
    assert (
        _post(env, "/review/m1/approve").status_code == 303
        and repo.get_content("m1").status == "approved"
    )
    prod = ProductionStore(repo.db, env["state"].clock)
    art = prod.latest_artifact("m1")
    m = prod.latest_manifest("m1")
    assert (
        art.manifest_id == m.id
        and art.files_sha == m.files_sha
        and Path(art.dir, "captions.json").is_file()
    )
    # 게시: 굳힌 PNG 그대로(다시 그리지 않음)
    from nsm.engine.context import JobContext
    from nsm.engine.publish import render_images
    from tests.fakes import FakeNotifier

    ctx = JobContext(
        config=app_config,
        repo=repo,
        clock=env["state"].clock,
        notifier=FakeNotifier(),
        media_dir=env["state"].media_dir,
    )
    ctx.renderer = env["renderer"]
    n = len(env["renderer"].rendered)
    paths = render_images(ctx, repo.get_content("m1"))
    assert [p.parent for p in paths] == [Path(art.dir)] * len(paths) and len(
        env["renderer"].rendered
    ) == n


def test_changes_after_preview_require_a_new_preview(env) -> None:
    assert _post(env, "/review/m1/preview").status_code == 303
    env["renderer"].version = "v2"  # 전역 디자인 설정이 바뀜
    r = _post(env, "/review/m1/approve")
    assert (
        "approve_blocked" in r.headers["location"]
        and env["repo"].get_content("m1").status == "pending_review"
    )
    # 다시 그린 미리보기를 보고 승인
    assert _post(env, "/review/m1/approve").headers["location"] == "/review"
    assert env["repo"].get_content("m1").status == "approved"


def test_migration_adopts_cards_or_reverts(env, app_config) -> None:
    from nsm.engine.artifacts import migrate_approved
    from nsm.engine.context import JobContext
    from tests.fakes import FakeNotifier

    repo = env["repo"]
    repo.transition_content("m1", "approved", approved_by="human")
    ctx = JobContext(
        config=app_config,
        repo=repo,
        clock=env["state"].clock,
        notifier=FakeNotifier(),
        media_dir=env["state"].media_dir,
    )
    migrate_approved(ctx)  # 게시용 카드가 없으면 검토로 돌리고 알림
    assert (
        repo.get_content("m1").status == "pending_review"
        and ctx.notifier.calls[-1][0] == "다시 승인 필요"
    )
    repo.transition_content("m1", "approved", approved_by="human")
    migrate_approved(
        ctx
    )  # 두 번째 시작: 한 번만 돌므로 렌더러 없이 승인한 원고를 되돌리지 않는다(S15 리뷰)
    assert repo.get_content("m1").status == "approved"
    from nsm.engine.artifacts import MIGRATED_KEY

    repo.set_setting(MIGRATED_KEY, False)  # (다른 작업 공간 DB처럼) 처음 보는 DB
    cards = env["state"].media_dir / "cards" / "m1"
    cards.mkdir(parents=True)
    for i in range(2):
        (cards / f"slide-{i + 1:02d}.png").write_bytes(PNG)
    migrate_approved(ctx)
    prod = ProductionStore(repo.db, env["state"].clock)
    assert (
        repo.get_content("m1").status == "approved"
        and prod.latest_artifact("m1").approved_by == "legacy"
    )


def test_approve_rejects_when_revision_moves_after_freeze(env) -> None:
    from nsm.store.picks import PicksStore  # noqa: F401

    repo = env["repo"]
    assert _post(env, "/review/m1/preview").status_code == 303
    real = repo.approve_human

    def bump_then_approve(cid, expected):
        repo.db.execute(
            "UPDATE contents SET revision = revision + 1 WHERE id = ?", [cid]
        )  # 그 사이 그림 반영
        return real(cid, expected)

    repo.approve_human = bump_then_approve
    r = _post(env, "/review/m1/approve")
    assert r.status_code == 409 and repo.get_content("m1").status == "pending_review"
    prod = ProductionStore(repo.db, env["state"].clock)
    assert prod.latest_artifact("m1") is None  # 굳힌 것은 되돌렸다
    approved_dir = env["state"].media_dir / "approved" / "m1"
    assert (
        not any(p.name.startswith("rev") for p in approved_dir.iterdir())
        if approved_dir.exists()
        else True
    )


def test_view_has_no_side_effects(env) -> None:
    prod = ProductionStore(env["repo"].db, env["state"].clock)
    env["client"].get("/review/m1")
    assert prod.get_production("m1") == (None, {})  # 화면을 열기만 해서는 출연진을 저장하지 않는다
