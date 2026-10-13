from __future__ import annotations

from pathlib import Path

import pytest

from nsm.domain.models import Classification
from nsm.engine.collect import run_collect
from nsm.engine.context import JobContext
from nsm.engine.generate import AUTO_PUBLISH_KEY, run_generate
from nsm.store.repo import Repository
from tests.fakes import (
    FakeClassifier,
    FakeNotifier,
    FakeSource,
    FakeTextGenerator,
    load_source_items,
)


class FixedImage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.prompts: list[str] = []

    def generate_background(self, prompt: str, width: int, height: int):
        self.prompts.append(prompt)
        return self.path


@pytest.fixture
def ctx(db, clock, app_config) -> JobContext:
    items = load_source_items()
    c = JobContext(
        config=app_config,
        repo=Repository(db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        sources=[FakeSource("naver", items)],
        relevance_classifier=FakeClassifier(
            default=Classification(label="relevant", confidence=0.9)
        ),
        topic_classifier=FakeClassifier(default=Classification(label="핸드드립", confidence=0.8)),
        classifier_name="fake",
        text_generator=FakeTextGenerator(),
    )
    run_collect(c)
    return c


def _enable_auto(ctx: JobContext, approvals: int = 20) -> None:
    ctx.repo.set_setting(AUTO_PUBLISH_KEY, True)
    ctx.config = ctx.config.model_copy(
        update={"settings": ctx.config.settings.model_copy(update={"protect_count": approvals})}
    )


def test_protect_mode_sends_everything_to_review(ctx: JobContext) -> None:
    r = run_generate(ctx)
    assert r.counts["created"] == 2  # 자료 4건: 첫 콘텐츠가 3건, 둘째가 1건 사용
    pending = ctx.repo.list_contents("pending_review")
    assert pending and "자동 게시가 꺼져 있음" in pending[0].review_reason
    assert ctx.notifier.calls[-1][0] == "검토 대기 콘텐츠"


def test_auto_publish_after_protect_mode(ctx: JobContext) -> None:
    _enable_auto(ctx, approvals=0)
    run_generate(ctx)
    approved = ctx.repo.list_contents("approved")
    assert len(approved) >= 1
    pubs = ctx.repo.publications_for_content(approved[-1].id)
    assert {p.platform for p in pubs} == {"instagram", "threads"}
    assert all(p.status == "queued" for p in pubs)


def test_second_content_of_day_goes_only_to_threads(ctx: JobContext) -> None:
    _enable_auto(ctx, approvals=0)
    r = run_generate(ctx)
    assert r.counts["created"] == 2
    second = ctx.repo.list_contents()[0]  # 최신
    assert [p.platform for p in ctx.repo.publications_for_content(second.id)] == ["threads"]
    # 하루 목표(2건)를 채우면 더 만들지 않는다
    assert run_generate(ctx).skipped == "오늘 생성 목표 달성"


def test_missing_source_and_banned_word_go_to_review(ctx: JobContext) -> None:
    _enable_auto(ctx, approvals=0)
    ctx.text_generator = FakeTextGenerator(missing_source=True, inject_text="무조건 최고")
    run_generate(ctx)
    c = ctx.repo.list_contents("pending_review")[0]
    assert "출처가 확인되지 않는 주장" in c.review_reason
    assert "금칙어" in c.review_reason


def test_sources_marked_used_and_templates_rotate(ctx: JobContext) -> None:
    _enable_auto(ctx, approvals=0)
    r = run_generate(ctx)
    assert r.counts["templates"] == ["info", "list"]
    assert ctx.repo.unused_relevant_items(0.8) == []


def test_cost_cap_stops_generation(ctx: JobContext) -> None:
    ctx.repo.record_cost("llm", "anthropic", 5.0)
    r = run_generate(ctx)
    assert r.skipped == "비용 상한"
    assert ctx.notifier.calls[-1][0] == "비용 상한 도달"
    assert ctx.repo.count_contents() == 0


def test_no_material_and_no_generator(ctx: JobContext, db, clock, app_config) -> None:
    empty = JobContext(
        config=app_config,
        repo=Repository(db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        text_generator=FakeTextGenerator(),
    )
    db.execute("UPDATE source_items SET used_in_content_id = 'x'")
    assert run_generate(empty).skipped.startswith("쓸 자료 없음")
    empty.text_generator = None
    assert "Claude API 키" in run_generate(empty).skipped


def test_generator_failure_is_reported(ctx: JobContext) -> None:
    class Boom(FakeTextGenerator):
        def generate_content(self, brief, persona, brand):
            raise RuntimeError("api down")

    ctx.text_generator = Boom()
    r = run_generate(ctx)
    assert not r.ok and "api down" in r.errors[0]
    assert ctx.repo.count_contents() == 0
    assert len(ctx.repo.unused_relevant_items(0.8)) == 4  # 자료는 다음 기회에 다시 쓴다


def test_background_image_applied(ctx: JobContext, tmp_path: Path) -> None:
    bg = tmp_path / "bg.png"
    bg.write_bytes(b"\x89PNG")
    ctx.image_generator = FixedImage(bg)
    run_generate(ctx)
    c = ctx.repo.list_contents()[-1]
    assert c.generated.deck.slides[0].background == str(bg)
    assert c.generated.deck.labels.ai_generated_image is True


def test_topic_rotation_prefers_least_recent(ctx: JobContext) -> None:
    # 두 주제의 자료가 있을 때, 이미 쓴 주제는 뒤로
    items = load_source_items()
    ctx.repo.set_source_topic(items[2].id, "원두", 0.9)
    _enable_auto(ctx, approvals=0)
    run_generate(ctx)
    topics = [
        r[0]
        for r in ctx.repo.db.execute(
            "SELECT persona_topic FROM contents ORDER BY created_at, rowid"
        )
    ]
    assert set(topics) == {"핸드드립", "원두"}


def test_claim_citing_source_outside_brief_goes_to_review(ctx: JobContext) -> None:
    _enable_auto(ctx, approvals=0)
    other = load_source_items()[3]  # 이번 브리프에 없을 수 있는 자료

    class CitesOther(FakeTextGenerator):
        def generate_content(self, brief, persona, brand):
            g = super().generate_content(brief, persona, brand)
            from nsm.domain.models import Claim

            ghost = next(
                i for i in load_source_items() if i.id not in {s.id for s in brief.sources}
            )
            return g.model_copy(update={"claims": [Claim(text="x", source_item_id=ghost.id)]})

    ctx.text_generator = CitesOther()
    run_generate(ctx)
    c = ctx.repo.list_contents()[-1]
    assert c.status == "pending_review" and "출처가 확인되지 않는" in c.review_reason
    assert other  # 자료 존재 확인


def test_refusal_skips_material_and_continues(ctx: JobContext) -> None:
    from nsm.ports import AdapterError

    class RefuseFirst(FakeTextGenerator):
        def __init__(self):
            super().__init__()
            self.n = 0

        def generate_content(self, brief, persona, brand):
            self.n += 1
            if self.n == 1:
                raise AdapterError("refusal")
            return super().generate_content(brief, persona, brand)

    _enable_auto(ctx, approvals=0)
    ctx.text_generator = RefuseFirst()
    r = run_generate(ctx)
    assert r.counts["created"] == 1  # 첫 묶음은 버리고 남은 자료로 하나
    assert ctx.repo.unused_relevant_items(0.8) == []


def test_save_is_atomic(ctx: JobContext, monkeypatch) -> None:
    _enable_auto(ctx, approvals=0)

    def boom(*a, **k):
        raise RuntimeError("disk full")

    real = ctx.repo.db.transaction

    from contextlib import contextmanager

    @contextmanager
    def failing():
        with real() as cur:
            yield cur
            boom()

    monkeypatch.setattr(ctx.repo.db, "transaction", failing)
    with pytest.raises(RuntimeError):  # runner가 잡아 실행 로그·알림으로 남긴다
        run_generate(ctx)
    assert ctx.repo.count_contents() == 0
    assert len(ctx.repo.unused_relevant_items(0.8)) == 4


def test_manual_run_makes_one_more_after_daily_target(ctx: JobContext) -> None:
    from nsm.engine.runner import run_job

    run_generate(ctx)  # 자동: 하루 목표 2건
    assert run_generate(ctx).skipped == "오늘 생성 목표 달성"
    # 새 자료가 들어온 뒤(쓸 자료가 있어야 만든다)
    fresh = [
        i.model_copy(update={"id": f"{i.id}-b", "url": f"{i.url}?b", "url_hash": f"{i.url_hash}b"})
        for i in load_source_items()
    ]
    ctx.sources = [FakeSource("naver", fresh)]
    run_collect(ctx)
    r = run_job("generate", ctx, manual=True)  # 사람이 "지금 실행": 1건 더
    assert r.counts["created"] == 1 and r.skipped == "", r.summary()
    extra = ctx.repo.list_contents()[0]
    # 쿼터를 넘긴 원고도 모든 플랫폼에 배정(쿼터가 남는 날 올라감)
    pubs = {p.platform for p in ctx.repo.publications_for_content(extra.id)}
    assert pubs == set(ctx.config.settings.enabled_platforms)
    assert run_job("generate", ctx).skipped == "오늘 생성 목표 달성"  # 자동은 그대로


def test_network_errors_are_explained_in_korean() -> None:
    import anthropic
    import httpx

    from nsm.engine.generate import network_error_text

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    assert "연결이 끊겼습니다" in network_error_text(anthropic.APITimeoutError(request=req))
    assert "연결이 끊겼습니다" in network_error_text(anthropic.APIConnectionError(request=req))
    busy = anthropic.RateLimitError("rate", response=httpx.Response(429, request=req), body=None)
    assert "바쁩니다" in network_error_text(busy)
    assert network_error_text(ValueError("x")) == "ValueError: x"


def test_network_failure_keeps_material_for_next_run(ctx: JobContext) -> None:
    import anthropic
    import httpx

    class Down(FakeTextGenerator):
        def generate_content(self, *a, **k):
            raise anthropic.APITimeoutError(request=httpx.Request("POST", "https://x"))

    before = len(ctx.repo.unused_relevant_items(0.0))
    ctx.text_generator = Down()
    r = run_generate(ctx)
    assert r.counts["created"] == 0 and "연결이 끊겼습니다" in r.errors[0]
    assert len(ctx.repo.unused_relevant_items(0.0)) == before  # 자료는 그대로
