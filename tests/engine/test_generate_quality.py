"""배치 Q1 생성·게시 품질: 규칙 위반 재생성(4·7), 카드 번호(6), SNS 출처 줄(3), 프롬프트."""

from __future__ import annotations

import pytest

from nsm.adapters.generator.claude_text import _system
from nsm.domain.models import Classification
from nsm.engine.collect import run_collect
from nsm.engine.context import JobContext
from nsm.engine.generate import AUTO_PUBLISH_KEY, run_generate
from nsm.engine.publish import build_post
from nsm.store.repo import Repository
from tests.fakes import (
    FakeClassifier,
    FakeNotifier,
    FakeSource,
    FakeTextGenerator,
    load_source_items,
)

BAD_CAPTION = "✔ 첫 번째 기준\n✔ 그다음은\n쉬운 홈카페 ☕"  # 2026-10-12 실제 캡션의 이모지 3개
BAD_THREADS = "자료에 따르면 분쇄 방식이 중요해요."


class ScriptedGenerator(FakeTextGenerator):
    """n번째 호출마다 정해진 캡션·스레드 글·카드 목록을 넣는다."""

    def __init__(self, script: list[tuple[str, str]], numbered: bool = False) -> None:
        super().__init__()
        self.script = script
        self.numbered = numbered
        self.calls = 0

    def generate_content(self, brief, persona, brand):
        gen = super().generate_content(brief, persona, brand)
        caption, threads = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if self.numbered:
            slides = [
                s.model_copy(update={"bullets": [f"{i}. {b}" for i, b in enumerate(s.bullets, 1)]})
                if s.kind in ("body", "list") and s.bullets
                else s
                for s in gen.deck.slides
            ]
            gen = gen.model_copy(update={"deck": gen.deck.model_copy(update={"slides": slides})})
        return gen.model_copy(update={"instagram_caption": caption, "threads_text": threads})


def _ctx(db, clock, app_config, generator) -> JobContext:
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
        text_generator=generator,
    )
    run_collect(c)
    c.repo.set_setting(AUTO_PUBLISH_KEY, True)
    c.config = c.config.model_copy(
        update={"settings": c.config.settings.model_copy(update={"protect_count": 0})}
    )
    return c


def _first(ctx: JobContext):
    return sorted(ctx.repo.list_contents(), key=lambda c: c.created_at)[0]


def test_violation_is_regenerated_once_and_fixed(db, clock, app_config) -> None:
    gen = ScriptedGenerator([(BAD_CAPTION, BAD_THREADS), ("깔끔한 캡션 ☕", "짧은 글이에요")])
    ctx = _ctx(db, clock, app_config, gen)
    r = run_generate(ctx)
    c = _first(ctx)
    assert r.counts["regenerated"] == 1
    assert c.generated.instagram_caption == "깔끔한 캡션 ☕"
    assert c.status == "approved"  # 다시 만든 것이 규칙을 지키면 평소처럼 자동 게시 판정


def test_still_violating_goes_to_review_with_reason(db, clock, app_config) -> None:
    gen = ScriptedGenerator([(BAD_CAPTION, BAD_THREADS)])
    ctx = _ctx(db, clock, app_config, gen)
    run_generate(ctx)
    c = _first(ctx)
    assert gen.calls >= 2  # 한 번 다시 만들었다
    assert c.status == "pending_review"
    assert "페르소나 규칙 위반" in c.review_reason and "이모지 3개" in c.review_reason
    assert "자료에 따르면" in c.review_reason


def test_numbered_bullets_are_cleaned_in_the_job(db, clock, app_config) -> None:
    gen = ScriptedGenerator([("캡션", "스레드")], numbered=True)
    ctx = _ctx(db, clock, app_config, gen)
    run_generate(ctx)
    for c in ctx.repo.list_contents():
        for s in c.generated.deck.slides:
            for b in getattr(s, "bullets", []) or []:
                assert not b[:2].rstrip(".").isdigit(), b


@pytest.mark.parametrize("platform", ["instagram", "threads"])
def test_sns_post_ends_with_sources_line(db, clock, app_config, platform) -> None:
    ctx = _ctx(db, clock, app_config, ScriptedGenerator([("캡션 본문", "스레드 본문")]))
    run_generate(ctx)
    post = build_post(_first(ctx), platform, [])
    last = post.text.splitlines()[-1]
    assert last.startswith("출처: ") and 1 <= len(last.removeprefix("출처: ").split(", ")) <= 2


def test_prompt_states_rules_checked_in_code(app_config) -> None:
    text = _system(app_config.persona, app_config.brand)
    assert "이모지는 캡션·스레드 글·카드 한 장마다 각각 1개까지" in text  # 픽스처 말투에서 읽음
    assert "번호나 기호" in text and "자료에 따르면" in text and "출처는 시스템이" in text
