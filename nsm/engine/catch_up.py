"""하루·주 한 번 잡 따라잡기(C40, ADR 0066, incidents/0013).

예정 시각에 프로그램이 꺼져 있었거나 PC가 절전이면 그날 백업·문서 색인·성과 읽기·주간 리포트가 통째로 빠졌다.
APScheduler는 꺼져 있던 동안의 예약을 모르고, 절전도 유예(misfire_grace_time)보다 길면 버린다. 그래서 켤 때와
30분마다 "오늘 예정 시각이 지났는데 그 뒤로 끝난 실행이 없는 잡"을 찾아 작업 스레드에 한 번씩 띄운다.

- 놓침 기준: 현지 시간대로 오늘 예정 시각(주간 리포트는 이번 주 월요일 08:00)이 지났고, 예정 1시간 전 이후 그 잡의
  끝난 실행이 없다. 실패로 끝난 실행도 "돌았다"로 본다(같은 날 다시 하지 않는다). 종료 중단(`INTERRUPTED_PREFIX`)과
  끝나지 않은 실행(프로그램이 실행 중에 꺼짐)은 돌지 않은 것으로 본다.
- 예정 시각에서 지터(트리거의 무작위 지연)+5분이 지나야 놓침으로 본다. 정상으로 켜져 있을 때 예약 실행보다 먼저
  따라잡기가 돌아 하루 두 번 백업하지 않게.
- 같은 프로세스 안에서는 하루에 한 번만 띄운다(`TRIED_KEY`에 현지 날짜·기동 id·잡). 띄운 뒤 실패하거나 기다리는 중이어도
  30분마다 다시 띄우지 않는다. 다음 기동은 기동 id가 달라 다시 판단한다(실행 전·중에 꺼진 것은 실행 기록이 없거나 중단이라
  다시 띄우고, 실패로 끝난 것은 기록이 있어 띄우지 않는다).
- 실제로 돌리기 직전에 끄는 중이면 하지 않고, 그새 예약 실행이 끝났으면(절전에서 깨며 APScheduler가 스스로 돌린 경우)
  건너뛴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from nsm.store.db import to_db

CATCH_UP_JOBS = ("maintenance", "index", "metrics", "weekly_report")
TRIED_KEY = "catch_up_tried"  # {"day": "2026-11-07", "boot": "<기동 id>", "jobs": ["maintenance"]}
RAN_WINDOW = timedelta(hours=1)  # 예정 1시간 전 이후의 실행만 "오늘 그 일"로 본다
MARGIN = timedelta(minutes=5)
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class Slot:
    """하루(또는 주) 한 번 잡의 예정 시각. 스케줄러 트리거와 따라잡기가 같은 값을 쓴다."""

    hour: int
    minute: int
    jitter: int  # 초. 트리거가 이만큼까지 늦게 돌 수 있다
    weekday: int | None = None  # 0 = 월요일. None이면 매일

    @property
    def label(self) -> str:
        hm = f"{self.hour:02d}:{self.minute:02d}"
        return hm if self.weekday is None else f"{'월화수목금토일'[self.weekday]} {hm}"

    @property
    def day_of_week(self) -> str | None:
        return None if self.weekday is None else WEEKDAYS[self.weekday]


def fixed_slots(cfg: Any) -> dict[str, Slot]:
    """정해진 시각에 도는 잡(수집·생성처럼 활동 시간을 따르는 것은 빼고)."""
    idx_h, idx_m = (int(x) for x in cfg.settings.rag.index_time.split(":"))
    return {
        "metrics": Slot(3, 10, 900),
        "weekly_report": Slot(8, 0, 600, weekday=0),
        # 데이터 백업·실행 기록 정리(C12). 지표(03:10) 뒤, 다른 작업이 드문 새벽
        "maintenance": Slot(4, 30, 600),
        # 문서 색인(R2): Gemini 무료 하루 한도가 풀린 뒤(태평양 자정 = 한국 오후 4~5시) 하루 한 번
        "index": Slot(idx_h, idx_m, 600),
    }


def scheduled_at(slot: Slot, now: datetime, tz: str) -> datetime:
    """now가 속한 현지 날짜(주간이면 그 주 월요일)의 예정 시각(aware)."""
    local = now.astimezone(ZoneInfo(tz))
    day = local.date()
    if slot.weekday is not None:
        day -= timedelta(days=(day.weekday() - slot.weekday) % 7)
    return datetime.combine(day, time(slot.hour, slot.minute), tzinfo=ZoneInfo(tz))


def missed_slot(slot: Slot, now: datetime, tz: str) -> datetime | None:
    """now 기준으로 판단할 예정 시각: 오늘(이번 주) 것이 기다림(지터+5분)까지 지났으면 그것. 아직이면, 바로 전
    회차의 기다림이 자정을 넘어 오늘로 넘어온 경우(예: 23:50 예정)만 그 회차. 그 밖엔 None(C40 리뷰)."""
    wait = timedelta(seconds=slot.jitter) + MARGIN
    at = scheduled_at(slot, now, tz)
    if now >= at + wait:
        return at
    prev = at - timedelta(days=7 if slot.weekday is not None else 1)
    today = now.astimezone(ZoneInfo(tz)).date()
    if now >= prev + wait and (prev + wait).astimezone(ZoneInfo(tz)).date() == today:
        return prev
    return None


def ran_since(repo: Any, job: str, missed_at: datetime) -> bool:
    """그 회차(예정 1시간 전 이후)에 끝난 실행이 있나."""
    return _ran_since(repo, job, missed_at - RAN_WINDOW)


def prefix_for(slot: Slot) -> str:
    return f"따라잡기({slot.label} 놓침) · "


def _ran_since(repo: Any, job: str, since: datetime) -> bool:
    from nsm.engine.runner import INTERRUPTED_PREFIX

    n = repo.db.execute(
        "SELECT count(*) FROM run_logs WHERE job = ? AND started_at >= ? AND finished_at IS NOT NULL "
        "AND NOT starts_with(coalesce(message, ''), ?)",
        [job, to_db(since), INTERRUPTED_PREFIX],
    )[0][0]
    return int(n) > 0


def _local_day(now: datetime, tz: str) -> str:
    return now.astimezone(ZoneInfo(tz)).date().isoformat()


def _tried(repo: Any, now: datetime, tz: str, boot: str) -> set[str]:
    v = repo.get_setting(TRIED_KEY, {}) or {}
    if isinstance(v, dict) and v.get("day") == _local_day(now, tz) and v.get("boot") == boot:
        return set(v.get("jobs") or [])
    return set()


def mark_tried(repo: Any, now: datetime, tz: str, boot: str, jobs: list[str]) -> None:
    done = _tried(repo, now, tz, boot) | set(jobs)
    repo.set_setting(TRIED_KEY, {"day": _local_day(now, tz), "boot": boot, "jobs": sorted(done)})


def due_catch_ups(
    cfg: Any, repo: Any, now: datetime, boot: str | None = None
) -> list[tuple[str, datetime]]:
    """[(잡, 놓친 예정 시각)], 설정 순서대로. boot가 있으면 이 기동에서 오늘 이미 띄운 잡은 뺀다."""
    s = cfg.settings
    slots = fixed_slots(cfg)
    tried = _tried(repo, now, s.timezone, boot) if boot else set()
    out = []
    for job in s.catch_up_jobs:
        slot = slots.get(job)
        if slot is None or job in tried:
            continue
        at = missed_slot(slot, now, s.timezone)
        if at is None:
            continue  # 아직 예정 전이거나, 예약 실행이 지터로 늦게 돌 수 있는 동안
        if ran_since(repo, job, at):
            continue
        out.append((job, at))
    return out


def queue_catch_ups(sched: Any, cfg: Any, repo: Any, now: datetime, boot: str) -> list[str]:
    """놓친 잡을 1분 간격으로 작업 스레드에 예약한다. 띄우기 전에 오늘 시도로 적는다(실패해도 같은 날 다시 띄우지 않게)."""
    due = due_catch_ups(cfg, repo, now, boot)
    if not due:
        return []
    jobs = [j for j, _ in due]
    mark_tried(repo, now, cfg.settings.timezone, boot, jobs)
    slots = fixed_slots(cfg)
    for i, (job, at) in enumerate(due):
        sched.schedule_once(job, now + timedelta(minutes=i), prefix_for(slots[job]), at)
    return jobs


def caught_up_today(repo: Any, now: datetime, tz: str) -> list[str]:
    """오늘(현지) 따라잡기로 끝까지 성공한 잡(대시보드 "오늘"의 한 줄). 건너뜀은 빼고, 먼저 돈 것부터."""
    from nsm.domain.policy import local_day_bounds

    start, end = local_day_bounds(now, tz)
    rows = repo.db.execute(
        "SELECT job, min(started_at) FROM run_logs WHERE ok AND started_at >= ? AND started_at < ? "
        "AND starts_with(coalesce(message, ''), '따라잡기(') "
        "AND coalesce(message, '') NOT LIKE '따라잡기(%) · 건너뜀:%' GROUP BY job ORDER BY 2",
        [to_db(start), to_db(end)],
    )
    return [r[0] for r in rows]
