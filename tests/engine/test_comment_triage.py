"""불만 댓글 2차 판정(C32, ADR 0052): 소식 반응은 공감 답글, 브랜드 불만·불확실은 사람에게."""

from __future__ import annotations

from nsm.domain.models import Classification, Comment
from nsm.engine.reply import SCOPE_INSTRUCTION, run_reply
from tests.fakes import FakeClassifier, load_comment_cases

COMPLAINT = next(c for c, cat in load_comment_cases() if cat == "complaint")


class _Capture:
    def __init__(self) -> None:
        self.contexts: list[str] = []

    def generate_reply(self, comment: Comment, persona, post_context: str) -> str:
        self.contexts.append(post_context)
        return "정가에 사기 힘든 건 정말 아쉽죠"


class _Scoped(FakeClassifier):
    """1차(유형)는 complaint, 2차(불만 범위)는 주어진 판정. 같은 댓글 글로 두 번 묻기 때문에 지시문으로 가른다."""

    def __init__(self, scope: Classification | None) -> None:
        super().__init__(
            by_text={COMPLAINT.text: Classification(label="complaint", confidence=0.9)}
        )
        self.scope = scope

    def classify(self, text, labels, instruction):
        self.calls.append((text, tuple(labels), instruction))
        if "불만의 대상" in instruction:
            return self.scope or Classification(label="complaint", confidence=0.9)
        return super().classify(text, labels, instruction)


def _only_complaint(ctx, classifier) -> None:
    ctx.comment_classifier = classifier
    for p, a in ctx.adapters.items():
        a.comments = {k: [COMPLAINT] if p == COMPLAINT.platform else [] for k in a.comments}


def test_topic_reaction_gets_empathetic_reply(ctx) -> None:
    _only_complaint(ctx, _Scoped(Classification(label="topic_reaction", confidence=0.9)))
    gen = _Capture()
    ctx.text_generator = gen
    r = run_reply(ctx)
    s = ctx.repo.get_comment(f"{COMPLAINT.platform}:{COMPLAINT.external_id}")
    assert s.status == "auto_replied" and r.counts["escalated"] == 0
    # 지침은 자료(데이터) 쪽에 섞지 않는다: 답글 규칙은 시스템 지시에 있다
    assert gen.contexts and "답글 지침" not in gen.contexts[0]
    # 2차 판정 지시에는 브랜드 이름이 들어간다
    asked = [c for c in ctx.comment_classifier.calls if "불만의 대상" in c[2]]
    assert asked and ctx.config.brand.name in asked[0][2]
    assert "{brand}" not in SCOPE_INSTRUCTION.format(brand="x")


def test_brand_complaint_and_uncertain_scope_go_to_human(ctx) -> None:
    _only_complaint(ctx, _Scoped(Classification(label="brand_complaint", confidence=0.9)))
    run_reply(ctx)
    s = ctx.repo.get_comment(f"{COMPLAINT.platform}:{COMPLAINT.external_id}")
    assert s.status == "escalated" and "브랜드" in s.reason
    # 신뢰도가 낮으면 모르는 것으로 보고 사람에게
    ctx.repo.db.execute("DELETE FROM comments")
    _only_complaint(ctx, _Scoped(Classification(label="topic_reaction", confidence=0.4)))
    run_reply(ctx, manual=True)  # 인스타그램 확인 간격을 건너뛰고 바로
    s = ctx.repo.get_comment(f"{COMPLAINT.platform}:{COMPLAINT.external_id}")
    assert s.status == "escalated" and "불확실" in s.reason


def test_scope_judgement_failure_goes_to_human(ctx) -> None:
    class Flaky(FakeClassifier):
        def classify(self, text, labels, instruction):
            if "불만의 대상" in instruction:
                raise RuntimeError("quota")
            return super().classify(text, labels, instruction)

    _only_complaint(
        ctx, Flaky(by_text={COMPLAINT.text: Classification(label="complaint", confidence=0.9)})
    )
    run_reply(ctx)
    s = ctx.repo.get_comment(f"{COMPLAINT.platform}:{COMPLAINT.external_id}")
    assert s.status == "escalated"


def test_scope_is_not_asked_when_outcome_is_already_fixed(ctx) -> None:
    """리뷰: 1차 신뢰도가 낮거나 답글이 불가능하면 2차 판정(호출)을 쓰지 않는다."""

    class Low(_Scoped):
        def classify(self, text, labels, instruction):
            c = super().classify(text, labels, instruction)
            return (
                c.model_copy(update={"confidence": 0.4}) if "불만의 대상" not in instruction else c
            )

    _only_complaint(ctx, Low(Classification(label="topic_reaction", confidence=0.9)))
    run_reply(ctx)
    s = ctx.repo.get_comment(f"{COMPLAINT.platform}:{COMPLAINT.external_id}")
    assert s.status == "escalated" and "불확실" in s.reason
    assert not [c for c in ctx.comment_classifier.calls if "불만의 대상" in c[2]]


def test_low_confidence_spam_is_not_silently_ignored(ctx) -> None:
    """리뷰: 숨기기 없는 플랫폼에서 신뢰도 낮은 스팸 판정은 버리지 않고 사람에게."""
    from tests.fakes import FakePlatformAdapter

    spam = next(c for c, cat in load_comment_cases() if cat == "spam")
    ctx.adapters["threads"] = FakePlatformAdapter(
        "threads", capabilities={"fetch_comments", "reply", "publish"}, clock=ctx.clock
    )
    ctx.adapters["threads"].comments = {"p1": [spam]}
    ctx.comment_classifier = FakeClassifier(default=Classification(label="spam", confidence=0.35))
    run_reply(ctx)
    s = ctx.repo.get_comment("threads:c6")
    assert s.status == "escalated" and "불확실" in s.reason


def test_scope_fallback_label_is_the_safe_one() -> None:
    from nsm.domain.policy import COMPLAINT_SCOPES

    assert COMPLAINT_SCOPES[-1] == "brand_complaint"  # 분류기가 라벨을 못 고르면 사람에게
