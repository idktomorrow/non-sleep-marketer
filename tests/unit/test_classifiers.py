from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nsm.adapters.classifier import make_classifiers
from nsm.adapters.classifier.claude import ClaudeClassifier
from nsm.adapters.classifier.jev import JevClassifier, build_sql
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


class FakeMD:
    def __init__(self, rows):
        self.rows = rows
        self.sql: list[str] = []
        self.inserted: list = []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        return self

    def executemany(self, sql, rows):
        self.inserted = rows

    def fetchone(self):
        return (self.rows[0],)

    def fetchall(self):
        return [(i, r) for i, r in enumerate(self.rows)]


def test_jev_sql_escapes_literals() -> None:
    sql = build_sql("it's 관련?", ["a'b", "c"], False)
    assert "'it''s 관련?'" in sql and "['a''b', 'c']" in sql and "prompt_jev(?," in sql


def test_jev_classifier_parses_choice_struct() -> None:
    md = FakeMD(
        [
            {
                "choice": "spam",
                "confidence": 0.9,
                "probabilities": [
                    {"value": "spam", "probability": 0.93},
                    {"value": "question", "probability": 0.03},
                ],
            },
            {"choice": "question", "confidence": 0.7},
        ]
    )
    sink = FakeCostSink()
    c = JevClassifier("tok", sink, connect=lambda t: md)
    one = c.classify("x", list(COMMENT_CATEGORIES), "분류")
    assert one.label == "spam" and one.confidence == pytest.approx(0.93)
    many = c.classify_many(["a", "b"], list(COMMENT_CATEGORIES), "분류")
    assert [m.label for m in many] == ["spam", "question"]
    assert md.inserted == [(0, "a"), (1, "b")]
    assert len(sink.records) == 2


def test_jev_classifier_rejects_unknown_label() -> None:
    md = FakeMD([{"choice": "weird", "confidence": 0.9}])
    with pytest.raises(AdapterError):
        JevClassifier("tok", connect=lambda t: md).classify("x", ["a", "b"], "q")


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
    both = MemorySecretStore({"anthropic_api_key": "k", "motherduck_token": "t"})
    assert make_classifiers(app_config, both, sink).name == "jev"
    forced = app_config.model_copy(
        update={"settings": app_config.settings.model_copy(update={"classifier": "jev"})}
    )
    with pytest.raises(RuntimeError):
        make_classifiers(forced, MemorySecretStore(), sink)


def test_claude_classifier_wraps_bad_json() -> None:
    class BadJson(FakeMessages):
        def create(self, **kwargs):
            r = super().create(**kwargs)
            r.content[1].text = '{"label": "rel'
            return r

    c = ClaudeClassifier(SimpleNamespace(messages=BadJson({})), "claude-haiku-5-5")
    with pytest.raises(AdapterError):
        c.classify("x", ["relevant", "irrelevant"], "q")


def test_jev_scrubs_token_from_errors() -> None:
    def connect(token):
        raise RuntimeError(f"cannot open md:?motherduck_token={token}")

    c = JevClassifier("SECRET-TOKEN", connect=connect)
    with pytest.raises(AdapterError) as e:
        c.classify_many(["a"], ["x", "y"], "q")
    assert "SECRET-TOKEN" not in str(e.value)


def test_jev_confidence_is_choice_probability_not_margin() -> None:
    # 2026-10-13 실제 응답: 치이카와 흥행 기사. confidence(0.40)는 1등-2등 차이라 그대로 쓰면
    # 관련 자료 기준 0.8에 못 미쳐 모두 떨어졌다. 고른 라벨의 확률(0.70)을 써야 한다.
    real = {
        "choice": "relevant",
        "probabilities": [
            {"value": "irrelevant", "probability": 0.3},
            {"value": "relevant", "probability": 0.7},
        ],
        "confidence": 0.4,
    }
    md = FakeMD([real, {"choice": "irrelevant", "confidence": 0.5}])
    c = JevClassifier("tok", connect=lambda t: md)
    hit, no_probs = c.classify_many(["a", "b"], ["relevant", "irrelevant"], "q")
    assert hit.confidence == pytest.approx(0.7)
    assert no_probs.confidence == pytest.approx(0.75)  # 확률이 없으면 (1 + 차이) / 2


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
