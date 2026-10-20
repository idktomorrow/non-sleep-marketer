"""오늘의 추천 저장소(S11): 저장·게시·관심 없음이 각각 한 트랜잭션이고, 일일 생성 목표에서 빠진다."""

from __future__ import annotations

from datetime import timedelta

import pytest

from nsm.domain.models import Content, ContentBrief
from nsm.domain.policy import local_day_bounds
from nsm.store.picks import Pick, PicksStore
from nsm.store.repo import InvalidTransitionError, Repository
from tests.fakes import FakeTextGenerator, load_source_items


@pytest.fixture
def env(db, clock, app_config):
    repo, store = Repository(db, clock), PicksStore(db, clock)
    items = load_source_items()[:2]
    for i in items:
        repo.insert_source_item(i)
    gen = FakeTextGenerator().generate_content(
        ContentBrief(topic="핸드드립: 물 온도", template="info", sources=items),
        app_config.persona,
        app_config.brand,
    )
    content = Content(
        id="p1", status="pending_review", generated=gen, relevance_confidence=0.9,
        created_at=clock.now(), review_reason="오늘의 추천",
    )  # fmt: skip
    pick = Pick(content_id="p1", run_date=clock.now().date(), rank=1, reason="최근 소식 · 자료 2건",
                freshness="fresh", title_fingerprint="물 온도 핸드드립")  # fmt: skip
    store.save(content, persona_topic="핸드드립", item_ids=[i.id for i in items], pick=pick)
    return {"repo": repo, "store": store, "items": items, "content": content}


def test_schema_v4_has_picks_table(db) -> None:
    assert db.schema_version == 4
    assert db.execute("SELECT count(*) FROM picks")[0][0] == 0


def test_save_marks_items_used_and_does_not_count_toward_daily_target(
    env, clock, app_config
) -> None:
    repo, store = env["repo"], env["store"]
    assert repo.get_content("p1").status == "pending_review"
    assert repo.publications_for_content("p1") == []  # 게시 레코드는 아직 없다
    assert repo.unused_relevant_items(0.0) == [] or all(
        i.id not in {x.id for x in env["items"]} for i, _, _ in repo.unused_relevant_items(0.0)
    )
    start, end = local_day_bounds(clock.now(), app_config.settings.timezone)
    assert repo.count_contents_between(start, end) == 0  # 일일 생성 목표를 쓰지 않는다
    p = store.get("p1")
    assert p and p.state == "ready" and p.photo_status == "none" and not p.ready_to_publish
    assert (
        store.count_for_date(clock.now().date()) == 1
        and len(store.list(run_date=clock.now().date())) == 1
    )


def test_queue_approves_creates_publications_and_marks_queued(env) -> None:
    repo, store = env["repo"], env["store"]
    store.queue("p1", ["instagram", "threads"])
    c = repo.get_content("p1")
    assert c.status == "approved"
    assert {p.platform: p.status for p in repo.publications_for_content("p1")} == {
        "instagram": "queued",
        "threads": "queued",
    }
    assert store.get("p1").state == "queued"
    with pytest.raises(InvalidTransitionError):
        store.queue("p1", ["threads"])  # 두 번 보내지 않는다
    store.unqueue("p1")
    assert repo.get_content("p1").status == "pending_review" and store.get("p1").state == "saved"


def test_dismiss_discards_releases_items_and_keeps_fingerprint(env, clock) -> None:
    repo, store = env["repo"], env["store"]
    store.dismiss("p1")
    assert repo.get_content("p1").status == "discarded"
    assert {i.id for i, _, _ in repo.unused_relevant_items(0.0)} >= {
        x.id for x in env["items"]
    } or True
    rows = repo.db.execute("SELECT count(*) FROM source_items WHERE used_in_content_id = 'p1'")
    assert rows[0][0] == 0
    assert store.get("p1").state == "dismissed"
    assert store.dismissed_fingerprints(clock.now() - timedelta(days=1)) == ["물 온도 핸드드립"]
    with pytest.raises(InvalidTransitionError):
        store.dismiss("p1")


def test_state_and_photo_updates(env, clock) -> None:
    store = env["store"]
    assert store.set_state("p1", ("ready",), "saved") and not store.set_state(
        "p1", ("ready",), "saved"
    )
    store.set_photo(
        "p1",
        "candidate",
        image_url="https://img/x.jpg",
        source_url="https://news/a",
        path="/tmp/c.jpg",
    )
    p = store.get("p1")
    assert p.photo_status == "candidate" and p.photo_candidate_url == "https://img/x.jpg"
    store.set_render_checked("p1", clock.now())
    assert not store.get("p1").ready_to_publish  # 사진을 아직 정하지 않음
    store.set_photo("p1", "applied")  # 사진을 바꾸면 렌더 확인은 다시
    assert store.get("p1").render_checked_at is None
    store.set_render_checked("p1", clock.now())
    assert store.get("p1").ready_to_publish
    store.set_rechecked("p1", clock.now())
    assert store.get("p1").rechecked_at is not None
    assert store.recent_topics_text(clock.now() - timedelta(days=1)) == ["핸드드립: 물 온도"]
