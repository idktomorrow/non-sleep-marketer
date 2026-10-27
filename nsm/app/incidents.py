"""대시보드 "장애" 화면(배치 E2, ADR 0055): 실패 기록(failures)과 지금 상태를 사람이 읽는 형태로.

지금 상태는 저장된 표가 아니라 그 순간의 사실에서 계산한다:
- 쉬는 중: 어댑터의 `blocked_until`(요청 제한, ADR 0038)
- 멈춤: health가 멈춘 플랫폼(`PAUSED_KEY`)과 멈출 때 저장한 이유(`PAUSE_INFO_KEY`, E4)
- 재시도 대기: 실패해 다음 시각을 기다리는 게시
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from nsm.adapters import evidence as _evidence
from nsm.adapters.diagnosis import screen_summary
from nsm.domain.errors import ERROR_KINDS, POLICIES
from nsm.engine.pause import pause_info, reason_text
from nsm.engine.publish import PAUSED_KEY
from nsm.engine.rate_limit import NAMES, blocked_until

SUMMARY_DAYS = 7


@dataclass
class Ongoing:
    kind: str
    platform: str
    text: str  # "스레드: 요청 제한으로 14:00까지 쉬는 중"


@dataclass
class KindRow:
    kind: str
    label: str
    count: int
    first: datetime | None
    last: datetime | None
    state: str  # "정상" 또는 진행 중 문장들
    ongoing: bool
    action: str


def _name(platform: str) -> str:
    return NAMES.get(platform, platform)


def ongoing(state: Any, now: datetime, fmt_time: Callable[[datetime], str]) -> list[Ongoing]:
    """지금 진행 중인 장애(없으면 빈 목록)."""
    out: list[Ongoing] = []
    resting = set()
    for platform, adapter in state.adapters.items():
        if until := blocked_until(adapter, now):
            resting.add(platform)
            out.append(
                Ongoing(
                    "rate_limited",
                    platform,
                    f"{_name(platform)}: 요청 제한으로 {fmt_time(until)}까지 쉬는 중",
                )
            )
    info = pause_info(state.repo)
    for platform in state.repo.get_setting(PAUSED_KEY, []) or []:
        # 멈출 때 저장한 이유를 그대로 쓴다(E4). 이유가 없으면 추정하지 않고 "이유 미기록"
        i = info.get(platform)
        reason = (i or {}).get("reason")
        kind = reason if reason in ERROR_KINDS else ""
        out.append(Ongoing(kind, platform, f"{_name(platform)}: {reason_text(i, fmt_time)}"))
    waiting: dict[str, datetime] = {}
    for p in state.repo.publications_in_status("failed"):
        due = p.next_attempt_at
        if due is None or due <= now or p.platform in resting:
            continue
        if p.platform not in waiting or due < waiting[p.platform]:
            waiting[p.platform] = due
    for platform, at in sorted(waiting.items()):
        kind = state.repo.latest_failure_kind(platform, "publish") or "transient"
        out.append(
            Ongoing(kind, platform, f"{_name(platform)}: 게시 재시도 대기(다음 {fmt_time(at)})")
        )
    return out


def kind_rows(state: Any, now: datetime, live: list[Ongoing]) -> list[KindRow]:
    summary = state.repo.failure_summary(now - timedelta(days=SUMMARY_DAYS))
    rows = []
    for kind in ERROR_KINDS:
        s = summary.get(kind, {})
        mine = [o.text for o in live if o.kind == kind]
        rows.append(
            KindRow(
                kind=kind,
                label=POLICIES[kind].label,
                count=int(s.get("count", 0)),
                first=s.get("first"),
                last=s.get("last"),
                state=" / ".join(mine) if mine else "정상",
                ongoing=bool(mine),
                action=s.get("action") or "-",
            )
        )
    return rows


def evidence_path(failure: dict[str, Any], root: Path | None = None) -> Path | None:
    """증거 폴더가 실제로 shots 폴더 안에 있을 때만(다른 경로를 서빙하지 않게)."""
    d = failure.get("evidence_dir")
    if not d:
        return None
    base = (root or _evidence.shots_dir()).resolve()
    try:
        p = Path(d).resolve()
    except OSError:
        return None
    if not p.is_dir() or base not in p.parents:
        return None
    return p


def detail(failure: dict[str, Any]) -> dict[str, Any]:
    p = evidence_path(failure)
    info = screen_summary(p) if p else {}
    return {
        "evidence": p,
        "has_shot": bool(p and (p / "screenshot.png").is_file()),
        "dialogs": info.get("dialogs"),
        "url": info.get("url"),
        "labels": (info.get("labels") or [])[:30],
        "text": str(info.get("text") or "")[:600],
    }
