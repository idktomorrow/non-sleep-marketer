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


# ---- C59: 묶음 단위 폴백 + Claude 묶음 호출 (ADR 0079) ----


def test_fallback_chunk_size_matches_gemini_batch() -> None:
    from nsm.adapters.classifier import CHUNK, gemini
    from nsm.adapters.classifier import claude as claude_mod

    assert CHUNK == gemini.BATCH == claude_mod.BATCH


def test_fallback_keeps_first_chunk_when_second_fails() -> None:
    from nsm.adapters.classifier import ChainState, FallbackClassifier
    from nsm.domain.models import Classification

    first_sizes: list[int] = []
    second_sizes: list[int] = []

    class FirstThenFail:
        def classify_many(self, texts, labels, instruction):
            first_sizes.append(len(texts))
            if len(first_sizes) > 1:
                raise AdapterError("503")
            return [Classification(label="first", confidence=0.8) for _ in texts]

    class Second:
        def classify_many(self, texts, labels, instruction):
            second_sizes.append(len(texts))
            return [Classification(label="second", confidence=0.7) for _ in texts]

    state = ChainState(clock=lambda: 0.0)
    f = FallbackClassifier([("gemini", FirstThenFail()), ("claude", Second())], state)
    out = f.classify_many([f"t{i}" for i in range(15)], ["first", "second"], "q")
    assert [c.label for c in out] == ["first"] * 10 + ["second"] * 5
    assert first_sizes == [10, 5] and second_sizes == [5]
    assert state.index == 1


def test_fallback_empty_list_makes_no_call() -> None:
    from nsm.adapters.classifier import FallbackClassifier

    class Never:
        def classify_many(self, *a):
            raise AssertionError("부르면 안 됨")

    assert FallbackClassifier([("gemini", Never())]).classify_many([], ["r", "i"], "q") == []


class ScriptedMessages:
    """요청마다 정해 둔 응답 텍스트를 돌려준다. 단건 스키마(label만 있음) 요청에는 단건 응답."""

    def __init__(self, batch_texts: list[str | None], single_label: str = "irrelevant") -> None:
        self.batch_texts = list(batch_texts)
        self.single_label = single_label
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        schema = kwargs["output_config"]["format"]["schema"]
        if "items" in schema["properties"]:
            text = self.batch_texts.pop(0)
        else:
            text = json.dumps({"label": self.single_label, "confidence": 0.6})
        content = [SimpleNamespace(type="thinking", thinking="")]
        if text is not None:
            content.append(SimpleNamespace(type="text", text=text))
        return SimpleNamespace(
            stop_reason="end_turn",
            content=content,
            usage=SimpleNamespace(input_tokens=500, output_tokens=50),
        )


def _items(n: int, label: str = "relevant", conf: float = 0.9, order=None) -> str:
    idx = order if order is not None else range(n)
    return json.dumps({"items": [{"i": i, "label": label, "confidence": conf} for i in idx]})


LABELS = ["relevant", "irrelevant"]


def test_claude_classify_many_batches_by_ten_and_maps_by_index() -> None:
    first = json.dumps(
        {
            "items": [
                {"i": i, "label": "relevant" if i % 2 == 0 else "irrelevant", "confidence": 0.5}
                for i in reversed(range(10))  # 순서가 뒤섞여 와도 i로 맞춘다
            ]
        }
    )
    second = json.dumps(
        {
            "items": [
                {"i": 0, "label": "irrelevant", "confidence": 1.3},
                {"i": 1, "label": "relevant", "confidence": -0.2},
                {"i": 2, "label": "relevant", "confidence": 0.4},
            ]
        }
    )
    msgs = ScriptedMessages([first, second])
    sink = FakeCostSink()
    c = ClaudeClassifier(SimpleNamespace(messages=msgs), "claude-haiku-5-5", sink)
    out = c.classify_many([f"자료 {i} " + "가" * 3000 for i in range(13)], LABELS, "관련 있나?")
    assert len(msgs.requests) == 2
    assert [r.label for r in out[:10]] == ["relevant", "irrelevant"] * 5
    assert [r.label for r in out[10:]] == ["irrelevant", "relevant", "relevant"]
    assert [r.confidence for r in out[10:]] == [1.0, 0.0, 0.4]
    assert len(sink.records) == 2
    assert all(k == "classifier" and p == "anthropic" for k, p, _, _ in sink.records)
    assert [r[3] for r in sink.records] == ["classify_many", "classify_many"]
    req = msgs.requests[0]
    assert req["max_tokens"] == 2048 and req["thinking"] == {"type": "disabled"}
    assert req["output_config"]["effort"] == "low"
    schema = req["output_config"]["format"]["schema"]
    item = schema["properties"]["items"]["items"]
    assert schema["additionalProperties"] is False and item["additionalProperties"] is False
    assert item["required"] == ["i", "label", "confidence"]
    assert item["properties"]["label"]["enum"] == LABELS
    content = req["messages"][0]["content"]
    assert "[9]" in content and "[10]" not in content
    assert (
        "가" * 1495 in content and "가" * 1496 not in content
    )  # 건당 1500자까지만(앞 "자료 i " 5자)


@pytest.mark.parametrize(
    "broken",
    [
        _items(4, order=[0, 1, 3]),  # 2번 빠짐
        _items(4, label="spam"),  # 허용되지 않은 라벨
        '{"items": [{"i": 0, "lab',  # 깨진 JSON
        None,  # 텍스트 블록 없음
        '["not", "an", "object"]',
        _items(4, order=[0, 1, 1, 2, 3]),  # 번호 겹침
        _items(4, order=[0, 1, 2, True]),  # 정수가 아닌 번호
    ],
)
def test_claude_classify_many_broken_batch_retries_one_by_one(broken) -> None:
    msgs = ScriptedMessages([broken], single_label="irrelevant")
    sink = FakeCostSink()
    c = ClaudeClassifier(SimpleNamespace(messages=msgs), "claude-haiku-5-5", sink)
    out = c.classify_many(["a", "b", "c", "d"], LABELS, "q")  # 예외 없이
    assert len(msgs.requests) == 1 + 4
    assert [r.label for r in out] == ["irrelevant"] * 4
    contents = [r["messages"][0]["content"] for r in msgs.requests[1:]]
    assert [x.endswith(f"<text>\n{t}\n</text>") for x, t in zip(contents, "abcd", strict=True)] == [
        True
    ] * 4
    assert [r[3] for r in sink.records] == ["classify_many"] + ["classify"] * 4


def test_claude_classify_many_refusal_still_raises() -> None:
    msgs = FakeMessages({"items": []}, stop_reason="refusal")
    c = ClaudeClassifier(SimpleNamespace(messages=msgs), "claude-haiku-5-5")
    with pytest.raises(AdapterError):
        c.classify_many(["a", "b"], LABELS, "q")
    assert len(msgs.requests) == 1


def test_claude_classify_many_single_and_empty_use_single_call() -> None:
    msgs = FakeMessages({"label": "relevant", "confidence": 0.8})
    c = ClaudeClassifier(SimpleNamespace(messages=msgs), "claude-haiku-5-5")
    assert c.classify_many([], LABELS, "q") == []
    assert msgs.requests == []
    out = c.classify_many(["하나"], LABELS, "q")
    assert [r.label for r in out] == ["relevant"]
    schema = msgs.requests[0]["output_config"]["format"]["schema"]
    assert set(schema["properties"]) == {"label", "confidence"}


# ---- C60: 낱말 분류기 위험 댓글 범주 우선 (ADR 0080, 외부 검토 1-8) ----


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("환불 어떻게 하나요?", "price_refund"),  # question 낱말이 2개여도 위험 범주가 이긴다
        ("고소하려면 어떻게 되나요?", "legal"),
        ("배송이 왜 안 와요? 궁금해요?", "complaint"),
        ("이거어떻게 쓰나요?", "question"),  # 위험 낱말이 없으면 그대로
        ("최고 좋아요 감사", "praise_general"),
        ("환불 받고 고소할래요", "legal"),  # 위험 범주끼리는 vocab 순서
    ],
)
def test_keyword_comment_priority_categories_win(text: str, expected: str) -> None:
    from nsm.adapters.classifier.keyword import COMMENT_PRIORITY

    k = KeywordClassifier(COMMENT_VOCAB, default_label="praise_general", priority=COMMENT_PRIORITY)
    assert COMMENT_PRIORITY == 4
    assert k.classify(text, list(COMMENT_CATEGORIES), "").label == expected


def test_keyword_without_priority_keeps_count_rule() -> None:
    k = KeywordClassifier(COMMENT_VOCAB, default_label="praise_general")
    assert k.classify("환불 어떻게 하나요?", list(COMMENT_CATEGORIES), "").label == "question"
    rel = KeywordClassifier({"relevant": ["커피", "드립"]}, default_label="irrelevant")
    assert rel.priority == []
    assert rel.classify("드립 커피", ["relevant", "irrelevant"], "").label == "relevant"
    assert rel.classify("날씨", ["relevant", "irrelevant"], "").label == "irrelevant"


def test_make_classifiers_comment_keyword_uses_priority(app_config) -> None:
    cs = make_classifiers(app_config, MemorySecretStore(), FakeCostSink())
    assert cs.comment.priority == ["spam", "legal", "price_refund", "complaint"]
    assert cs.relevance.priority == [] and cs.topic.priority == []
    assert cs.comment.classify("환불 어떻게 하나요?", list(COMMENT_CATEGORIES), "").label == (
        "price_refund"
    )
