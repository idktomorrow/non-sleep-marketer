"""대시보드 설정 화면(M17): 폼 → 새 AppConfig. 라우트는 server.py, 여기는 검사와 변환만(테스트하기 쉽게).

API 키·게시 계정·드라이런은 여기서 바꾸지 않는다. 키는 keyring, 계정과 드라이런은 `nsm init`(ADR 0028).
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from nsm.config.schema import AppConfig
from nsm.config.store import format_windows, parse_windows

MAX_TOPICS = 10
MAX_LINES = 30
MAX_TEXT = 500
QUOTA_MAX = 10
COST_CAP_MAX = 100.0


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
        reply_interval_minutes=int(
            _number(form, "reply_interval_minutes", "댓글 확인 간격", 3, 60)
        ),
    )
    try:
        return AppConfig.model_validate(
            {**cfg.model_dump(), "persona": persona, "brand": brand, "settings": settings}
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
        "posting_hours": format_windows(s.posting_hours) if s.posting_hours else "",
        "quota_instagram": str(s.quotas.get("instagram", 0)),
        "quota_threads": str(s.quotas.get("threads", 0)),
        "daily_cost_cap_usd": f"{s.daily_cost_cap_usd:g}",
        "min_hours_between_posts": f"{s.min_hours_between_posts:g}",
        "reply_interval_minutes": str(s.reply_interval_minutes),
        "desktop_notifications": "on" if s.desktop_notifications else "",
    }
