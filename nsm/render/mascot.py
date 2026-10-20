"""사진 우선형(editorial)의 몽글몽글 캐릭터 소품 고르기(S10 제안, ADR 0049 보강).

캐릭터는 자체 SVG(동그란 몸·새싹·홍조)다. 저작권 캐릭터를 흉내 내지 않는다(ADR 0036).
카드 글에 든 낱말로 소품을 고른다: 항공 소식이면 조종사 모자, 커피면 머그컵, 극장판이면 팝콘, 굿즈 출시면 선물 가방.
"""

from __future__ import annotations

from collections.abc import Iterable

PROPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("plane", ("비행기", "항공", "기내", "공항", "탑승", "기장", "승무원", "ANA", "제트", "운항")),
    ("mug", ("커피", "카페", "원두", "드립", "라떼", "에스프레소", "로스터", "홈카페", "머그")),
    ("popcorn", ("극장판", "영화", "4DX", "개봉", "상영", "팝콘", "시사회", "애니메이션")),
    (
        "gift",
        ("굿즈", "인형", "키링", "출시", "신상", "한정", "콜라보", "팝업", "발매", "예약판매"),
    ),
    ("star", ("크리스마스", "이벤트", "행사", "축제", "기념", "생일", "파티")),
)
DEFAULT_PROP = "wave"


def pick_prop(texts: Iterable[str]) -> str:
    """글 묶음에서 가장 먼저 걸리는 소품. 낱말이 여러 소품에 걸리면 더 많이 나온 쪽."""
    hay = " ".join(t for t in texts if t).lower()
    best, best_n = DEFAULT_PROP, 0
    for prop, words in PROPS:
        n = sum(hay.count(w.lower()) for w in words)
        if n > best_n:
            best, best_n = prop, n
    return best
