"""키워드 분류기: API 키가 하나도 없을 때의 대체 구현.

품질은 낮지만 무료이고 오프라인으로 돈다. 드라이런 체험용이며, 신뢰도를 일부러 낮게(최대 0.85) 둬서
정책이 대부분 사람 검토로 보내게 한다.
"""

from __future__ import annotations

from nsm.domain.models import Classification

# 댓글 6분류용 기본 어휘
COMMENT_VOCAB: dict[str, list[str]] = {
    "spam": [
        "http://",
        "https://",
        "dm 주세요",
        "dm주세요",
        "부업",
        "수익",
        "클릭",
        "홍보",
        "팔로우 해주시면",
    ],
    "legal": ["법적", "고소", "소송", "상표", "저작권", "변호사", "신고하겠", "명예훼손"],
    "price_refund": ["환불", "가격", "비싸", "할인", "결제", "취소", "얼마"],
    "complaint": ["불만", "실망", "배송", "안 와", "안와", "불량", "최악", "별로", "너무하"],
    "question": ["?", "궁금", "어떻게", "되나요", "인가요", "알려주"],
    "praise_general": ["좋아", "최고", "감사", "예뻐", "멋져", "유용", "저장", "👍", "❤"],
}


class KeywordClassifier:
    name = "keyword"

    def __init__(
        self,
        vocab: dict[str, list[str]] | None = None,
        default_label: str | None = None,
    ) -> None:
        self.vocab = {k: [w.lower() for w in v] for k, v in (vocab or {}).items()}
        self.default_label = default_label

    def classify(self, text: str, labels: list[str], instruction: str) -> Classification:
        low = text.lower()
        scores = {
            label: sum(1 for w in self.vocab.get(label, [label.lower()]) if w and w in low)
            for label in labels
        }
        # 위험한 범주가 먼저 이기도록 labels 순서가 아닌 vocab 순서를 우선한다
        order = [k for k in self.vocab if k in labels] + [k for k in labels if k not in self.vocab]
        best = max(order, key=lambda k: (scores[k], -order.index(k)))
        if scores[best] == 0:
            fallback = self.default_label if self.default_label in labels else labels[-1]
            return Classification(label=fallback, confidence=0.5)
        return Classification(label=best, confidence=min(0.85, 0.6 + 0.1 * scores[best]))

    def classify_many(
        self, texts: list[str], labels: list[str], instruction: str
    ) -> list[Classification]:
        return [self.classify(t, labels, instruction) for t in texts]
