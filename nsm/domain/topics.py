"""관심 주제 순환(S16 제안, ADR 0060): 설정의 '관심 주제' 칸 하나로 여러 주제를 다룬다.

- 종류(정보·밈)별로 최근 추천에서 **가장 오래 안 다룬 주제부터**. 한 번도 안 다룬 주제가 먼저(입력 순서).
- 정보 후보는 이 순서대로 주제를 번갈아 놓는다(한 게시물에는 주제 하나).
- 계정 설명(업종·인물상)과 관심 주제가 전혀 겹치지 않으면 경고만 한다(덮어쓰지 않는다).
"""

from __future__ import annotations

from datetime import datetime

from nsm.domain.picks_policy import normalize_tokens


def topic_order(topics: list[str], last_used: dict[str, datetime]) -> list[str]:
    never = [t for t in topics if t not in last_used]
    used = sorted((t for t in topics if t in last_used), key=lambda t: last_used[t])
    return never + used


def interleave_by_topic(ranked: list, order: list[str]) -> list:
    """주제별 순위 목록을 order 순서로 한 개씩 번갈아 놓는다(주제 안 순서는 그대로)."""
    groups: dict[str, list] = {}
    for c in ranked:
        groups.setdefault(c.topic, []).append(c)
    keys = [t for t in order if t in groups] + [t for t in groups if t not in order]
    out: list = []
    while any(groups[k] for k in keys):
        for k in keys:
            if groups[k]:
                out.append(groups[k].pop(0))
    return out


def account_topic_conflict(industry: str, character: str, topics: list[str]) -> str | None:
    """계정 설명과 관심 주제가 한 낱말도 겹치지 않으면 확인 문구. 판단은 사람이(덮어쓰지 않음)."""
    if not topics:
        return None
    acct = normalize_tokens(f"{industry} {character}")
    tops = set().union(*(normalize_tokens(t) for t in topics))
    if acct and tops and not (acct & tops):
        return (
            f"계정 설명(업종 '{industry}')과 관심 주제({', '.join(topics)})가 달라 보입니다. "
            "원고 말투·소개에 계정 설명이 쓰이니 설정에서 확인하세요(자동으로 바꾸지 않습니다)."
        )
    return None
