"""경계 1 시나리오: 대댓글 (S4 댓글 시나리오, #12)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from nsm.config.store import parse_windows
from nsm.domain.models import Classification, Comment
from nsm.engine.context import JobContext
from nsm.engine.publish import PAUSED_KEY
from nsm.engine.reply import run_reply
from nsm.store.repo import Repository
from tests.fakes import (
    FakeClassifier,
    FakeNotifier,
    FakePlatformAdapter,
    FakeTextGenerator,
    load_comment_cases,
)

KST = ZoneInfo("Asia/Seoul")


@pytest.fixture
def ctx(db, clock, app_config, tmp_path) -> JobContext:
    cases = load_comment_cases()
    by_text = {c.text: Classification(label=cat, confidence=0.9) for c, cat in cases}
    cfg = app_config.model_copy(
        update={
            "persona": app_config.persona.model_copy(
                update={"active_hours": parse_windows("00:00-23:59")}
            )
        }
    )
    c = JobContext(
        config=cfg,
        repo=Repository(db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        comment_classifier=FakeClassifier(by_text=by_text),
        text_generator=FakeTextGenerator(),
        adapters={
            p: FakePlatformAdapter(p, clock=clock) for p in ("threads", "instagram", "naver_blog")
        },
        media_dir=tmp_path,
    )
    # 게시된 글 하나씩 만들어 둔다(댓글은 내 게시물에만)
    from nsm.domain.models import Content, ContentBrief
    from tests.fakes import load_source_items

    gen = FakeTextGenerator().generate_content(
        ContentBrief(topic="핸드드립", template="info", sources=load_source_items()[:1]),
        cfg.persona,
        cfg.brand,
    )
    content = Content(
        id="c1", status="approved", generated=gen, relevance_confidence=0.9, created_at=clock.now()
    )
    c.repo.insert_content(content)
    for p, ext in (("threads", "p1"), ("instagram", "p2"), ("naver_blog", "p3")):
        pid = c.repo.create_publications("c1", [p])[0]
        c.repo.transition_publication(pid, "publishing")
        c.repo.transition_publication(pid, "published", external_id=ext, url=f"https://x/{ext}")
    for comment, _ in cases:
        c.adapters[comment.platform].comments.setdefault(comment.post_external_id, []).append(
            comment.model_copy(update={"created_at": clock.now() - timedelta(minutes=5)})
        )
    return c


def _status(ctx: JobContext, platform: str, ext: str) -> str:
    return ctx.repo.get_comment(f"{platform}:{ext}").status


def test_six_categories_handled(ctx: JobContext) -> None:
    r = run_reply(ctx)
    assert r.counts["new"] == 6
    assert _status(ctx, "threads", "c1") == "auto_replied"  # 칭찬
    assert _status(ctx, "threads", "c2") == "auto_replied"  # 질문
    assert _status(ctx, "instagram", "c3") == "escalated"  # 불만
    assert _status(ctx, "instagram", "c4") == "escalated"  # 환불
    assert _status(ctx, "naver_blog", "c5") == "escalated"  # 법적
    assert _status(ctx, "threads", "c6") == "hidden"  # 스팸
    assert len(ctx.adapters["threads"].replied) == 2
    assert ctx.adapters["threads"].hidden[0].external_id == "c6"
    assert ctx.notifier.calls[-1][0] == "댓글 확인 필요"


def test_same_comment_not_processed_twice(ctx: JobContext) -> None:
    run_reply(ctx)
    r = run_reply(ctx)
    assert r.counts["new"] == 0
    assert len(ctx.adapters["threads"].replied) == 2


def test_fourth_reply_to_same_author_blocked(ctx: JobContext) -> None:
    ad = ctx.adapters["threads"]
    ad.comments = {
        "p1": [
            Comment(
                platform="threads",
                external_id=f"x{i}",
                post_external_id="p1",
                author_id="fan",
                author_name="팬",
                text="정리 너무 좋아요! 저장했어요",
                created_at=ctx.clock.now() - timedelta(minutes=10 - i),
            )
            for i in range(4)
        ]
    }
    run_reply(ctx)
    assert len(ad.replied) == 3
    assert _status(ctx, "threads", "x3") == "ignored"


def test_banned_word_in_reply_is_not_sent(ctx: JobContext) -> None:
    ctx.text_generator = FakeTextGenerator(reply_text="우리 원두가 무조건 최고예요")
    run_reply(ctx)
    assert ctx.adapters["threads"].replied == []
    s = ctx.repo.get_comment("threads:c1")
    assert s.status == "escalated" and "금칙어" in s.reason and s.reply_text


def test_spam_escalated_when_hide_unsupported(ctx: JobContext) -> None:
    ctx.adapters["threads"] = FakePlatformAdapter(
        "threads", capabilities={"fetch_comments", "reply", "publish"}, clock=ctx.clock
    )
    ctx.adapters["threads"].comments = {
        "p1": [c for c, cat in load_comment_cases() if cat == "spam"]
    }
    run_reply(ctx)
    s = ctx.repo.get_comment("threads:c6")
    assert s.status == "escalated" and "스팸 추정" in s.reason


def test_low_confidence_goes_to_human(ctx: JobContext) -> None:
    ctx.comment_classifier = FakeClassifier(
        default=Classification(label="praise_general", confidence=0.4)
    )
    run_reply(ctx)
    s = ctx.repo.get_comment("threads:c1")
    assert s.status == "escalated" and "불확실" in s.reason


def test_no_generator_escalates_instead_of_replying(ctx: JobContext) -> None:
    ctx.text_generator = None
    run_reply(ctx)
    assert _status(ctx, "threads", "c1") == "escalated"


def test_skips_outside_hours_and_paused(ctx: JobContext) -> None:
    ctx.config = ctx.config.model_copy(
        update={
            "persona": ctx.config.persona.model_copy(
                update={"active_hours": parse_windows("09:00-12:00")}
            )
        }
    )
    ctx.clock.set(datetime(2026, 10, 11, 14, 0, tzinfo=KST))
    assert run_reply(ctx).skipped == "활동 시간 밖"
    ctx.clock.set(datetime(2026, 10, 11, 10, 0, tzinfo=KST))
    ctx.repo.set_setting(PAUSED_KEY, ["threads", "instagram", "naver_blog"])
    assert run_reply(ctx).counts["new"] == 0


def test_one_bad_comment_does_not_block_others(ctx: JobContext) -> None:
    class Flaky(FakeTextGenerator):
        def generate_reply(self, comment, persona, post_context):
            if comment.external_id == "c1":
                raise RuntimeError("api down")
            return super().generate_reply(comment, persona, post_context)

    ctx.text_generator = Flaky()
    r = run_reply(ctx)
    assert _status(ctx, "threads", "c1") == "escalated"
    assert _status(ctx, "threads", "c2") == "auto_replied"
    assert r.errors


def test_leftover_new_comments_are_processed(ctx: JobContext) -> None:
    # 지난 실행이 댓글을 저장만 하고 처리 전에 죽었다고 가정
    comment, _ = load_comment_cases()[0]
    ctx.repo.insert_comment(comment)
    ctx.adapters["threads"].comments = {}
    run_reply(ctx)
    assert ctx.repo.get_comment("threads:c1").status == "auto_replied"


def test_fetch_failure_marks_run_not_ok(ctx: JobContext) -> None:
    def boom(ids, since):
        raise RuntimeError("session expired")

    ctx.adapters["instagram"].fetch_new_comments = boom
    r = run_reply(ctx)
    assert not r.ok and any("instagram" in e for e in r.errors)


class _Capture(FakeTextGenerator):
    def __init__(self) -> None:
        super().__init__()
        self.contexts: list[str] = []

    def generate_reply(self, comment, persona, post_context):
        self.contexts.append(post_context)
        return super().generate_reply(comment, persona, post_context)


def test_reply_sees_sources_used_for_the_post(ctx: JobContext) -> None:
    """C18: 답글은 게시물 본문뿐 아니라 원고에 쓴 자료(원문)까지 보고 쓴다."""
    from tests.fakes import load_source_items

    item = load_source_items()[0].model_copy(
        update={"text": "한국 팝업은 11월 3일 성수동에서 열린다. " + "자세한 내용 " * 400}
    )
    ctx.repo.insert_source_item(item)
    ctx.repo.mark_items_used([item.id], "c1")
    gen = _Capture()
    ctx.text_generator = gen
    run_reply(ctx)
    assert gen.contexts
    text = gen.contexts[0]
    assert "게시물 주제: 핸드드립" in text and "게시물 본문:" in text
    assert "[자료 1]" in text and item.title in text and "11월 3일 성수동" in text
    assert len(text) < 2500  # 자료는 앞부분만(1500자)


def test_reply_without_sources_still_has_post(ctx: JobContext) -> None:
    gen = _Capture()
    ctx.text_generator = gen
    run_reply(ctx)
    assert "게시물 주제: 핸드드립" in gen.contexts[0] and "[자료" not in gen.contexts[0]
