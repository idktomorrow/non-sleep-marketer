"""엔진 테스트 공용 픽스처: 댓글 잡 컨텍스트(test_reply_job·test_comment_triage가 함께 쓴다, C32)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from nsm.config.store import parse_windows
from nsm.domain.models import Classification, Content, ContentBrief
from nsm.engine.context import JobContext
from nsm.store.repo import Repository
from tests.fakes import (
    FakeClassifier,
    FakeNotifier,
    FakePlatformAdapter,
    FakeTextGenerator,
    load_comment_cases,
    load_source_items,
)


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
