from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nsm.adapters.classifier import make_classifiers
from nsm.adapters.classifier.claude import ClaudeClassifier
from nsm.adapters.classifier.keyword import COMMENT_VOCAB, KeywordClassifier
from nsm.config.secrets import MemorySecretStore
from nsm.domain.models import COMMENT_CATEGORIES
from nsm.ports import AdapterError
from tests.fakes import FakeCostSink, load_comment_cases


class FakeMessages:
    def __init__(self, payload: dict, stop_reason: str = "end_turn") -> None:
        self.payload = payload
        self.stop_reason = stop_reason
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[
                SimpleNamespace(type="thinking", thinking=""),
                SimpleNamespace(type="text", text=json.dumps(self.payload)),
            ],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=20),
        )


def test_claude_classifier_uses_json_schema_and_records_cost() -> None:
    msgs = FakeMessages({"label": "relevant", "confidence": 1.4})
    sink = FakeCostSink()
    c = ClaudeClassifier(SimpleNamespace(messages=msgs), "claude-haiku-5-5", sink)
    r = c.classify("핸드드립 이야기", ["relevant", "irrelevant"], "관련 있나?")
    assert r.label == "relevant" and r.confidence == 1.0  # 범위 밖 값은 잘라낸다
    req = msgs.requests[0]
    schema = req["output_config"]["format"]["schema"]
    assert schema["properties"]["label"]["enum"] == ["relevant", "irrelevant"]
    assert req["model"] == "claude-haiku-5-5"
    kind, provider, usd, _ = sink.records[0]
    assert kind == "classifier" and provider == "anthropic"
    assert usd == pytest.approx((1000 * 0.10 + 20 * 0.50) / 1_000_000)


def test_claude_classifier_raises_on_refusal() -> None:
    msgs = FakeMessages({"label": "relevant", "confidence": 0.9}, stop_reason="refusal")
    c = ClaudeClassifier(SimpleNamespace(messages=msgs), "claude-haiku-5-5")
    with pytest.raises(AdapterError):
        c.classify("x", ["relevant", "irrelevant"], "q")


def test_keyword_classifier_on_comment_fixture() -> None:
    k = KeywordClassifier(COMMENT_VOCAB, default_label="praise_general")
    cases = load_comment_cases()
    hits = sum(
        k.classify(c.text, list(COMMENT_CATEGORIES), "").label == expected for c, expected in cases
    )
    assert hits >= 5
    assert k.classify("아무 말", ["relevant", "irrelevant"], "").confidence == 0.5


def test_make_classifiers_auto_selection(app_config) -> None:
    sink = FakeCostSink()
    assert make_classifiers(app_config, MemorySecretStore(), sink).name == "keyword"
    assert (
        make_classifiers(app_config, MemorySecretStore({"anthropic_api_key": "k"}), sink).name
        == "claude"
    )
    both = MemorySecretStore({"anthropic_api_key": "k", "gemini_api_key": "g"})
    cs = make_classifiers(app_config, both, sink)
    assert cs.name == "gemini"  # 무료 구간이 먼저(ADR 0048)
    assert [n for n, _ in cs.relevance.chain] == ["gemini", "claude", "keyword"]
    for forced_to in ("gemini", "claude"):
        forced = app_config.model_copy(
            update={"settings": app_config.settings.model_copy(update={"classifier": forced_to})}
        )
        with pytest.raises(RuntimeError):
            make_classifiers(forced, MemorySecretStore(), sink)
    kw = app_config.model_copy(
        update={"settings": app_config.settings.model_copy(update={"classifier": "keyword"})}
    )
    assert make_classifiers(kw, both, sink).name == "keyword"


def test_fallback_switches_once_and_records_who_answered() -> None:
    from nsm.adapters.classifier import (
        NOTIFY_EVERY_S,
        RETRY_PRIMARY_AFTER_S,
        ChainState,
        FallbackClassifier,
    )
    from nsm.domain.models import Classification

    calls: list[str] = []

    class Boom:
        def classify(self, *a):
            calls.append("boom")
            raise AdapterError("quota")

        def classify_many(self, texts, *a):
            calls.append("boom")
            raise AdapterError("quota")

    class Ok:
        def classify(self, text, labels, instruction):
            calls.append("ok")
            return Classification(label=labels[0], confidence=0.9)

        def classify_many(self, texts, labels, instruction):
            calls.append("ok")
            return [Classification(label=labels[0], confidence=0.9) for _ in texts]

    switched: list[tuple[str, str]] = []
    now = {"t": 0.0}
    state = ChainState(clock=lambda: now["t"])
    state.on_switch = lambda a, b, e: switched.append((a, b))
    f = FallbackClassifier([("gemini", Boom()), ("claude", Ok())], state)
    g = FallbackClassifier([("gemini", Boom()), ("claude", Ok())], state)  # 주제용, 상태 공유
    assert f.name == "gemini"
    assert [c.label for c in f.classify_many(["x", "y"], ["r", "i"], "q")] == ["r", "r"]
    assert switched == [("gemini", "claude")] and f.name == "claude" and g.name == "claude"
    g.classify("z", ["r", "i"], "q")
    assert calls == [
        "boom",
        "ok",
        "ok",
    ]  # 넘어간 뒤에는 실패한 쪽을 다시 두드리지 않는다(다른 용도도)
    # 30분 지나면 첫 분류기를 다시 시도한다. 또 실패해도 알림은 6시간에 한 번
    now["t"] += RETRY_PRIMARY_AFTER_S
    f.classify("z", ["r", "i"], "q")
    assert calls[-2:] == ["boom", "ok"] and switched == [("gemini", "claude")]
    now["t"] += NOTIFY_EVERY_S
    f.classify("z", ["r", "i"], "q")
    assert switched == [("gemini", "claude")] * 2
    with pytest.raises(AdapterError):
        FallbackClassifier([("gemini", Boom())]).classify("z", ["r", "i"], "q")


def test_claude_classifier_wraps_bad_json() -> None:
    class BadJson(FakeMessages):
        def create(self, **kwargs):
            r = super().create(**kwargs)
            r.content[1].text = '{"label": "rel'
            return r

    c = ClaudeClassifier(SimpleNamespace(messages=BadJson({})), "claude-haiku-5-5")
    with pytest.raises(AdapterError):
        c.classify("x", ["relevant", "irrelevant"], "q")


def test_claude_clients_have_timeouts(app_config, monkeypatch) -> None:
    """C24: 작업 스레드가 하나라 Claude 호출이 멈추면 다른 작업도 멈춘다. 기본(10분×5번)을 쓰지 않는다."""
    import anthropic

    from nsm.adapters.generator import make_text_generator

    seen: list[dict] = []

    class FakeClient:
        def __init__(self, **kw):
            seen.append(kw)

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    secrets = MemorySecretStore({"anthropic_api_key": "k"})
    make_classifiers(app_config, secrets, FakeCostSink())
    make_text_generator(app_config, secrets, FakeCostSink())
    assert len(seen) == 2
    for kw in seen:
        assert 0 < kw["timeout"] <= 180 and kw["max_retries"] <= 5
