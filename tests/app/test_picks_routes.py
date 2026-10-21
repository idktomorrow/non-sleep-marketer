"""오늘의 추천 화면·동작(S11): 게시/보관/관심 없음, 사진 후보 쓰기, 재확인 관문, 프리셋."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nsm.app.server import AppState, create_app
from nsm.domain.models import Content, ContentBrief
from nsm.store.picks import Pick, PicksStore
from nsm.store.repo import Repository
from tests.app.test_dashboard import _run_now
from tests.fakes import FakeRenderer, FakeTextGenerator, load_source_items


@pytest.fixture
def env(db, clock, app_config, tmp_path: Path):
    repo, store = Repository(db, clock), PicksStore(db, clock)
    holder = {"cfg": app_config}

    def set_config(c):
        holder["cfg"] = c

    media = tmp_path / "media"
    state = AppState(
        repo=repo,
        clock=clock,
        get_config=lambda: holder["cfg"],
        set_config=set_config,
        submit=_run_now,
        adapters={},
        media_dir=media,
        run_job_now=lambda name: _run_now(lambda: name),
        job_names=frozenset({"collect", "picks"}),
        renderer=FakeRenderer(),
        picks=store,
        picks_dir=media / "picks",
    )
    client = TestClient(create_app(state))
    items = load_source_items()[:2]
    for i in items:
        repo.insert_source_item(i)
    gen = FakeTextGenerator().generate_content(
        ContentBrief(topic="핸드드립: 물 온도", template="info", sources=items),
        app_config.persona,
        app_config.brand,
    )
    content = Content(
        id="pk1", status="pending_review", generated=gen, relevance_confidence=0.9,
        created_at=clock.now(), review_reason="오늘의 추천",
    )  # fmt: skip
    photo_dir = media / "picks" / "pk1"
    photo_dir.mkdir(parents=True)
    photo = photo_dir / "candidate.png"
    photo.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8ffff3f0005fe02fea7d6a4b00000000049454e44ae426082"
        )
    )
    store.save(
        content,
        persona_topic="핸드드립",
        item_ids=[i.id for i in items],
        pick=Pick(
            content_id="pk1",
            run_date=clock.now().date(),
            rank=1,
            reason="최근 소식 · 자료 2건",
            freshness="fresh",
            photo_status="candidate",
            photo_candidate_url="https://img/x.png",
            photo_source_url="https://news.example.com/a/1",
            photo_candidate_path=str(photo),
            title_fingerprint="물 온도 핸드드립",
        ),  # fmt: skip
    )
    return {
        "client": client,
        "repo": repo,
        "store": store,
        "state": state,
        "holder": holder,
        "items": items,
    }


def _post(env, url: str, **data):
    return env["client"].post(
        url, data={"csrf": env["state"].csrf_token, **data}, follow_redirects=False
    )


def test_pages_show_pick_with_photo_candidate(env) -> None:
    html = env["client"].get("/picks").text
    assert "오늘의 추천" in html and "사진 확인 대기" in html and "pk1" in html
    home = env["client"].get("/").text
    assert "오늘의 추천" in home and "/picks/pk1/queue" in home
    detail = env["client"].get("/review/pk1").text
    assert "이 사진 쓰기" in detail and "/picks/pk1/photo" in detail
    assert env["client"].get("/picks/pk1/photo").status_code == 200
    assert _post(env, "/picks/pk1/save", csrf="wrong").status_code == 403


def test_photo_use_sets_cover_and_outro_background_and_resets_render_check(env) -> None:
    store, repo = env["store"], env["repo"]
    r = _post(env, "/picks/pk1/photo/use")
    assert r.status_code == 303
    deck = repo.get_content("pk1").generated.deck
    assert deck.slides[0].background and deck.slides[0].background == deck.slides[-1].background
    assert Path(deck.slides[0].background).is_file() and not deck.labels.ai_generated_image
    assert store.get("pk1").photo_status == "applied" and store.get("pk1").render_checked_at is None
    assert _post(env, "/picks/pk1/photo/use").status_code == 409  # 후보는 한 번만
    # 미리보기 렌더가 넘침 없이 끝나면 "게시 준비 완료"
    assert _post(env, "/review/pk1/preview").status_code == 303
    assert store.get("pk1").ready_to_publish
    assert "게시 준비 완료" in env["client"].get("/picks").text


def test_photo_drop_then_queue_creates_publications_in_one_step(env) -> None:
    store, repo = env["store"], env["repo"]
    assert _post(env, "/picks/pk1/photo/drop").status_code == 303
    assert store.get("pk1").photo_status == "dropped"
    r = _post(env, "/picks/pk1/queue")
    assert r.status_code == 303 and r.headers["location"] == "/queue"
    assert repo.get_content("pk1").status == "approved"
    assert {p.platform for p in repo.publications_for_content("pk1")} == {"instagram", "threads"}
    assert store.get("pk1").state == "queued"
    assert _post(env, "/picks/pk1/queue").status_code == 409
    # 게시 대기에서 되돌리면 보관으로
    assert _post(env, "/queue/pk1/unapprove").status_code == 303
    assert repo.get_content("pk1").status == "pending_review" and store.get("pk1").state == "saved"


def test_review_approve_route_also_creates_publications_for_picks(env) -> None:
    repo, store = env["repo"], env["store"]
    assert _post(env, "/review/pk1/approve").status_code == 303
    assert repo.get_content("pk1").status == "approved"
    assert len(repo.publications_for_content("pk1")) == 2 and store.get("pk1").state == "queued"


def test_save_then_dismiss_releases_items_and_keeps_record(env) -> None:
    repo, store = env["repo"], env["store"]
    assert _post(env, "/picks/pk1/save").status_code == 303
    assert store.get("pk1").state == "saved"
    assert _post(env, "/picks/pk1/save").status_code == 409  # 보관은 오늘의 추천 상태에서만
    assert _post(env, "/picks/pk1/dismiss").status_code == 303
    assert repo.get_content("pk1").status == "discarded" and store.get("pk1").state == "dismissed"
    assert (
        repo.db.execute("SELECT count(*) FROM source_items WHERE used_in_content_id = 'pk1'")[0][0]
        == 0
    )
    # 검토 화면의 폐기도 같은 길로 간다(추천 원고가 아니면 기존 동작)
    assert _post(env, "/review/pk1/discard").status_code == 409


def test_old_saved_pick_needs_recheck_before_queue(env, clock) -> None:
    store = env["store"]
    _post(env, "/picks/pk1/save")
    _post(env, "/picks/pk1/photo/drop")
    clock.advance(days=3)
    html = env["client"].get("/picks").text
    assert "다시 확인했어요" in html and "일 전 자료" in html
    assert _post(env, "/picks/pk1/queue").status_code == 409
    assert _post(env, "/picks/pk1/recheck").status_code == 303
    assert store.get("pk1").rechecked_at == clock.now()
    assert _post(env, "/picks/pk1/queue").status_code == 303
    clock.advance(days=2)  # 재확인도 오래되면 다시
    _post(env, "/queue/pk1/unapprove")
    assert _post(env, "/picks/pk1/queue").status_code == 409


def test_missing_pick_or_content(env) -> None:
    assert _post(env, "/picks/nope/save").status_code == 404
    assert env["client"].get("/picks/nope/photo").status_code == 404
    env["repo"].insert_content(
        Content(
            id="plain",
            status="pending_review",
            generated=env["repo"].get_content("pk1").generated,
            relevance_confidence=0.9,
            created_at=env["state"].clock.now(),
        )  # fmt: skip
    )
    assert _post(env, "/picks/plain/save").status_code == 404  # 추천 원고가 아님
    assert _post(env, "/review/plain/approve").status_code == 303  # 기존 승인은 그대로


def test_interest_preset_fills_topics_and_enables_mode(env) -> None:
    r = _post(env, "/settings/interest-preset", key="lol")
    assert r.status_code == 303 and r.headers["location"].startswith("/settings?saved=1")
    cfg = env["holder"]["cfg"]
    assert (
        cfg.settings.picks.enabled
        and "롤 패치" in cfg.persona.topics
        and "김밥" in cfg.settings.collect.exclude_words
    )
    assert _post(env, "/settings/interest-preset", key="nope").status_code == 400
    html = env["client"].get("/settings").text
    assert 'name="picks_enabled"' in html and "리그 오브 레전드" in html
    assert 'href="/picks"' in html  # 추천 모드가 켜지면 메뉴에 보인다


def test_settings_form_round_trips_picks_fields(env) -> None:
    from nsm.app.settings_form import SettingsError, apply_settings_form, form_values

    cfg = env["holder"]["cfg"]
    form = {
        **form_values(cfg),
        "picks_enabled": "on",
        "picks_count": "2",
        "picks_aliases": "핸드드립: hand drip, ハンドドリップ",
    }
    new = apply_settings_form(cfg, form)
    # 추천 구성·별칭은 작업 공간(프리셋)별 설정이다(S13)
    assert new.settings.picks.enabled and new.workspace.info_count == 2
    assert new.workspace.aliases == {"핸드드립": ["hand drip", "ハンドドリップ"]}
    assert form_values(new)["picks_aliases"] == "핸드드립: hand drip, ハンドドリップ"
    old_form = {k: v for k, v in form_values(new).items() if not k.startswith("picks_")}
    assert apply_settings_form(new, old_form).workspace.info_count == 2  # 옛 폼은 값을 지킨다
    # 사용자가 일부러 비운 별칭은 빈 값으로 남는다(이전 값 복원 안 함)
    assert apply_settings_form(new, {**form, "picks_aliases": ""}).workspace.aliases == {}
    with pytest.raises(SettingsError):
        apply_settings_form(new, {**form, "picks_count": "5", "meme_count": "3"})  # 합계 6 초과
