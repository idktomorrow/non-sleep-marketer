"""관심 분야 프리셋(S11 제안, ADR 0050): 한 번 누르면 검색어·별칭·제외어가 채워진다.

프리셋은 출발점일 뿐이다. 사용자는 설정 화면에서 관심 주제·제외어를 그대로 고칠 수 있다.
별칭은 검색어 확장에만 쓰고(`settings.picks.aliases`), 분류 라벨과 추천 주제는 대표 주제 하나다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class InterestPreset:
    key: str
    name: str
    topics: list[str]  # 대표 주제(분류 라벨·추천 주제)
    aliases: dict[str, list[str]] = field(default_factory=dict)  # 대표 주제 → 검색 별칭
    exclude_words: list[str] = field(default_factory=list)
    note: str = ""  # 화면에 보이는 주의 문구
    tone: str = ""  # 비우지 않으면 페르소나 말투도 바꾼다(키치·밈 톤 등)


PRESETS: dict[str, InterestPreset] = {
    "chiikawa": InterestPreset(
        key="chiikawa",
        name="치이카와",
        topics=["치이카와 굿즈", "치이카와 콜라보", "치이카와 행사"],
        aliases={
            "치이카와 굿즈": ["ちいかわ グッズ", "Chiikawa goods"],
            "치이카와 콜라보": ["ちいかわ コラボ"],
            "치이카와 행사": ["ちいかわ イベント", "치이카와 팝업"],
        },
        exclude_words=["중고", "양도", "삽니다"],
        note="굿즈·협업 사진에는 캐릭터 저작권이 있습니다. 사진 후보는 확인한 것만 쓰세요.",
        tone='친구한테 떠드는 밈 말투. 짧게 끊고 드립 한 스푼, 제목은 한 줄 드립(예: "이런 비행기 아니면 타기도 싫다"), 느낌표는 한 번만, 이모지는 한 개까지',
    ),
    "lol": InterestPreset(
        key="lol",
        name="리그 오브 레전드",
        topics=["롤 패치", "롤 e스포츠", "롤 신규 챔피언"],
        aliases={
            "롤 패치": ["리그오브레전드 패치", "LoL patch"],
            "롤 e스포츠": ["LCK", "롤드컵", "LoL Worlds"],
            "롤 신규 챔피언": ["리그오브레전드 챔피언", "LoL champion"],
        },
        exclude_words=["김밥", "롤케이크", "롤러", "롤스크린", "계란말이"],
        note="'롤'만 검색하면 김밥·케이크가 섞여서 별칭과 제외어를 같이 둡니다.",
    ),
    "celeb": InterestPreset(
        key="celeb",
        name="연예인",
        topics=["아이돌 컴백", "드라마 캐스팅", "연예인 광고 모델"],
        aliases={
            "아이돌 컴백": ["K-pop comeback", "컴백 티저"],
            "드라마 캐스팅": ["드라마 출연 확정"],
            "연예인 광고 모델": ["브랜드 앰버서더", "광고 모델 발탁"],
        },
        exclude_words=["열애설", "루머", "논란"],
        note="인물 사진에는 초상권이 있습니다. 사진 후보는 공식 보도자료·소속사 공개 이미지만 쓰고, 확신이 없으면 쓰지 마세요.",
    ),
    "fashion": InterestPreset(
        key="fashion",
        name="패션",
        topics=["패션 신상 출시", "패션 브랜드 콜라보", "패션 세일"],
        aliases={
            "패션 신상 출시": ["신상 드롭", "new arrival"],
            "패션 브랜드 콜라보": ["브랜드 협업 컬렉션", "collab collection"],
            "패션 세일": ["시즌오프", "블랙프라이데이 패션"],
        },
        exclude_words=["중고", "구제", "대여"],
        note="상품 사진은 브랜드 공식 룩북·보도자료만 쓰세요.",
    ),
}


def apply_preset(cfg: Any, key: str) -> Any:
    """프리셋을 설정에 적용한 새 AppConfig. 관심 주제·별칭·제외어를 바꾸고 추천 모드를 켠다."""
    if key not in PRESETS:
        raise KeyError(key)
    ps = PRESETS[key]
    persona = cfg.persona.model_copy(
        update={"topics": list(ps.topics), **({"tone": ps.tone} if ps.tone else {})}
    )
    collect = cfg.settings.collect.model_copy(
        update={
            "exclude_words": list(
                dict.fromkeys([*cfg.settings.collect.exclude_words, *ps.exclude_words])
            )
        }
    )
    picks = cfg.settings.picks.model_copy(
        update={"enabled": True, "aliases": {k: list(v) for k, v in ps.aliases.items()}}
    )
    settings = cfg.settings.model_copy(update={"collect": collect, "picks": picks})
    return cfg.model_copy(update={"persona": persona, "settings": settings})


def search_terms(topics: list[str], aliases: dict[str, list[str]]) -> list[str]:
    """검색에 쓰는 낱말: 대표 주제 + 별칭(중복 제거, 순서 유지). 분류 라벨에는 쓰지 않는다."""
    out: list[str] = []
    for t in topics:
        out.append(t)
        out.extend(aliases.get(t, []))
    return list(dict.fromkeys(x.strip() for x in out if x and x.strip()))
