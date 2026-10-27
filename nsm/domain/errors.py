"""오류 종류 표(배치 E1, ADR 0054): 흩어져 있던 실패 처리를 한 표로 묶는다.

엔진(게시·답글·health·지표)은 예외를 직접 if문으로 가르지 않고 `policy_for(e)`로 이 표를 읽는다.
포트(`nsm/ports`)는 동결이라 `AdapterError`를 바꾸지 않고, 어댑터가 던지는 하위 클래스의 `kind` 속성을
읽는다(요청 제한의 `retry_at`과 같은 방식, ADR 0038).

이 표는 "자동으로 하는 것"(재시도·쉬기·우회·사람에게)만 정한다. 코드가 스스로 화면 처리를 고치는 일은
하지 않는다: 게시는 되돌릴 수 없어서, 잘못 고친 재시도는 두 번 올리기가 된다(C23·C24 "두 번 올리는 것보다
한 번 빠지는 쪽").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, get_args

ErrorKind = Literal["transient", "rate_limited", "auth", "ui_changed", "config_or_bug"]
ERROR_KINDS: tuple[ErrorKind, ...] = get_args(ErrorKind)

# retry: schedule = 설정의 간격(retry_delays_minutes, 기본 15·60·240분) 뒤 다시, 다 쓰면 포기하고 검토로
#        until    = 플랫폼이 정한 시각까지 쉬었다 다시(재시도 횟수를 쓰지 않음)
#        none     = 다시 하지 않음(해도 같다). 증거를 남기고 바로 검토로
RetryMode = Literal["schedule", "until", "none"]


@dataclass(frozen=True)
class ErrorPolicy:
    kind: ErrorKind
    label: str  # 사람에게 보이는 이름
    retry: RetryMode
    pause_on_health_failure: (
        bool  # health 점검이 이 종류로 실패하면 그 플랫폼의 자동 게시·답글을 멈추나
    )
    notify: str  # 언제 알리나(사람이 읽는 설명)
    to_human: str  # 언제 사람에게 넘기나(사람이 읽는 설명)
    evidence: bool  # 실패 순간 화면·대화상자·로그를 남기나


POLICIES: dict[ErrorKind, ErrorPolicy] = {
    "transient": ErrorPolicy(
        kind="transient",
        label="일시적(네트워크·시간 초과)",
        retry="schedule",
        pause_on_health_failure=True,
        notify="재시도를 다 쓰고 포기할 때",
        to_human="재시도를 다 쓰면 원고를 검토로 되돌림. 답글은 그 댓글을 사람 확인으로",
        evidence=True,
    ),
    "rate_limited": ErrorPolicy(
        kind="rate_limited",
        label="요청 제한(HTTP 429)",
        retry="until",
        pause_on_health_failure=False,
        notify="쉬는 구간마다 한 번",
        to_human="넘기지 않음(쉬었다가 자동으로 다시)",
        evidence=False,
    ),
    "auth": ErrorPolicy(
        kind="auth",
        label="로그인·권한",
        retry="schedule",
        pause_on_health_failure=True,
        notify='health 점검이 플랫폼을 멈출 때("로그인 필요")',
        to_human="바로: 브라우저에서 다시 로그인해야 함. 로그인되면 health가 다시 켬",
        evidence=True,
    ),
    "ui_changed": ErrorPolicy(
        kind="ui_changed",
        label="외부 화면 변경(버튼·대화상자를 못 찾음)",
        retry="none",
        pause_on_health_failure=True,
        notify="바로",
        to_human="바로: 원고를 검토로 되돌리고 증거 폴더를 알림에 붙임",
        evidence=True,
    ),
    "config_or_bug": ErrorPolicy(
        kind="config_or_bug",
        label="설정 누락·프로그램 오류",
        retry="schedule",
        pause_on_health_failure=True,
        notify="재시도를 다 쓰고 포기할 때",
        to_human="재시도를 다 쓰면 원고를 검토로 되돌림",
        evidence=True,
    ),
}


def short_label(kind: str) -> str:
    """화면용 짧은 이름(괄호 앞): "외부 화면 변경(버튼…)" → "외부 화면 변경"."""
    p = POLICIES.get(kind)  # type: ignore[call-overload]
    return p.label.split("(")[0] if p else kind


class KindedError(Exception):
    """엔진이 예외 없이 알게 된 실패(예: health가 '로그인 안 됨'을 돌려줌)를 표에 기록할 때 쓴다."""

    def __init__(self, message: str, kind: ErrorKind) -> None:
        super().__init__(message)
        self.kind = kind


def kind_of(exc: BaseException) -> ErrorKind:
    """예외의 종류. 어댑터가 붙인 `kind`가 있으면 그것, 요청 제한 시각이 있으면 rate_limited.

    종류가 없는 외부 동작 실패(어댑터가 아닌 곳의 AdapterError, 네트워크 예외)는 일시적으로 본다.
    그 밖의 파이썬 예외(KeyError 등)는 프로그램 오류다. 둘 다 재시도 정책은 같아 지금 동작이 바뀌지 않는다.
    """
    k = getattr(exc, "kind", None)
    if k in ERROR_KINDS:
        return k  # type: ignore[return-value]
    if isinstance(getattr(exc, "retry_at", None), datetime):
        return "rate_limited"
    from nsm.ports import AdapterError

    if isinstance(exc, (AdapterError, OSError, TimeoutError, ConnectionError)):
        return "transient"
    if "Timeout" in type(exc).__name__ or "Connection" in type(exc).__name__:
        return "transient"
    return "config_or_bug"


def policy_for(exc: BaseException) -> ErrorPolicy:
    return POLICIES[kind_of(exc)]


def evidence_dir(exc: BaseException) -> str | None:
    """어댑터가 남긴 증거 폴더 경로(없으면 None)."""
    d = getattr(exc, "evidence_dir", None)
    return str(d) if d else None
