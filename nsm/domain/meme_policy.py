"""밈 후보의 순수 규칙(S14 제안, ADR 0056). 외부 호출·DB 접근이 없다.

- 중복: 같은 농담(상황+반전 토큰이 많이 겹치고 **반전 구조도 같음**)만 잡는다. 같은 캐릭터·분야의 다른 반전은 통과.
  단어만 바꾼 같은 농담은 잡는다. 토큰 유사도만으로 "정보에 드립 붙인 것"을 버리지 않는다(검토 신호만).
- 사실 신호: 숫자·날짜·가격·"출시/확정/발표" 표현이 있는데 claims가 비면 **검토 신호**("사실 확인 신호")일 뿐
  오류가 아니다. "인형 3개만 사려 했는데" 같은 창작 대사는 검토자가 통과시킨다.
"""

from __future__ import annotations

import re

from nsm.domain.picks_policy import jaccard, normalize_tokens
from nsm.ports import MemeIdea, MemeScript

DUP_JACCARD = 0.6
# 조사만 다른 낱말("월급과"·"월급")을 같은 토큰으로: 단어만 바꾼 같은 농담을 잡기 위해. 밈 비교에만 쓴다
_PARTICLES = (
    "과",
    "와",
    "는",
    "은",
    "이",
    "가",
    "을",
    "를",
    "의",
    "도",
    "에",
    "로",
    "에서",
    "한테",
)
_MEME_STOP = frozenset(["vs", "대", "그리고", "근데"])
# 낱말 단위로만(한 글자 "원"·"엔"은 "원래·응원·엔딩"에 걸려 신호가 소음이 된다, S14 리뷰). 통화는 숫자와 붙은 것만
_FACT_WORDS = ("출시", "확정", "발표", "공개", "예약", "가격", "할인", "판매")
_MONEY = re.compile(r"\d[\d,.]*\s*(원|엔|달러|만원|억)|[¥$₩]\s*\d|\d\s*%")
_NUMBER = re.compile(r"\d")


def meme_tokens(text: str) -> set[str]:
    out: set[str] = set()
    for tok in normalize_tokens(text):
        if tok in _MEME_STOP:
            continue
        for p in sorted(_PARTICLES, key=len, reverse=True):
            if len(tok) > len(p) + 1 and tok.endswith(p):
                tok = tok[: -len(p)]
                break
        out.add(tok)
    return out


def meme_fingerprint(idea: MemeIdea) -> str:
    """상황+반전의 정규화 토큰(저장용)."""
    return " ".join(sorted(meme_tokens(f"{idea.situation} {idea.punchline}")))


def _structure(idea: MemeIdea) -> frozenset[str]:
    """반전 구조: 대비(contrast)의 토큰. 같은 상황이라도 대비가 다르면 다른 농담으로 본다."""
    return frozenset(meme_tokens(idea.contrast))


def is_duplicate(idea: MemeIdea, recent: list[tuple[str, str]]) -> bool:
    """recent = [(fingerprint, contrast_tokens_joined)]. 상황+반전 겹침 ≥0.6 **이고** 대비 구조도 겹칠 때만."""
    mine = set(meme_fingerprint(idea).split())
    struct = _structure(idea)
    for fp, contrast in recent:
        theirs = set(fp.split())
        if jaccard(mine, theirs) < DUP_JACCARD:
            continue
        their_struct = set(contrast.split())
        if not struct or not their_struct or jaccard(set(struct), their_struct) >= 0.5:
            return True
    return False


def pick_idea(ideas: list[MemeIdea], recent: list[tuple[str, str]]) -> MemeIdea | None:
    """첫 번째로 중복이 아닌 아이디어. 전부 중복이면 None(억지로 채우지 않는다)."""
    for idea in ideas:
        if not is_duplicate(idea, recent):
            return idea
    return None


def fact_signals(script: MemeScript) -> list[str]:
    """검토 신호: 사실처럼 보이는 표현이 있는데 자료 근거(claims)가 없다. 창작 대사일 수 있으니 오류는 아니다."""
    if script.claims:
        return []
    texts = [p.label for p in script.panels] + [p.bubble for p in script.panels] + [script.caption]
    hits = []
    for t in texts:
        if _MONEY.search(t) or _NUMBER.search(t) or any(w in t for w in _FACT_WORDS):
            hits.append(t[:40])
    return [f"사실 확인 신호: {h}" for h in hits[:3]]


def info_overlap(idea: MemeIdea, info_titles: list[str], aliases=None) -> float:
    """정보 클러스터 제목과 얼마나 겹치는지(0~1). 화면 신호용이며 자동 제외에 쓰지 않는다."""
    mine = normalize_tokens(f"{idea.situation} {idea.punchline}", aliases)
    best = 0.0
    for t in info_titles:
        best = max(best, jaccard(mine, normalize_tokens(t, aliases)))
    return best
