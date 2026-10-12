"""경계 1 시나리오: 게시 (S4 #20). 가짜 어댑터 + 메모리 DB + 고정 시계."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from nsm.config.store import parse_windows
from nsm.domain.labels import AD_HASHTAG, BLOG_TITLE_SUFFIX, VIRTUAL_PERSON_TEXT
from nsm.domain.models import Classification
from nsm.engine.collect import run_collect
from nsm.engine.context import JobContext
from nsm.engine.generate import AUTO_PUBLISH_KEY, run_generate
from nsm.engine.publish import PAUSED_KEY, recover_interrupted, run_publish
from nsm.store.repo import Repository
from tests.fakes import (
    FakeClassifier,
    FakeNotifier,
    FakePlatformAdapter,
    FakeRenderer,
    FakeSource,
    FakeTextGenerator,
    load_source_items,
)

KST = ZoneInfo("Asia/Seoul")
PLATFORMS = ("instagram", "threads", "naver_blog")


@pytest.fixture
def ctx(db, clock, app_config, tmp_path) -> JobContext:
    c = JobContext(
        config=app_config,
        repo=Repository(db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        sources=[FakeSource("naver", load_source_items())],
        relevance_classifier=FakeClassifier(
            default=Classification(label="relevant", confidence=0.9)
        ),
        topic_classifier=FakeClassifier(default=Classification(label="핸드드립", confidence=0.8)),
        classifier_name="fake",
        text_generator=FakeTextGenerator(),
        renderer=FakeRenderer(),
        adapters={p: FakePlatformAdapter(p, clock=clock) for p in PLATFORMS},
        media_dir=tmp_path / "media",
    )
    c.repo.set_setting(AUTO_PUBLISH_KEY, True)
    # 재시도·간격 시나리오가 몇 시간에 걸치므로 활동 시간은 종일로 둔다(활동 시간 규칙은 따로 검사)
    c.config = c.config.model_copy(
        update={
            "settings": c.config.settings.model_copy(update={"protect_count": 0}),
            "persona": c.config.persona.model_copy(
                update={"active_hours": parse_windows("00:00-23:59")}
            ),
        }
    )
    run_collect(c)
    run_generate(c)
    return c


def _published(ctx: JobContext, platform: str):
    return ctx.adapters[platform].published


def test_publishes_one_per_platform_with_labels(ctx: JobContext) -> None:
    r = run_publish(ctx)
    assert r.counts == {"published": 3, "failed": 0}
    for p in PLATFORMS:
        post = _published(ctx, p)[0]
        assert post.text.startswith(VIRTUAL_PERSON_TEXT)
        assert len(post.images) == 4  # info 덱: cover + 2 body + outro
    assert _published(ctx, "naver_blog")[0].title.endswith(BLOG_TITLE_SUFFIX)
    # 렌더러에 넘긴 덱에는 가상인물 표기가 켜져 있다
    assert all(d.labels.virtual_person for d in ctx.renderer.rendered)
    # 콘텐츠당 한 번만 렌더
    assert len(ctx.renderer.rendered) == 1


def test_ad_label_follows_setting(ctx: JobContext, db) -> None:
    db.execute("UPDATE contents SET is_ad = TRUE")
    run_publish(ctx)
    post = _published(ctx, "threads")[0]
    assert post.text.startswith(f"{AD_HASHTAG} {VIRTUAL_PERSON_TEXT}")
    assert ctx.renderer.rendered[0].labels.ad is True


def test_content_published_only_after_all_platforms(ctx: JobContext) -> None:
    ctx.adapters["instagram"].fail_next_publish = 1
    run_publish(ctx)
    first = ctx.repo.list_contents()[-1]
    assert first.status == "approved"
    ctx.clock.advance(minutes=16)
    run_publish(ctx)
    assert ctx.repo.get_content(first.id).status == "published"


def test_quota_and_min_gap(ctx: JobContext) -> None:
    run_publish(ctx)
    # 스레드는 쿼터 2지만 직전 게시와 간격이 짧으면 기다린다
    run_publish(ctx)
    assert len(_published(ctx, "threads")) == 1
    ctx.clock.advance(hours=ctx.config.settings.min_hours_between_posts)
    run_publish(ctx)
    assert len(_published(ctx, "threads")) == 2
    # 인스타는 쿼터 1이라 둘째 날까지 더 안 올라간다
    assert len(_published(ctx, "instagram")) == 1


def test_retry_schedule_then_abandon(ctx: JobContext) -> None:
    ad = ctx.adapters["threads"]
    ad.fail_next_publish = 99
    pub_id = ctx.repo.due_publications("threads", ctx.clock.now())[0].id
    for minutes in ctx.config.settings.retry_delays_minutes:
        run_publish(ctx)
        p = ctx.repo.get_publication(pub_id)
        assert p.status == "failed"
        assert p.next_attempt_at == ctx.clock.now() + timedelta(minutes=minutes)
        ctx.clock.advance(minutes=minutes)
    run_publish(ctx)
    p = ctx.repo.get_publication(pub_id)
    assert p.status == "abandoned" and p.attempts == 4
    content = ctx.repo.get_content(p.content_id)
    assert content.status == "pending_review" and "4회 실패" in content.review_reason
    assert ctx.notifier.calls[-1][0] == "게시 포기"


def test_no_publish_outside_hours_or_when_disabled(ctx: JobContext) -> None:
    ctx.config = ctx.config.model_copy(
        update={
            "persona": ctx.config.persona.model_copy(
                update={"active_hours": parse_windows("09:00-12:00")}
            )
        }
    )
    ctx.clock.set(datetime(2026, 10, 11, 14, 0, tzinfo=KST))
    assert run_publish(ctx).skipped == "활동 시간 밖"
    ctx.clock.set(datetime(2026, 10, 11, 10, 0, tzinfo=KST))
    ctx.config = ctx.config.model_copy(
        update={"persona": ctx.config.persona.model_copy(update={"enabled": False})}
    )
    assert run_publish(ctx).skipped == "페르소나가 꺼져 있음"
    assert all(not _published(ctx, p) for p in PLATFORMS)


def test_paused_platform_is_skipped(ctx: JobContext) -> None:
    ctx.repo.set_setting(PAUSED_KEY, ["instagram"])
    run_publish(ctx)
    assert not _published(ctx, "instagram")
    assert _published(ctx, "threads")


def test_pending_review_content_is_not_published(ctx: JobContext, db) -> None:
    db.execute("UPDATE contents SET status = 'pending_review'")
    assert run_publish(ctx).counts["published"] == 0


def test_restart_recovery_sends_interrupted_to_review(ctx: JobContext) -> None:
    pub = ctx.repo.due_publications("threads", ctx.clock.now())[0]
    ctx.repo.transition_publication(pub.id, "publishing", increment_attempts=True)
    # 프로세스가 여기서 죽었다고 가정 → 재시작
    assert recover_interrupted(ctx) == 1
    assert ctx.repo.get_publication(pub.id).status == "abandoned"
    c = ctx.repo.get_content(pub.content_id)
    assert c.status == "pending_review" and "중단" in c.review_reason
    run_publish(ctx)
    assert not _published(ctx, "threads") or all(
        p.content_id != pub.content_id for p in _published(ctx, "threads")
    )


def test_edited_deck_is_rerendered(ctx: JobContext, db) -> None:
    from nsm.engine.publish import render_images

    c = ctx.repo.list_contents()[-1]
    first = render_images(ctx, c)
    assert render_images(ctx, c) == first and len(ctx.renderer.rendered) == 1  # 캐시
    db.execute("UPDATE contents SET is_ad = TRUE WHERE id = ?", [c.id])
    render_images(ctx, ctx.repo.get_content(c.id))
    assert len(ctx.renderer.rendered) == 2 and ctx.renderer.rendered[-1].labels.ad is True


def test_reapproval_requeues_abandoned(ctx: JobContext) -> None:
    pub = ctx.repo.due_publications("threads", ctx.clock.now())[0]
    ctx.repo.transition_publication(pub.id, "publishing", increment_attempts=True)
    recover_interrupted(ctx)
    ctx.repo.transition_content(pub.content_id, "approved", approved_by="human")
    assert ctx.repo.requeue_abandoned(pub.content_id) == 1
    p = ctx.repo.get_publication(pub.id)
    assert p.status == "queued" and p.attempts == 0
    run_publish(ctx)
    for _ in range(3):
        ctx.clock.advance(hours=ctx.config.settings.min_hours_between_posts)
        run_publish(ctx)
    assert ctx.repo.get_content(pub.content_id).status == "published"


def test_failure_marks_run_not_ok_but_others_publish(ctx: JobContext) -> None:
    ctx.adapters["instagram"].fail_next_publish = 1
    r = run_publish(ctx)
    assert not r.ok and r.counts == {"published": 2, "failed": 1}


def test_no_adapters_is_an_error(ctx: JobContext) -> None:
    ctx.adapters = {}
    r = run_publish(ctx)
    assert not r.ok and "어댑터" in r.errors[0]


def test_long_threads_text_fits_limit(ctx: JobContext, db) -> None:
    from nsm.domain.policy import TEXT_LIMITS
    from nsm.engine.publish import build_post

    c = ctx.repo.list_contents()[-1]
    g = c.generated.model_copy(update={"threads_text": "가" * 600})
    post = build_post(c.model_copy(update={"generated": g}), "threads", [])
    assert len(post.text) == TEXT_LIMITS["threads"]
    assert post.text.startswith(VIRTUAL_PERSON_TEXT) and post.text.endswith("…")


def test_overflowing_card_goes_to_review_not_published(ctx: JobContext) -> None:
    class Overflowing(FakeRenderer):
        last_overflow = [2]

    ctx.renderer = Overflowing()
    r = run_publish(ctx)
    assert r.counts["published"] == 0
    c = ctx.repo.list_contents()[-1]
    assert c.status == "pending_review" and "넘침" in c.review_reason
    assert all(not _published(ctx, p) for p in PLATFORMS)
    assert ctx.notifier.calls[-1][0] == "카드 글 넘침"
