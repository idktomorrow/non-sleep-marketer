"""플랫폼 자동 게시·답글 멈춤의 이유(배치 E4, ADR 0054 개정).

멈춘 플랫폼 목록(`PAUSED_KEY`)은 그대로 두고(게시·답글·감시가 읽는다), 이유와 시각은 옆 설정값에 따로 둔다.
화면·알림·실행 기록은 이 이유를 그대로 쓴다. 이유가 없는 예전 멈춤은 "이유 미기록"으로 보인다.

이유: `auth`(로그인 풀림·계정 불일치), `transient`(상태 확인이 연속으로 일시적 실패), `ui_changed`·
`config_or_bug`(상태 확인 실패), `manual`(사람이 멈춤).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from nsm.domain.errors import short_label

PAUSE_INFO_KEY = "paused_platforms_info"  # {플랫폼: {"reason", "since", "detail", "failures"}}
HEALTH_STREAK_KEY = "health_failure_streak"  # {플랫폼: 연속 일시적 실패 수}


def pause_info(repo: Any) -> dict[str, dict[str, Any]]:
    raw = repo.get_setting(PAUSE_INFO_KEY, {}) or {}
    return (
        {k: dict(v) for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}
    )


def short_reason(info: dict[str, Any] | None) -> str:
    """짧은 이유: "로그인 필요", "상태 확인 2회 연속 실패(일시적)", "수동", "이유 미기록"."""
    reason = (info or {}).get("reason")
    if reason == "auth":
        return "로그인 필요"
    if reason == "transient":
        return f"상태 확인 {(info or {}).get('failures') or 2}회 연속 실패(일시적)"
    if reason == "manual":
        return "수동으로 멈춤"
    if reason in ("ui_changed", "config_or_bug", "rate_limited"):
        return f"상태 확인 실패({short_label(reason)})"
    return "이유 미기록"


def reason_text(
    info: dict[str, Any] | None, fmt_time: Callable[[datetime], str] | None = None
) -> str:
    """장애 화면·"오늘" 줄에 쓰는 문장."""
    reason = (info or {}).get("reason")
    tail = {"auth": "다시 로그인하면 풀림", "manual": "사람이 다시 켜야 풀림"}.get(
        reason or "", "다음 점검에서 정상이면 풀림"
    )
    since = ""
    if fmt_time and (info or {}).get("since"):
        try:
            since = f", {fmt_time(datetime.fromisoformat(info['since']))}부터"  # type: ignore[index]
        except (TypeError, ValueError):
            since = ""
    short = short_reason(info)
    if reason == "manual":
        head = "수동으로 자동 게시·답글 멈춤"
    elif short == "이유 미기록":
        head = "자동 게시·답글 멈춤, 이유 미기록"
    else:
        head = f"{short}로 자동 게시·답글 멈춤"
    return f"{head}({tail}{since})"


def pause_notice(platform: str, info: dict[str, Any], detail: str) -> tuple[str, str]:
    """멈출 때 보내는 알림의 (제목, 본문). 이유를 그대로 말한다."""
    reason = info.get("reason")
    if reason == "auth":
        return (
            f"{platform} 로그인 필요",
            f"{platform} 자동 게시·답글을 멈췄습니다. 브라우저에서 다시 로그인하세요. ({detail})",
        )
    return (
        f"{platform} 상태 확인 실패",
        f"{platform} {short_reason(info)}로 자동 게시·답글을 멈췄습니다. "
        f"다음 점검(30분 안)에서 정상이면 다시 시작합니다. ({detail})",
    )
