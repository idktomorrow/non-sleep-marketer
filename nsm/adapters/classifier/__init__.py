"""분류기 선택 (ADR 0048): auto = Gemini 키 → gemini, Anthropic 키 → claude, 둘 다 없으면 keyword.

고른 분류기가 호출에 실패하면 다음 분류기로 넘어간다(FallbackClassifier). 관련성·주제·댓글 세 분류기가
상태(ChainState)를 함께 써서 한 번만 넘어가고 알림도 한 번만 간다. 넘어간 뒤 일정 시간이 지나면 첫 분류기를
다시 시도한다(잠깐의 오류로 유료 Claude에 눌러앉지 않게). jev(MotherDuck)는 C27에서 뺐다.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from nsm.adapters.classifier.keyword import COMMENT_VOCAB, KeywordClassifier
from nsm.config.schema import AppConfig
from nsm.config.secrets import SecretStore
from nsm.domain.models import Classification
from nsm.ports import Classifier, CostSink

log = logging.getLogger(__name__)

OnSwitch = Callable[[str, str, Exception], None]
RETRY_PRIMARY_AFTER_S = 30 * 60  # 대체한 뒤 이만큼 지나면 첫 분류기부터 다시 시도
NOTIFY_EVERY_S = 6 * 3600  # 같은 대체 알림은 이 간격으로만


class ChainState:
    """세 FallbackClassifier가 함께 쓰는 상태: 지금 몇 번째 분류기인지, 언제 넘어갔는지, 언제 알렸는지."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.index = 0
        self.switched_at: float | None = None
        self.notified_at: float | None = None
        self.on_switch: OnSwitch | None = None
        self.clock = clock


class FallbackClassifier:
    """순서대로 시도하는 분류기. 앞의 것이 예외를 내면 다음 것으로 넘어간다.

    `name`은 지금 쓰는 분류기 이름이라 분류 기록에 실제로 판정한 분류기가 남는다.
    """

    def __init__(
        self, chain: list[tuple[str, Classifier]], state: ChainState | None = None
    ) -> None:
        if not chain:
            raise ValueError("분류기가 하나도 없습니다")
        self.chain = chain
        self.state = state or ChainState()

    @property
    def name(self) -> str:
        return self.chain[self.state.index][0]

    def _call(self, method: str, *args):
        st = self.state
        now = st.clock()
        if (
            st.index > 0
            and st.switched_at is not None
            and now - st.switched_at >= RETRY_PRIMARY_AFTER_S
        ):
            log.info("분류기 %s를 다시 시도", self.chain[0][0])
            st.index, st.switched_at = 0, None
        while True:
            name, clf = self.chain[st.index]
            try:
                return getattr(clf, method)(*args)
            except Exception as e:  # noqa: BLE001  다음 분류기로
                if st.index + 1 >= len(self.chain):
                    raise
                nxt = self.chain[st.index + 1][0]
                log.warning("분류기 %s 실패, %s로 대체: %s", name, nxt, e)
                st.index += 1
                if st.switched_at is None:
                    st.switched_at = now
                due = st.notified_at is None or now - st.notified_at >= NOTIFY_EVERY_S
                if st.on_switch and due:
                    st.notified_at = now
                    try:
                        st.on_switch(name, nxt, e)
                    except Exception:  # noqa: BLE001  알림 실패가 분류를 막지 않게
                        log.exception("분류기 대체 알림 실패")

    def classify(self, text: str, labels: list[str], instruction: str) -> Classification:
        return self._call("classify", text, labels, instruction)

    def classify_many(
        self, texts: list[str], labels: list[str], instruction: str
    ) -> list[Classification]:
        return self._call("classify_many", texts, labels, instruction)


@dataclass
class ClassifierSet:
    """용도별 분류기. 키워드 대체 구현은 용도마다 어휘가 달라서 셋으로 나눈다."""

    name: str
    relevance: Classifier
    topic: Classifier
    comment: Classifier
    state: ChainState | None = None

    def set_on_switch(self, cb: OnSwitch) -> None:
        if self.state is not None:
            self.state.on_switch = cb


def _keyword_set(cfg: AppConfig) -> tuple[Classifier, Classifier, Classifier]:
    p, b = cfg.persona, cfg.brand
    relevance = KeywordClassifier(
        {"relevant": [*p.topics, b.name, *b.products, b.industry]}, default_label="irrelevant"
    )
    topic = KeywordClassifier({t: [t] for t in p.topics}, default_label=p.topics[0])
    comment = KeywordClassifier(COMMENT_VOCAB, default_label="praise_general")
    return relevance, topic, comment


def _gemini(cfg: AppConfig, key: str, cost_sink: CostSink) -> Classifier:
    from nsm.adapters.classifier.gemini import GeminiClassifier

    return GeminiClassifier(key, cfg.settings.models.gemini, cost_sink)


def _claude(cfg: AppConfig, key: str, cost_sink: CostSink) -> Classifier:
    import anthropic

    from nsm.adapters.classifier.claude import ClaudeClassifier
    from nsm.adapters.claude_common import CLASSIFY_TIMEOUT_S, MAX_RETRIES

    return ClaudeClassifier(
        anthropic.Anthropic(max_retries=MAX_RETRIES, timeout=CLASSIFY_TIMEOUT_S, api_key=key),
        cfg.settings.models.classifier,
        cost_sink,
    )


def make_classifiers(cfg: AppConfig, secrets: SecretStore, cost_sink: CostSink) -> ClassifierSet:
    choice = cfg.settings.classifier
    gemini_key = secrets.get("gemini_api_key")
    anthropic_key = secrets.get("anthropic_api_key")

    if choice == "gemini" and not gemini_key:
        raise RuntimeError("classifier=gemini인데 Gemini API 키가 없습니다 (`nsm init`)")
    if choice == "claude" and not anthropic_key:
        raise RuntimeError("classifier=claude인데 Claude API 키가 없습니다 (`nsm init`)")

    # 키가 있는 순서대로 사슬을 만든다. 고정 선택이면 그것부터, 뒤에는 대체용
    chain: list[tuple[str, Classifier]] = []
    if gemini_key and choice in ("auto", "gemini"):
        chain.append(("gemini", _gemini(cfg, gemini_key, cost_sink)))
    if anthropic_key and choice in ("auto", "gemini", "claude"):
        chain.append(("claude", _claude(cfg, anthropic_key, cost_sink)))
    kw_rel, kw_topic, kw_comment = _keyword_set(cfg)
    if not chain or choice == "keyword":
        return ClassifierSet("keyword", kw_rel, kw_topic, kw_comment)

    # 낱말 규칙은 맨 끝 대체(키가 있는 분류기가 모두 실패했을 때). 상태는 셋이 공유
    state = ChainState()
    rel = FallbackClassifier([*chain, ("keyword", kw_rel)], state)
    topic = FallbackClassifier([*chain, ("keyword", kw_topic)], state)
    comment = FallbackClassifier([*chain, ("keyword", kw_comment)], state)
    return ClassifierSet(chain[0][0], rel, topic, comment, state=state)
