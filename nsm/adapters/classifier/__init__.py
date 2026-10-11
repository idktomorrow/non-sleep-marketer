"""분류기 선택 (ADR 0003): auto = MotherDuck 토큰 → jev, Anthropic 키 → claude, 둘 다 없으면 keyword."""

from __future__ import annotations

from dataclasses import dataclass

from nsm.adapters.classifier.keyword import COMMENT_VOCAB, KeywordClassifier
from nsm.config.schema import AppConfig
from nsm.config.secrets import SecretStore
from nsm.ports import Classifier, CostSink


@dataclass
class ClassifierSet:
    """용도별 분류기. 키워드 대체 구현은 용도마다 어휘가 달라서 셋으로 나눈다."""

    name: str
    relevance: Classifier
    topic: Classifier
    comment: Classifier


def make_classifiers(cfg: AppConfig, secrets: SecretStore, cost_sink: CostSink) -> ClassifierSet:
    choice = cfg.settings.classifier
    md_token = secrets.get("motherduck_token")
    anthropic_key = secrets.get("anthropic_api_key")

    if choice == "jev" or (choice == "auto" and md_token):
        if not md_token:
            raise RuntimeError("classifier=jev인데 MotherDuck 토큰이 없습니다 (`nsm init`)")
        from nsm.adapters.classifier.jev import JevClassifier

        c = JevClassifier(md_token, cost_sink)
        return ClassifierSet("jev", c, c, c)

    if choice == "claude" or (choice == "auto" and anthropic_key):
        if not anthropic_key:
            raise RuntimeError("classifier=claude인데 Claude API 키가 없습니다 (`nsm init`)")
        import anthropic

        from nsm.adapters.classifier.claude import ClaudeClassifier

        c = ClaudeClassifier(
            anthropic.Anthropic(api_key=anthropic_key), cfg.settings.models.classifier, cost_sink
        )
        return ClassifierSet("claude", c, c, c)

    p, b = cfg.persona, cfg.brand
    relevance = KeywordClassifier(
        {"relevant": [*p.topics, b.name, *b.products, b.industry]}, default_label="irrelevant"
    )
    topic = KeywordClassifier({t: [t] for t in p.topics}, default_label=p.topics[0])
    comment = KeywordClassifier(COMMENT_VOCAB, default_label="praise_general")
    return ClassifierSet("keyword", relevance, topic, comment)
