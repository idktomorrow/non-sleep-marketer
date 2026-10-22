from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from nsm.domain.models import Classification
from nsm.engine.collect import run_collect
from nsm.engine.context import JobContext
from nsm.engine.runner import run_job
from nsm.store.repo import Repository
from tests.fakes import FakeClassifier, FakeNotifier, FakeSource, load_source_items

KST = ZoneInfo("Asia/Seoul")


class BrokenSource:
    name = "broken"

    def collect(self, topics, limit):
        raise RuntimeError("site changed")


@pytest.fixture
def ctx(db, clock, app_config) -> JobContext:
    items = load_source_items()
    stock = next(i for i in items if "코스피" in i.title)
    relevance = FakeClassifier(
        by_text={
            f"{stock.title}\n{stock.text}": Classification(label="irrelevant", confidence=0.95)
        },
        default=Classification(label="relevant", confidence=0.9),
    )
    topic = FakeClassifier(default=Classification(label="핸드드립", confidence=0.8))
    return JobContext(
        config=app_config,
        repo=Repository(db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        sources=[FakeSource("naver", items), BrokenSource()],
        relevance_classifier=relevance,
        topic_classifier=topic,
        comment_classifier=FakeClassifier(),
        classifier_name="fake",
    )


def test_collect_stores_classifies_and_isolates_failures(ctx: JobContext) -> None:
    r = run_collect(ctx)
    assert (
        r.counts
        == {
            "new": 4,
            "duplicate": 0,
            "classified": 4,
            "relevant": 3,
            "bodies": 0,
            "facts_judged": 2,  # 원문 읽기 없는 소스는 요약문으로 판정, 이번 실행에 없는 소스(trends)는 보류(C31)
        }
    )
    assert any("broken" in e for e in r.errors)
    assert r.ok
    usable = ctx.repo.unused_relevant_items(0.8)
    assert len(usable) == 3
    assert all(topic == "핸드드립" for _, _, topic, _ in usable)
    assert all("코스피" not in item.title for item, _, _, _ in usable)


def test_collect_second_run_dedupes_and_does_not_reclassify(ctx: JobContext) -> None:
    run_collect(ctx)
    calls = len(ctx.relevance_classifier.calls)
    r = run_collect(ctx)
    assert r.counts["new"] == 0 and r.counts["duplicate"] == 4
    assert len(ctx.relevance_classifier.calls) == calls


def test_collect_skips_outside_active_hours(ctx: JobContext) -> None:
    ctx.clock.set(datetime(2026, 10, 11, 14, 0, tzinfo=KST))  # 12~18시는 활동 시간 밖
    r = run_collect(ctx)
    assert r.skipped == "활동 시간 밖"
    assert ctx.sources[0].calls == []


def test_collect_skips_when_persona_disabled(ctx: JobContext) -> None:
    ctx.config = ctx.config.model_copy(
        update={"persona": ctx.config.persona.model_copy(update={"enabled": False})}
    )
    assert run_collect(ctx).skipped == "페르소나가 꺼져 있음"


def test_overnight_window(ctx: JobContext) -> None:
    from nsm.config.store import parse_windows

    ctx.config = ctx.config.model_copy(
        update={
            "persona": ctx.config.persona.model_copy(
                update={"active_hours": parse_windows("22:00-02:00")}
            )
        }
    )
    ctx.clock.set(datetime(2026, 10, 12, 1, 0, tzinfo=KST))
    assert not run_collect(ctx).skipped


def test_runner_logs_runs_and_notifies_on_crash(ctx: JobContext) -> None:
    r = run_job("collect", ctx)
    assert r.ok
    runs = ctx.repo.recent_runs()
    assert runs[0]["job"] == "collect" and runs[0]["ok"] is True

    def boom(_ctx):
        raise RuntimeError("db gone")

    from nsm.engine import runner

    runner.JOBS["boom"] = boom
    try:
        r2 = run_job("boom", ctx)
    finally:
        del runner.JOBS["boom"]
    assert not r2.ok
    assert ctx.notifier.calls[-1][2] == "error"
    with pytest.raises(KeyError):
        run_job("nope", ctx)


class FlakyClassifier(FakeClassifier):
    """일괄 호출은 실패하고, 특정 텍스트는 한 건 호출도 실패한다."""

    def __init__(self, bad: str, **kw) -> None:
        super().__init__(**kw)
        self.bad = bad

    def classify_many(self, texts, labels, instruction):
        raise RuntimeError("batch failed")

    def classify(self, text, labels, instruction):
        if self.bad in text:
            raise RuntimeError("refused")
        return super().classify(text, labels, instruction)


def test_collect_isolates_single_item_failures(ctx: JobContext) -> None:
    ctx.relevance_classifier = FlakyClassifier(
        "코스피", default=Classification(label="relevant", confidence=0.9)
    )
    r = run_collect(ctx)
    assert r.counts["classified"] == 4 and r.counts["relevant"] == 3
    # 실패한 항목은 닫혀서 다음 실행에 다시 비용을 쓰지 않는다
    assert ctx.repo.unclassified_items() == []


def test_collect_retries_missing_topic_next_run(ctx: JobContext) -> None:
    class Down(FakeClassifier):
        def classify(self, text, labels, instruction):
            raise RuntimeError("api down")

    good_topic = ctx.topic_classifier
    ctx.topic_classifier = Down()
    run_collect(ctx)
    assert len(ctx.repo.relevant_items_without_topic(0.8)) == 3
    ctx.topic_classifier = good_topic
    run_collect(ctx)
    assert ctx.repo.relevant_items_without_topic(0.8) == []
