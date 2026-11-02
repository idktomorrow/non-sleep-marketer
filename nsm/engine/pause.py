"""플랫폼 자동 게시·답글 멈춤의 이유(배치 E4, ADR 0054 개정).

멈춘 플랫폼 목록(`PAUSED_KEY`)은 그대로 두고(게시·답글·감시가 읽는다), 이유와 시각은 옆 설정값에 따로 둔다.
화면·알림·실행 기록은 이 이유를 그대로 쓴다. 이유가 없는 예전 멈춤은 "이유 미기록"으로 보인다.

이유: `auth`(로그인 풀림·계정 불일치), `transient`(상태 확인이 연속으로 일시적 실패), `ui_changed`·
`config_or_bug`(상태 확인 실패), `manual`(사람이 멈춤, 지금은 쓰는 곳 없이 표시만 준비).

E5(ADR 0060): `auth`는 세부 상태(`auth_state`: 로그아웃·본인 확인·문자 인증·두 단계 인증·계정 제한·원인 모름)로
나눠 사람이 할 일을 다르게 말한다. 같은 상태가 이어지는 동안 알림은 처음 한 번과 `notified_at`부터 24시간마다
한 번, 상태가 바뀌면 바로 다시 알린다.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from nsm.domain.errors import short_label

# {플랫폼: {"reason", "since", "detail", "failures", "auth_state"(E5), "notified_at"(E5)}}
PAUSE_INFO_KEY = "paused_platforms_info"
HEALTH_STREAK_KEY = "health_failure_streak"  # {플랫폼: 연속 일시적 실패 수}
REMIND_EVERY = timedelta(hours=24)  # 같은 멈춤 상태의 알림 간격

# 로그인 세부 상태별 (짧은 이름, 사람이 할 일, 알림 본문). {name}은 플랫폼 이름(E5, ADR 0060)
AUTH_STATE_TEXT: dict[str, tuple[str, str, str]] = {
    "logged_out": (
        "로그인 풀림",
        "Aside 브라우저에서 다시 로그인하면 풀림",
        "{name} 로그인이 풀렸습니다. Aside 브라우저에서 다시 로그인하세요. "
        "로그인되면 다음 점검(30분 이내)에 자동으로 다시 시작합니다.",
    ),
    "challenge": (
        "본인 확인 창",
        "Aside 브라우저에서 직접 본인 확인을 끝내면 풀림",
        "{name}이 본인 확인 창을 띄웠습니다. Aside 브라우저에서 직접 확인을 끝내 주세요. 자동으로 풀지 않습니다.",
    ),
    "verify": (
        "문자·이메일 인증 요구",
        "문자·이메일 인증을 마치면 풀림",
        "{name}이 문자·이메일 인증을 요구합니다. 인증을 마치면 다음 점검(30분 이내)에 자동으로 다시 시작합니다.",
    ),
    "two_factor": (
        "두 단계 인증 요구",
        "Aside 브라우저에서 인증 코드를 넣으면 풀림",
        "{name}이 두 단계 인증 코드를 요구합니다. Aside 브라우저에서 코드를 넣어 주세요. "
        "마치면 다음 점검(30분 이내)에 자동으로 다시 시작합니다.",
    ),
    "restricted": (
        "계정 제한",
        "{name} 앱에서 계정 상태를 확인해야 함",
        "{name} 계정이 제한됐습니다. {name} 앱에서 상태를 확인하세요. 자동으로 풀지 않습니다.",
    ),
    "unknown": (
        "로그인 필요(원인 모름)",
        "Aside 브라우저에서 화면을 확인하고 로그인하면 풀림",
        "{name} 로그인이 안 돼 있는데 화면으로 원인을 알아보지 못했습니다. Aside 브라우저에서 {name}을 열어 확인하세요.",
    ),
}


def _state_text(info: dict[str, Any] | None, platform: str = "") -> tuple[str, str, str] | None:
    """auth 멈춤의 세부 상태 문구. 상태가 없는 예전 멈춤은 None(예전 문구 그대로)."""
    if (info or {}).get("reason") != "auth":
        return None
    t = AUTH_STATE_TEXT.get(str((info or {}).get("auth_state") or ""))
    if t is None:
        return None
    from nsm.engine.rate_limit import NAMES

    name = NAMES.get(platform, platform)
    batchim = _has_batchim(name)
    subs = {
        "{name}이": name + ("이" if batchim else "가"),
        "{name}을": name + ("을" if batchim else "를"),
    }

    def fill(x: str) -> str:
        for k, v in subs.items():
            x = x.replace(k, v)
        return x.replace("{name}", name)

    return tuple(fill(x) for x in t)  # type: ignore[return-value]


def _has_batchim(word: str) -> bool:
    c = word[-1:] or "a"
    return "가" <= c <= "힣" and (ord(c) - 0xAC00) % 28 != 0


def _ro(word: str) -> str:
    """'로'/'으로' 조사(받침 없음·ㄹ받침이면 '로'). 끝의 괄호 설명은 빼고 본다: "로그인 필요(원인 모름)"."""
    w = word.split("(")[0].strip() if word.endswith(")") else word
    c = w[-1:] or "a"
    if "가" <= c <= "힣" and (ord(c) - 0xAC00) % 28 not in (0, 8):
        return f"{word}으로"
    return f"{word}로"


def todo_text(info: dict[str, Any] | None, platform: str = "") -> str:
    """사람이 할 일 한 줄(오늘 화면). 세부 상태가 없으면 빈 문자열."""
    t = _state_text(info, platform)
    return t[1] if t else ""


def auth_state_of(obj: Any) -> str | None:
    """어댑터·오류가 붙인 로그인 세부 상태(포트 동결이라 속성으로 읽는다). 모르는 값은 None."""
    s = getattr(obj, "auth_state", None)
    return s if s in AUTH_STATE_TEXT else None


def remind_due(info: dict[str, Any], now: datetime) -> bool:
    """로그인 세부 상태로 멈춘 채 마지막 알림에서 24시간이 지났나.

    다시 알리는 것은 로그인 세부 상태가 있는 `auth` 멈춤뿐이다(E5 범위). 마지막 알림 시각이 없는 멈춤(E5 전)은
    호출하는 쪽이 지금 시각을 적어 두고 거기서부터 센다(올리자마자 알림이 쏟아지지 않게).
    """
    if info.get("reason") != "auth" or not info.get("auth_state") or not info.get("notified_at"):
        return False
    try:
        return now - datetime.fromisoformat(info["notified_at"]) >= REMIND_EVERY
    except (TypeError, ValueError):
        return False


def pause_info(repo: Any) -> dict[str, dict[str, Any]]:
    raw = repo.get_setting(PAUSE_INFO_KEY, {}) or {}
    return (
        {k: dict(v) for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}
    )


def short_reason(info: dict[str, Any] | None) -> str:
    """짧은 이유: "로그인 필요", "상태 확인 2회 연속 실패(일시적)", "수동", "이유 미기록"."""
    reason = (info or {}).get("reason")
    if reason == "auth":
        t = _state_text(info)
        return t[0] if t else "로그인 필요"
    if reason == "transient":
        n = (info or {}).get("failures")
        return (
            f"상태 확인 {n}회 연속 실패(일시적)"
            if isinstance(n, int) and n > 1
            else "상태 확인 실패(일시적)"
        )
    if reason == "manual":
        return "수동으로 멈춤"
    if reason in ("ui_changed", "config_or_bug", "rate_limited"):
        return f"상태 확인 실패({short_label(reason)})"
    return "이유 미기록"


def reason_text(
    info: dict[str, Any] | None,
    fmt_time: Callable[[datetime], str] | None = None,
    platform: str = "",
) -> str:
    """장애 화면·"오늘" 줄에 쓰는 문장."""
    reason = (info or {}).get("reason")
    # health는 점검이 성공하면 이유와 상관없이 푼다(수동 멈춤도 마찬가지)
    tail = "다시 로그인하면 풀림" if reason == "auth" else "다음 상태 점검에서 정상이면 풀림"
    if t := _state_text(info, platform):
        tail = t[1]
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
        head = f"{_ro(short)} 자동 게시·답글 멈춤"
    return f"{head}({tail}{since})"


def pause_notice(platform: str, info: dict[str, Any], detail: str) -> tuple[str, str]:
    """멈출 때 보내는 알림의 (제목, 본문). 이유를 그대로 말한다."""
    reason = info.get("reason")
    if t := _state_text(info, platform):
        return f"{platform} {t[0]}", f"{t[2]} ({detail})"
    if reason == "auth":
        return (
            f"{platform} 로그인 필요",
            f"{platform} 자동 게시·답글을 멈췄습니다. 브라우저에서 다시 로그인하세요. ({detail})",
        )
    return (
        f"{platform} 상태 확인 실패",
        f"{platform} {_ro(short_reason(info))} 자동 게시·답글을 멈췄습니다. "
        f"다음 상태 점검에서 정상이면 다시 시작합니다. ({detail})",
    )
