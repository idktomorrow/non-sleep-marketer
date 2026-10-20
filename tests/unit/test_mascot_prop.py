"""사진 우선형 몽글 캐릭터의 소품 고르기(S10): 카드 글의 낱말로 고르고, 없으면 손 흔들기."""

from nsm.render.mascot import pick_prop


def test_pick_prop_by_keywords() -> None:
    assert (
        pick_prop(["치이카와 × ANA 항공 콜라보", "기내 한정 굿즈"]) == "plane"
    )  # 항공·기내 2 > 굿즈 1
    assert pick_prop(["핸드드립 물 온도", "원두 보관법"]) == "mug"
    assert pick_prop(["극장판 4DX 확정", "개봉 일정"]) == "popcorn"
    assert pick_prop(["신상 키링 출시", "팝업 스토어"]) == "gift"
    assert pick_prop(["크리스마스 마켓 라인업"]) == "star"
    assert pick_prop(["오늘의 날씨", ""]) == "wave"
