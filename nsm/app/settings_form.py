"""대시보드 설정 화면(M17): 폼 → 새 AppConfig. 라우트는 server.py, 여기는 검사와 변환만(테스트하기 쉽게).

API 키·게시 계정·드라이런은 여기서 바꾸지 않는다. 키는 keyring, 계정과 드라이런은 `nsm init`(ADR 0028).
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from nsm.config.schema import AppConfig
from nsm.config.store import format_windows, parse_windows

MAX_TOPICS = 10
TEXT_PROVIDER_NAMES = {
    "auto": "자동(Claude 키 있으면 Claude, 아니면 OpenAI)",
    "openai": "OpenAI만",
    "claude": "Claude만",
}
IMAGE_QUALITY_NAMES = {
    "low": "낮음(장당 약 1~2센트)",
    "medium": "보통(장당 약 5센트)",
    "high": "높음(장당 약 20센트)",
}
SLOT_TOTAL_MAX = 6  # 하루 추천(정보+밈) 합계 상한(S13)
MAX_LINES = 30
MAX_TEXT = 500
QUOTA_MAX = 10
COST_CAP_MAX = 100.0
IG_INTERVAL = "instagram_comment_interval_minutes"


class SettingsError(ValueError):
    """사람이 넣은 설정 값이 형식에 맞지 않음. 화면에 그대로 보여 준다."""


def _text(form: dict[str, Any], key: str, label: str, required: bool = True) -> str:
    v = str(form.get(key, "")).strip()
    if required and not v:
        raise SettingsError(f"{label}: 비워 둘 수 없습니다")
    if len(v) > MAX_TEXT:
        raise SettingsError(f"{label}: {MAX_TEXT}자까지입니다")
    return v


def _lines(form: dict[str, Any], key: str, label: str, max_n: int = MAX_LINES) -> list[str]:
    out = list(dict.fromkeys(x.strip() for x in str(form.get(key, "")).splitlines() if x.strip()))
    if len(out) > max_n:
        raise SettingsError(f"{label}: {max_n}개까지입니다")
    if any(len(x) > MAX_TEXT for x in out):
        raise SettingsError(f"{label}: 한 줄은 {MAX_TEXT}자까지입니다")
    return out


def _number(form: dict[str, Any], key: str, label: str, lo: float, hi: float) -> float:
    raw = str(form.get(key, "")).strip()
    try:
        v = float(raw)
    except ValueError as e:
        raise SettingsError(f"{label}: 숫자를 넣어 주세요") from e
    if not lo <= v <= hi:
        raise SettingsError(f"{label}: {lo:g}~{hi:g} 사이여야 합니다")
    return v


def _windows(form: dict[str, Any], key: str, label: str) -> list | None:
    raw = str(form.get(key, "")).strip()
    if not raw:
        return None
    try:
        return parse_windows(raw)
    except ValueError as e:
        raise SettingsError(f"{label}: {e}") from e


PERIODS = {"1d": "최근 1일", "1w": "최근 1주", "1m": "최근 1달", "all": "기간 없음(정확도순)"}


CARD_STYLE_NAMES = {
    "clean": "깔끔(기본): 브랜드 색 면 + 흰 카드",
    "photo": "사진형: 꽉 찬 어두운 배경 + 형광펜 제목 + 말풍선",
    "illust": "일러스트형: 파스텔 배경 + 번호 카드 + 캐릭터",
    "editorial": "사진 우선형(제안): 올린 사진을 크게 + 짧은 제목 + 설명 패널. 사진이 없으면 종이색 카드",
}


# 일러스트형 캐릭터(C17, ADR 0046). 순서대로 화면에 보인다
CHARACTER_NAMES = {
    "none": "없음(기본): 캐릭터 없이",
    "custom": '직접 올린 그림: 아래 "캐릭터·로고 그림"에서 올린 캐릭터',
    "drawn": "도형 캐릭터: 코드로 그린 동그란 캐릭터(브랜드 색)",
}


def _character(form: dict[str, Any], cfg_brand: Any) -> dict[str, Any]:
    """캐릭터 선택과 AI 표기. 폼에 칸이 없으면(옛 화면·테스트) 지금 값을 지킨다."""
    if "card_character" not in form:
        return {"character": cfg_brand.character, "character_ai_made": cfg_brand.character_ai_made}
    v = str(form["card_character"])
    if v not in CHARACTER_NAMES:
        raise SettingsError("캐릭터: 목록에 있는 것 중 하나를 고르세요")
    if v == "custom" and not (cfg_brand.character_cover or cfg_brand.character_outro):
        raise SettingsError(
            '캐릭터: 직접 올린 그림이 아직 없습니다. 아래 "캐릭터·로고 그림"에서 먼저 올려 주세요'
        )
    return {"character": v, "character_ai_made": form.get("character_ai_made") == "on"}


def _card_style(form: dict[str, Any], current: str) -> str:
    # 폼에 칸이 없으면(옛 화면·테스트) 지금 값을 지킨다
    v = str(form.get("card_style", current))
    if v not in CARD_STYLE_NAMES:
        raise SettingsError("카드 스타일: 깔끔·사진형·일러스트형·사진 우선형 중 하나를 고르세요")
    return v


def _period(form: dict[str, Any]) -> str:
    v = str(form.get("recent_period", "1w"))
    if v not in PERIODS:
        raise SettingsError("자료 검색 기간: 1일·1주·1달·기간 없음 중 하나를 고르세요")
    return v


def _aliases(form: dict[str, Any], current: dict[str, list[str]]) -> dict[str, list[str]]:
    """검색 별칭(S11): 한 줄에 "대표 주제: 별칭, 별칭". 칸이 없는 옛 폼은 지금 값을 지킨다."""
    if "picks_aliases" not in form:
        return current
    out: dict[str, list[str]] = {}
    for line in _lines(form, "picks_aliases", "검색 별칭"):
        if ":" not in line:
            raise SettingsError(
                f"검색 별칭: '대표 주제: 별칭1, 별칭2' 형식으로 적어 주세요 ({line})"
            )
        topic, rest = line.split(":", 1)
        names = [x.strip() for x in rest.split(",") if x.strip()]
        if topic.strip() and names:
            out[topic.strip()] = names
    return out


def _ai(form: dict[str, Any], cfg: AppConfig) -> dict[str, Any]:
    """AI 공급자·모델(S13, 전역). 칸이 없는 옛 폼은 지금 값을 지킨다."""
    s = cfg.settings
    out: dict[str, Any] = {"text_provider": s.text_provider, "models": s.models.model_dump()}
    if "text_provider" in form:
        v = str(form.get("text_provider", s.text_provider))
        if v not in TEXT_PROVIDER_NAMES:
            raise SettingsError("글 공급자: 자동·OpenAI·Claude 중 하나를 고르세요")
        out["text_provider"] = v
    for key, label in (("openai_text", "OpenAI 글 모델"), ("image", "이미지 모델")):
        if key in form:
            v = str(form.get(key, "")).strip()
            if not v or len(v) > 60 or any(ch.isspace() for ch in v):
                raise SettingsError(f"{label}: 모델 이름을 공백 없이 적어 주세요(예: gpt-6.1-sol)")
            out["models"][key] = v
    if "image_quality" in form:
        v = str(form.get("image_quality", ""))
        if v not in IMAGE_QUALITY_NAMES:
            raise SettingsError("이미지 품질: 낮음·보통·높음 중 하나를 고르세요")
        out["models"]["image_quality"] = v
    return out


def _picks(form: dict[str, Any], cfg: AppConfig) -> dict[str, Any]:
    """오늘의 추천(S11) 운영 설정(켜기만 전역). 칸이 없는 옛 폼은 지금 값을 지킨다."""
    cur = cfg.settings.picks
    if "picks_count" not in form:
        return cur.model_dump()
    return {**cur.model_dump(), "enabled": form.get("picks_enabled") == "on"}


def _workspace(form: dict[str, Any], cfg: AppConfig) -> dict[str, Any]:
    """작업 공간별 설정(S13): 추천 구성·검색 별칭. 칸이 없는 옛 폼은 지금 값을 지킨다."""
    cur = cfg.workspace
    out = cur.model_dump()
    if "picks_count" in form:
        out["info_count"] = int(_number(form, "picks_count", "하루 정보 추천 수", 0, 5))
    if "meme_count" in form:
        out["meme_count"] = int(_number(form, "meme_count", "하루 밈 추천 수", 0, 3))
    if out["info_count"] + out["meme_count"] > SLOT_TOTAL_MAX:
        raise SettingsError(f"하루 추천 수: 정보+밈 합계는 {SLOT_TOTAL_MAX}개까지입니다")
    out["aliases"] = _aliases(form, cur.aliases)
    return out


def apply_settings_form(cfg: AppConfig, form: dict[str, Any]) -> AppConfig:
    """폼 값을 반영한 새 설정. 형식이 틀리면 SettingsError(아무것도 바뀌지 않음)."""
    topics = _lines(form, "topics", "관심 주제", MAX_TOPICS)
    if not topics:
        raise SettingsError("관심 주제: 하나 이상 필요합니다")
    active = _windows(form, "active_hours", "활동 시간")
    if active is None:
        raise SettingsError("활동 시간: 비워 둘 수 없습니다 (예: 00:00-23:59)")
    persona = cfg.persona.model_dump()
    persona.update(
        name=_text(form, "persona_name", "페르소나 이름"),
        character=_text(form, "character", "인물상"),
        tone=_text(form, "tone", "말투"),
        topics=topics,
        banned_words=_lines(form, "banned_words", "금칙어"),
        banned_patterns=_lines(form, "banned_patterns", "금칙 패턴"),
        active_hours=active,
        ad_label=form.get("ad_label") == "on",
    )
    brand = cfg.brand.model_dump()
    brand.update(
        name=_text(form, "brand_name", "브랜드 이름"),
        industry=_text(form, "industry", "업종"),
        products=_lines(form, "products", "제품"),
        key_messages=_lines(form, "key_messages", "핵심 메시지"),
        primary_color=_text(form, "primary_color", "주 색"),
        accent_color=_text(form, "accent_color", "보조 색"),
        card_style=_card_style(form, cfg.brand.card_style),
        **_character(form, cfg.brand),
    )
    settings = cfg.settings.model_dump()
    settings.update(
        posting_hours=_windows(form, "posting_hours", "게시 시간"),
        quotas={
            **cfg.settings.quotas,
            "instagram": int(
                _number(form, "quota_instagram", "인스타그램 하루 게시 수", 0, QUOTA_MAX)
            ),
            "threads": int(_number(form, "quota_threads", "스레드 하루 게시 수", 0, QUOTA_MAX)),
        },
        daily_cost_cap_usd=_number(form, "daily_cost_cap_usd", "하루 비용 상한", 0.1, COST_CAP_MAX),
        min_hours_between_posts=_number(form, "min_hours_between_posts", "게시 간격", 0, 24),
        desktop_notifications=form.get("desktop_notifications") == "on",
        collect={
            **cfg.settings.collect.model_dump(),
            "recent_period": _period(form),
            # 칸이 없는 옛 폼은 지금 값을 지킨다(C15)
            "exclude_words": _lines(form, "exclude_words", "수집 제외어")
            if "exclude_words" in form
            else cfg.settings.collect.exclude_words,
        },
        reply_interval_minutes=int(
            _number(form, "reply_interval_minutes", "댓글 확인 간격", 3, 60)
        ),
        picks=_picks(form, cfg),
        **_ai(form, cfg),
        # 칸이 없는 옛 폼은 지금 값을 지킨다
        instagram_comment_interval_minutes=int(
            _number(form, IG_INTERVAL, "인스타그램 댓글 확인 간격", 10, 180)
        )
        if IG_INTERVAL in form
        else cfg.settings.instagram_comment_interval_minutes,
    )
    try:
        return AppConfig.model_validate(
            {
                **cfg.model_dump(),
                "persona": persona,
                "brand": brand,
                "settings": settings,
                "workspace": _workspace(form, cfg),
            }
        )
    except ValidationError as e:
        first = e.errors()[0]
        raise SettingsError(_friendly(first)) from e


def _friendly(err: dict[str, Any]) -> str:
    loc = [str(x) for x in err.get("loc", ())]
    names = {"primary_color": "주 색", "accent_color": "보조 색", "banned_patterns": "금칙 패턴"}
    where = next((names[x] for x in loc if x in names), None)
    msg = str(err.get("msg", "형식 오류")).removeprefix("Value error, ")
    # 모델 전체 검사(예: 금칙 패턴 정규식)는 메시지에 이미 어느 칸인지 들어 있다
    return f"{where}: {msg}" if where else msg


def form_values(cfg: AppConfig) -> dict[str, str]:
    """화면에 채워 넣을 현재 값(폼 이름 그대로)."""
    p, b, s = cfg.persona, cfg.brand, cfg.settings
    return {
        "persona_name": p.name,
        "character": p.character,
        "tone": p.tone,
        "topics": "\n".join(p.topics),
        "banned_words": "\n".join(p.banned_words),
        "banned_patterns": "\n".join(p.banned_patterns),
        "active_hours": format_windows(p.active_hours),
        "ad_label": "on" if p.ad_label else "",
        "brand_name": b.name,
        "industry": b.industry,
        "products": "\n".join(b.products),
        "key_messages": "\n".join(b.key_messages),
        "primary_color": b.primary_color,
        "accent_color": b.accent_color,
        "card_style": b.card_style,
        "card_character": b.character,
        "character_ai_made": "on" if b.character_ai_made else "",
        "posting_hours": format_windows(s.posting_hours) if s.posting_hours else "",
        "quota_instagram": str(s.quotas.get("instagram", 0)),
        "quota_threads": str(s.quotas.get("threads", 0)),
        "daily_cost_cap_usd": f"{s.daily_cost_cap_usd:g}",
        "min_hours_between_posts": f"{s.min_hours_between_posts:g}",
        "reply_interval_minutes": str(s.reply_interval_minutes),
        IG_INTERVAL: str(s.instagram_comment_interval_minutes),
        "desktop_notifications": "on" if s.desktop_notifications else "",
        "recent_period": s.collect.recent_period,
        "exclude_words": "\n".join(s.collect.exclude_words),
        "text_provider": s.text_provider,
        "openai_text": s.models.openai_text,
        "image": s.models.image,
        "image_quality": s.models.image_quality,
        "picks_enabled": "on" if s.picks.enabled else "",
        "picks_count": str(cfg.workspace.info_count),
        "meme_count": str(cfg.workspace.meme_count),
        "picks_aliases": "\n".join(
            f"{k}: {', '.join(v)}" for k, v in cfg.workspace.aliases.items()
        ),
    }
