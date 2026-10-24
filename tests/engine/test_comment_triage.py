"""불만 댓글 2차 판정(C32, ADR 0052): 소식 반응은 공감 답글, 브랜드 불만·불확실은 사람에게."""

from __future__ import annotations

from nsm.domain.models import Classification, Comment
from nsm.engine.reply import SCOPE_INSTRUCTION, TOPIC_REACTION_GUIDANCE, run_reply
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


def _only_complaint(ctx, scope: Classification | None) -> None:
    ctx.comment_classifier = _Scoped(scope)
    for p, a in ctx.adapters.items():
        a.comments = {k: [COMPLAINT] if p == COMPLAINT.platform else [] for k in a.comments}


def test_topic_reaction_gets_empathetic_reply(ctx) -> None:
    _only_complaint(ctx, Classification(label="topic_reaction", confidence=0.9))
    gen = _Capture()
    ctx.text_generator = gen
    r = run_reply(ctx)
    s = ctx.repo.get_comment(f"{COMPLAINT.platform}:{COMPLAINT.external_id}")
    assert s.status == "auto_replied" and r.counts["escalated"] == 0
    assert gen.contexts and gen.contexts[0].startswith(TOPIC_REACTION_GUIDANCE)
    # 2차 판정 지시에는 브랜드 이름이 들어간다
    asked = [c for c in ctx.comment_classifier.calls if "불만의 대상" in c[2]]
    assert asked and ctx.config.brand.name in asked[0][2]
    assert "{brand}" not in SCOPE_INSTRUCTION.format(brand="x")


def test_brand_complaint_and_uncertain_scope_go_to_human(ctx) -> None:
    _only_complaint(ctx, Classification(label="brand_complaint", confidence=0.9))
    run_reply(ctx)
    s = ctx.repo.get_comment(f"{COMPLAINT.platform}:{COMPLAINT.external_id}")
    assert s.status == "escalated" and "브랜드" in s.reason
    # 신뢰도가 낮으면 모르는 것으로 보고 사람에게
    ctx.repo.db.execute("DELETE FROM comments")
    _only_complaint(ctx, Classification(label="topic_reaction", confidence=0.4))
    run_reply(ctx, manual=True)  # 인스타그램 확인 간격을 건너뛰고 바로
    s = ctx.repo.get_comment(f"{COMPLAINT.platform}:{COMPLAINT.external_id}")
    assert s.status == "escalated" and "불확실" in s.reason


def test_scope_judgement_failure_goes_to_human(ctx) -> None:
    class Flaky(FakeClassifier):
        def classify(self, text, labels, instruction):
            if "불만의 대상" in instruction:
                raise RuntimeError("quota")
            return super().classify(text, labels, instruction)

    ctx.comment_classifier = Flaky(
        by_text={COMPLAINT.text: Classification(label="complaint", confidence=0.9)}
    )
    for p, a in ctx.adapters.items():
        a.comments = {k: [COMPLAINT] if p == COMPLAINT.platform else [] for k in a.comments}
    run_reply(ctx)
    s = ctx.repo.get_comment(f"{COMPLAINT.platform}:{COMPLAINT.external_id}")
    assert s.status == "escalated"
