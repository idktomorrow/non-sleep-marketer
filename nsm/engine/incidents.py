"""실패 기록과 자동 진단(배치 E2, ADR 0055).

엔진(게시·답글·health·지표)이 실패를 오류 종류 표(ADR 0054)대로 처리한 뒤 `record_incident`를 부른다.
- `failures` 표에 한 줄: 시각·플랫폼·잡·단계·종류·메시지·증거 폴더·취한 조치·진단 문장.
- 화면 변경(그리고 어댑터에서 난 설정·버그 오류)이고 증거가 있으면 Claude가 원인 추정 두 문장을 쓴다.
  플랫폼·종류당 6시간에 1회, 하루 5회, 하루 비용 상한 안에서만. 실패하거나 상한이면 진단 없이 넘어간다.
기록·진단이 실패해도 원래 실패 처리(재시도·알림)는 막지 않는다.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from nsm.domain.errors import evidence_dir, kind_of
from nsm.domain.policy import local_day_bounds, under_cost_cap
from nsm.engine.context import JobContext, is_interrupted

log = logging.getLogger(__name__)

DIAGNOSE_KINDS = ("ui_changed", "config_or_bug")
DIAGNOSE_EVERY = timedelta(hours=6)  # "화면 변경" 알림 중복 제거와 같은 간격(E1)
DIAGNOSE_PER_DAY = 5


def _can_diagnose(ctx: JobContext, platform: str, kind: str, where: str | None) -> str | None:
    """진단해도 되면 None, 아니면 이유(로그용)."""
    if ctx.diagnoser is None:
        return "진단기 없음(Claude 키)"
    if kind not in DIAGNOSE_KINDS:
        return "대상 종류 아님"
    if not where or not Path(where).is_dir():
        return "증거 없음"  # config_or_bug는 어댑터가 증거를 남긴 것(어댑터에서 난 것)만
    now = ctx.clock.now()
    last = ctx.repo.last_diagnosis_at(platform, kind)
    if last is not None and now - last < DIAGNOSE_EVERY:
        return "6시간 안에 이미 진단함"
    start, end = local_day_bounds(now, ctx.tz)
    if ctx.repo.count_diagnoses_between(start, end) >= DIAGNOSE_PER_DAY:
        return "오늘 진단 상한(5회)"
    if not under_cost_cap(
        ctx.repo.cost_between(start, end), ctx.config.settings.daily_cost_cap_usd
    ):
        return "오늘 비용 상한"
    return None


def record_incident(
    ctx: JobContext, platform: str, job: str, stage: str, exc: BaseException, action: str
) -> str | None:
    """실패를 남기고, 할 수 있으면 진단한다. 진단 문장(없으면 None)을 돌려준다 — 알림 끝에 붙인다."""
    if is_interrupted(ctx, exc):
        return None  # 끄는 순간 끊긴 것은 실패가 아니다(C33): 기록·진단하지 않는다
    kind = kind_of(exc)
    where = evidence_dir(exc)
    try:
        fid = ctx.repo.record_failure(
            platform=platform,
            job=job,
            stage=stage,
            kind=kind,
            message=str(exc)[:1000],
            evidence_dir=where,
            action=action,
        )
    except Exception as e:  # noqa: BLE001  기록 실패가 실패 처리를 막지 않게
        log.warning("실패 기록 저장 실패: %s", e)
        return None
    why = _can_diagnose(ctx, platform, kind, where)
    if why is not None:
        log.debug("진단 건너뜀(%s): %s", platform, why)
        return None
    try:
        ctx.repo.mark_diagnosis_attempt(fid)  # 실패한 시도도 상한에 센다
        text = ctx.diagnoser.diagnose(platform, str(exc), Path(where))  # type: ignore[union-attr,arg-type]
    except Exception as e:  # noqa: BLE001  진단이 실패해도 알림은 진단 없이 그대로
        log.warning("자동 진단 실패(%s): %s", platform, e)
        return None
    try:
        ctx.repo.set_failure_diagnosis(fid, text)
    except Exception as e:  # noqa: BLE001
        log.warning("진단 저장 실패: %s", e)
    return text


def with_diagnosis(body: str, diagnosis: str | None) -> str:
    return f"{body} · {diagnosis}" if diagnosis else body
