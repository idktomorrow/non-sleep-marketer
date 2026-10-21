"""밈 규칙(S14 제안, ADR 0056): 중복은 상황+반전 겹침 **그리고** 대비 구조가 같을 때만. 사실 표현은 검토 신호만."""

from __future__ import annotations

from nsm.domain.meme_policy import (
    fact_signals,
    info_overlap,
    is_duplicate,
    meme_fingerprint,
    pick_idea,
)
from nsm.ports import MemeIdea, MemePanel, MemeScript

A = MemeIdea("극중 월급 vs 현실 연봉", "시무룩 vs 느긋", "'시러~'는 무슨.", "격차")


def _recent(*ideas: MemeIdea) -> list[tuple[str, str]]:
    from nsm.domain.meme_policy import meme_tokens

    return [(meme_fingerprint(i), " ".join(sorted(meme_tokens(i.contrast)))) for i in ideas]


def test_same_joke_with_swapped_words_is_duplicate() -> None:
    same = MemeIdea("극중 월급과 현실 연봉", "시무룩 느긋", "시러는 무슨.", "")
    assert is_duplicate(same, _recent(A))


def test_different_twist_same_character_passes() -> None:
    other = MemeIdea("굿즈 품절 전후", "설렘 vs 절망", "품절이라고?", "")
    assert not is_duplicate(other, _recent(A))
    # 같은 상황이라도 대비 구조가 다르면 다른 농담
    same_situation = MemeIdea("극중 월급 vs 현실 연봉", "출근길 vs 퇴근길", "'시러~'는 무슨.", "")
    assert not is_duplicate(same_situation, _recent(A))


def test_pick_idea_skips_duplicates_and_returns_none_when_all_dup() -> None:
    dup = MemeIdea("극중 월급 vs 현실 연봉", "시무룩 vs 느긋", "'시러~'는 무슨.", "")
    fresh = MemeIdea("팝업 줄서기", "아침 vs 저녁", "아직도 줄?", "")
    assert pick_idea([dup, fresh], _recent(A)) is fresh
    assert pick_idea([dup], _recent(A)) is None


def _script(labels, bubbles, caption="밈 #공감", claims=()) -> MemeScript:
    return MemeScript(
        idea=A,
        panels=(MemePanel(labels[0], bubbles[0], "s1"), MemePanel(labels[1], bubbles[1], "s2")),
        caption=caption,
        threads_text="t",
        claims=claims,
    )


def test_fact_signals_are_review_hints_not_errors() -> None:
    creative = _script(["인형 3개만", "결국"], ["사려 했는데", "12개"])
    hints = fact_signals(creative)
    assert hints and all(h.startswith("사실 확인 신호") for h in hints)  # 신호일 뿐
    assert (
        fact_signals(
            _script(["출시 전", "출시 후"], ["언제 나와", "품절"], claims=(("출시", "s1"),))
        )
        == []
    )
    assert fact_signals(_script(["전", "후"], ["시러", "싫다"])) == []  # 숫자·사실어 없음


def test_info_overlap_is_a_signal_only() -> None:
    idea = MemeIdea("치이카와 ANA 콜라보 비행기", "설렘 vs 현실", "표 값이 얼마라고", "")
    assert info_overlap(idea, ["치이카와 × ANA 콜라보 굿즈 공개"]) > 0.3
    assert info_overlap(idea, ["핸드드립 물 온도"]) == 0
