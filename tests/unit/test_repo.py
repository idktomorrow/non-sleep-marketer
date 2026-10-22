from __future__ import annotations

from datetime import date, timedelta

import pytest

from nsm.domain.models import AccountMetrics, Content, PostMetrics
from nsm.store.repo import InvalidTransitionError, Repository
from tests.fakes import FakeTextGenerator, load_comment_cases, load_source_items


@pytest.fixture
def repo(db, clock) -> Repository:
    return Repository(db, clock)


def _content(repo: Repository, app_config, status="draft", cid="c1") -> Content:
    items = load_source_items()
    from nsm.domain.models import ContentBrief

    gen = FakeTextGenerator().generate_content(
        ContentBrief(topic="핸드드립", template="info", sources=items[:2]),
        app_config.persona,
        app_config.brand,
    )
    c = Content(
        id=cid,
        status=status,
        generated=gen,
        relevance_confidence=0.9,
        created_at=repo.clock.now(),
    )
    repo.insert_content(c)
    return c


def test_source_items_dedupe_by_url_hash(repo: Repository) -> None:
    items = load_source_items()
    assert repo.insert_source_item(items[0]) is True
    dup = items[0].model_copy(update={"id": "other-id"})
    assert repo.insert_source_item(dup) is False
    assert repo.has_url_hash(items[0].url_hash)


def test_unused_relevant_items_and_mark_used(repo: Repository) -> None:
    items = load_source_items()
    for it in items:
        repo.insert_source_item(it)
    assert len(repo.unclassified_items()) == len(items)
    repo.set_source_relevance(items[0].id, 0.95)
    repo.set_source_relevance(items[1].id, 0.5)
    repo.set_source_topic(items[0].id, "핸드드립", 0.9)
    got = repo.unused_relevant_items(0.8)
    assert [i.id for i, _, _, _ in got] == [items[0].id]
    assert got[0][2] == "핸드드립"
    repo.mark_items_used([items[0].id], "content-1")
    assert repo.unused_relevant_items(0.8) == []
    assert repo.existing_source_ids([items[0].id, "ghost"]) == {items[0].id}


def test_content_roundtrip_and_transitions(repo: Repository, app_config) -> None:
    c = _content(repo, app_config)
    assert repo.get_content(c.id) == c
    repo.transition_content(c.id, "pending_review", reason="보호 모드")
    repo.transition_content(c.id, "approved", approved_by="human")
    assert repo.count_human_approved() == 1
    repo.transition_content(c.id, "published")
    with pytest.raises(InvalidTransitionError):
        repo.transition_content(c.id, "approved")
    got = repo.get_content(c.id)
    assert got.status == "published" and got.review_reason == "보호 모드"


def test_publication_lifecycle(repo: Repository, app_config, clock) -> None:
    c = _content(repo, app_config, status="approved")
    ids = repo.create_publications(c.id, ["threads", "instagram"])
    assert len(ids) == 2
    assert repo.create_publications(c.id, ["threads"]) == []  # 중복 생성 없음
    due = repo.due_publications("threads", clock.now())
    assert len(due) == 1
    pid = due[0].id
    repo.transition_publication(pid, "publishing", increment_attempts=True)
    retry_at = clock.now() + timedelta(minutes=15)
    repo.transition_publication(pid, "failed", error="timeout", next_attempt_at=retry_at)
    assert repo.due_publications("threads", clock.now()) == []
    assert len(repo.due_publications("threads", retry_at)) == 1
    repo.transition_publication(pid, "publishing", increment_attempts=True)
    repo.transition_publication(pid, "published", external_id="t-1", url="https://x/t-1")
    p = repo.get_publication(pid)
    assert p.status == "published" and p.attempts == 2 and p.last_error is None
    start = clock.now() - timedelta(hours=1)
    assert repo.count_published_between("threads", start, clock.now() + timedelta(hours=1)) == 1
    assert repo.content_for_post("threads", "t-1").id == c.id
    with pytest.raises(InvalidTransitionError):
        repo.transition_publication(pid, "publishing")


def test_due_publications_ignores_unapproved(repo: Repository, app_config, clock) -> None:
    c = _content(repo, app_config, status="pending_review")
    repo.create_publications(c.id, ["threads"])
    assert repo.due_publications("threads", clock.now()) == []


def test_comments_dedupe_and_update(repo: Repository) -> None:
    comment, _ = load_comment_cases()[0]
    assert repo.insert_comment(comment) is True
    assert repo.insert_comment(comment) is False
    key = repo.comment_key(comment)
    repo.update_comment(
        key,
        status="auto_replied",
        category="praise_general",
        confidence=0.9,
        action="auto_reply",
        reply_text="고마워요",
        handled_by="policy",
    )
    s = repo.get_comment(key)
    assert s.status == "auto_replied" and s.reply_text == "고마워요" and s.handled_at is not None
    assert len(repo.list_comments("auto_replied")) == 1


def test_reply_counts(repo: Repository) -> None:
    d = date(2026, 10, 11)
    assert repo.reply_count("threads", "u1", d) == 0
    repo.increment_reply_count("threads", "u1", d)
    repo.increment_reply_count("threads", "u1", d)
    assert repo.reply_count("threads", "u1", d) == 2
    assert repo.reply_count("threads", "u1", d + timedelta(days=1)) == 0


def test_costs_settings_metrics_runs(repo: Repository, clock) -> None:
    repo.record_cost("llm", "anthropic", 0.12, "c1")
    repo.record_cost("image", "openai", 0.05)
    start = clock.now() - timedelta(hours=1)
    assert repo.cost_between(start, clock.now() + timedelta(hours=1)) == pytest.approx(0.17)
    repo.set_setting("auto_publish_enabled", True)
    assert repo.get_setting("auto_publish_enabled") is True
    repo.set_setting("auto_publish_enabled", False)
    assert repo.get_setting("auto_publish_enabled") is False
    assert repo.get_setting("missing", 7) == 7
    repo.insert_post_metrics(
        PostMetrics(platform="threads", post_external_id="t-1", captured_at=clock.now(), likes=3)
    )
    repo.insert_account_metrics(
        AccountMetrics(platform="threads", captured_at=clock.now(), followers=10)
    )
    rid = repo.start_run("collect")
    repo.finish_run(rid, True, "ok")
    assert repo.recent_runs()[0]["ok"] is True


def test_recent_topics(repo: Repository, app_config, clock) -> None:
    _content(repo, app_config)
    assert repo.recent_topics(clock.now() - timedelta(days=14)) == {"핸드드립"}
    assert repo.recent_topics(clock.now() + timedelta(days=1)) == set()


def test_failed_requires_retry_time_and_partial_publish_keeps_content_approved(
    repo: Repository, app_config, clock
) -> None:
    c = _content(repo, app_config, status="approved")
    repo.create_publications(c.id, ["threads", "instagram"])
    t, i = sorted(repo.publications_for_content(c.id), key=lambda p: p.platform != "threads")
    repo.transition_publication(t.id, "publishing")
    with pytest.raises(ValueError):
        repo.transition_publication(t.id, "failed", error="x")
    repo.transition_publication(t.id, "published", external_id="t", url="u")
    assert repo.mark_content_published_if_complete(c.id) is False
    assert repo.get_content(c.id).status == "approved"
    # 남은 플랫폼은 여전히 게시 대상
    assert [p.id for p in repo.due_publications("instagram", clock.now())] == [i.id]
    repo.transition_publication(i.id, "publishing")
    repo.transition_publication(i.id, "published", external_id="i", url="u2")
    assert repo.mark_content_published_if_complete(c.id) is True
    assert repo.get_content(c.id).status == "published"


def test_guarded_transition_rejects_stale_state(repo: Repository, app_config) -> None:
    c = _content(repo, app_config, status="approved")
    pid = repo.create_publications(c.id, ["threads"])[0]
    repo.transition_publication(pid, "publishing")
    # 두 번째 호출자는 이미 publishing인 것을 보고 실패해야 한다(이중 게시 방지)
    with pytest.raises(InvalidTransitionError):
        repo.transition_publication(pid, "publishing")
    with pytest.raises(KeyError):
        repo.transition_publication("ghost", "publishing")
