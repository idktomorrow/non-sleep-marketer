"""글 공급자 선택(S13 제안): auto는 Claude → OpenAI 순, 명시하면 그 공급자만(키 없으면 None + 이유).
어댑터 묶음은 다음 작업부터 바뀌고, 진행 중 스냅샷은 그대로다."""

from __future__ import annotations

import pytest

from nsm.adapters.generator import (
    choose_text_provider,
    make_image_studio,
    make_text_generator_with_reason,
)
from nsm.config.secrets import MemorySecretStore
from nsm.engine.context import Adapters, JobContext
from tests.fakes import FakeCostSink, FakeNotifier, FakeTextGenerator


def _cfg(app_config, provider: str):
    return app_config.model_copy(
        update={"settings": app_config.settings.model_copy(update={"text_provider": provider})}
    )


@pytest.mark.parametrize(
    ("provider", "keys", "expected"),
    [
        ("auto", {"anthropic_api_key": "a", "openai_api_key": "o"}, "claude"),
        ("auto", {"openai_api_key": "o"}, "openai"),
        ("auto", {}, None),
        ("openai", {"anthropic_api_key": "a"}, None),  # 명시하면 다른 키가 있어도 바꾸지 않는다
        ("openai", {"openai_api_key": "o"}, "openai"),
        ("claude", {"openai_api_key": "o"}, None),
        ("claude", {"anthropic_api_key": "a"}, "claude"),
    ],
)
def test_choose_text_provider(app_config, provider, keys, expected) -> None:
    got, reason = choose_text_provider(_cfg(app_config, provider), MemorySecretStore(keys))
    assert got == expected
    assert (reason == "") == (expected is not None)
    if expected is None:
        assert "키" in reason


def test_openai_generator_is_built_from_settings(app_config) -> None:
    from nsm.adapters.generator.openai_text import OpenAITextGenerator

    cfg = _cfg(app_config, "openai")
    gen, reason = make_text_generator_with_reason(
        cfg, MemorySecretStore({"openai_api_key": "sk-x"}), FakeCostSink()
    )
    assert isinstance(gen, OpenAITextGenerator) and reason == ""
    assert gen.model == "gpt-6.1-sol" and gen.effort == "low" and gen.include_blog is False
    gen, reason = make_text_generator_with_reason(
        cfg, MemorySecretStore({"anthropic_api_key": "a"}), FakeCostSink()
    )
    assert gen is None and "OpenAI" in reason


def test_image_studio_only_with_openai_key(app_config, tmp_path) -> None:
    assert make_image_studio(app_config, MemorySecretStore({}), FakeCostSink(), tmp_path) is None
    studio = make_image_studio(
        app_config, MemorySecretStore({"openai_api_key": "o"}), FakeCostSink(), tmp_path
    )
    assert studio is not None and studio.default_quality == "medium"


def test_apply_adapters_closes_previous_resources(app_config, db, clock) -> None:
    from contextlib import ExitStack

    from nsm.store.repo import Repository

    ctx = JobContext(
        config=app_config, repo=Repository(db, clock), clock=clock, notifier=FakeNotifier()
    )
    closed: list[str] = []
    first, second = ExitStack(), ExitStack()
    first.callback(lambda: closed.append("first"))
    second.callback(lambda: closed.append("second"))
    ctx.apply_adapters(Adapters(resources=first))
    ctx.apply_adapters(Adapters(resources=second))
    assert closed == ["first"]  # 이전 묶음의 드라이버만 닫힌다
    ctx.close_adapters()
    assert closed == ["first", "second"]


def test_snapshot_keeps_running_job_isolated_from_refresh(app_config, db, clock) -> None:
    from nsm.store.repo import Repository

    ctx = JobContext(
        config=app_config, repo=Repository(db, clock), clock=clock, notifier=FakeNotifier()
    )
    first = FakeTextGenerator()
    ctx.apply_adapters(Adapters(text_generator=first, classifier_name="keyword"))
    running = ctx.snapshot()  # 잡이 시작할 때 받는 컨텍스트
    second = FakeTextGenerator()
    ctx.apply_adapters(
        Adapters(text_generator=None, text_generator_reason="키 없음", classifier_name="gemini")
    )
    assert running.text_generator is first and running.classifier_name == "keyword"
    nxt = ctx.snapshot()
    assert nxt.text_generator is None and nxt.text_generator_reason == "키 없음"
    ctx.apply_adapters(Adapters(text_generator=second))
    assert ctx.snapshot().text_generator is second and running.text_generator is first
