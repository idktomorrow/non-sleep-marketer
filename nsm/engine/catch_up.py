"""하루·주 한 번 잡 따라잡기(C40, ADR 0066, incidents/0013).

예정 시각에 프로그램이 꺼져 있었거나 PC가 절전이면 그날 백업·문서 색인·성과 읽기·주간 리포트가 통째로 빠졌다.
APScheduler는 꺼져 있던 동안의 예약을 모르고, 절전도 유예(misfire_grace_time)보다 길면 버린다. 그래서 켤 때와
30분마다 "오늘 예정 시각이 지났는데 그 뒤로 끝난 실행이 없는 잡"을 찾아 작업 스레드에 한 번씩 띄운다.

- 놓침 기준: 현지 시간대로 오늘 예정 시각(주간 리포트는 이번 주 월요일 08:00)이 지났고, 예정 1시간 전 이후 그 잡의
  끝난 실행이 없다. 실패로 끝난 실행도 "돌았다"로 본다(같은 날 다시 하지 않는다). 종료 중단(`INTERRUPTED_PREFIX`)과
  끝나지 않은 실행(프로그램이 실행 중에 꺼짐)은 돌지 않은 것으로 본다.
- 예정 시각에서 지터(트리거의 무작위 지연)+5분이 지나야 놓침으로 본다. 정상으로 켜져 있을 때 예약 실행보다 먼저
  따라잡기가 돌아 하루 두 번 백업하지 않게.
- 같은 프로세스 안에서는 한 회차를 한 번만 띄운다(`TRIED_KEY`에 현지 날짜·기동 id·잡과 회차). 띄운 뒤 실패하거나 기다리는 중이어도
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

# 따라잡기 대상과 실행 순서(C41). 설정 저장 순서와 상관없이 이 순서로 1분 간격: 자료를 모은 뒤 원고를 만들고,
# 그다음 백업(새 원고까지 담기게)·지표·색인·주간 리포트
CATCH_UP_JOBS = ("collect", "generate", "maintenance", "metrics", "index", "weekly_report")
ACTIVITY_JOBS = ("collect", "generate")  # 활동 시간을 따르는 잡: 페르소나를 꺼 두면 따라잡지 않는다
# {"day": "2026-11-07", "boot": "<기동 id>", "jobs": ["collect@2026-11-07T09:20:00+09:00", ...]}: 회차까지 적는다
TRIED_KEY = "catch_up_tried"
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


def at_offset(start: time, minutes: int) -> tuple[int, int]:
    """활동 시간 창 시작에서 minutes분 뒤(시, 분). 스케줄러 트리거와 따라잡기가 같이 쓴다."""
    t = datetime.combine(datetime(2000, 1, 1), start) + timedelta(minutes=minutes)
    return t.hour, t.minute


def activity_slots(cfg: Any) -> dict[str, list[Slot]]:
    """활동 시간을 따르는 잡의 예정 시각: 자료 모으기는 첫 창·마지막 창 시작 20분 뒤(창이 하나면 하루 1회),
    원고 만들기는 첫 창 시작 50분 뒤."""
    windows = cfg.persona.active_hours
    first, last = windows[0], windows[-1]
    collect = sorted({at_offset(first.start, 20), at_offset(last.start, 20)})
    gen_h, gen_m = at_offset(first.start, 50)
    return {
        "collect": [Slot(h, m, 1800) for h, m in collect],
        "generate": [Slot(gen_h, gen_m, 600)],
    }


def catch_up_slots(cfg: Any) -> dict[str, list[Slot]]:
    """따라잡기 대상 잡마다 예정 시각 목록(자료 모으기는 하루 2회일 수 있다)."""
    return {**activity_slots(cfg), **{k: [v] for k, v in fixed_slots(cfg).items()}}


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


def finished_since(repo: Any, job: str, since: datetime) -> bool:
    """since 이후 끝난 실행(성공·실패·건너뜀, 종료 중단과 끝나지 않은 것 제외)이 있나. 게시 감시도 쓴다."""
    return _ran_since(repo, job, since)


def ran_since(repo: Any, job: str, missed_at: datetime) -> bool:
    """그 회차(예정 1시간 전 이후)에 끝난 실행이 있나."""
    return _ran_since(repo, job, missed_at - RAN_WINDOW)


def prefix_for(slot: Slot) -> str:
    return f"따라잡기({slot.label} 놓침) · "


def latest_missed(slots: list[Slot], now: datetime, tz: str) -> tuple[Slot, datetime] | None:
    """하루 여러 회차면 가장 최근에 지난(기다림까지 지난) 회차 하나."""
    found = [(sl, at) for sl in slots if (at := missed_slot(sl, now, tz)) is not None]
    return max(found, key=lambda x: x[1]) if found else None


def _next_regular_soon(slots: list[Slot], at: datetime, now: datetime, tz: str) -> bool:
    """놓친 회차 뒤에 같은 잡의 다른 회차가 1시간 안에 있거나, 이미 예정 시각이 지나 지터로 도는 중인가."""
    for sl in slots:
        nxt = scheduled_at(sl, now, tz)
        wait = timedelta(seconds=sl.jitter) + MARGIN
        if at < nxt and nxt - RAN_WINDOW <= now < nxt + wait:
            return True
    return False


def _key(job: str, at: datetime) -> str:
    return f"{job}@{at.isoformat()}"


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


def _due(cfg: Any, repo: Any, now: datetime, boot: str | None) -> list[tuple[str, datetime, Slot]]:
    s = cfg.settings
    wanted = set(s.catch_up_jobs)
    slots = catch_up_slots(cfg)
    tried = _tried(repo, now, s.timezone, boot) if boot else set()
    out = []
    for job in CATCH_UP_JOBS:  # 정해진 순서(설정 저장 순서와 무관)
        if job not in wanted or (job in ACTIVITY_JOBS and not cfg.persona.enabled):
            continue
        hit = latest_missed(slots[job], now, s.timezone)
        if hit is None:
            continue  # 아직 예정 전이거나, 예약 실행이 지터로 늦게 돌 수 있는 동안
        slot, at = hit
        if _key(job, at) in tried or ran_since(repo, job, at):
            continue
        if _next_regular_soon(slots[job], at, now, s.timezone):
            continue  # 같은 잡의 다음 예약(예: 저녁 모으기)이 곧 돈다: 겹쳐 두 번 돌지 않게(C41 리뷰)
        out.append((job, at, slot))
    return out


def due_catch_ups(
    cfg: Any, repo: Any, now: datetime, boot: str | None = None
) -> list[tuple[str, datetime]]:
    """[(잡, 놓친 예정 시각)], `CATCH_UP_JOBS` 순서. boot가 있으면 이 기동에서 그 회차를 이미 띄운 잡은 뺀다."""
    return [(job, at) for job, at, _ in _due(cfg, repo, now, boot)]


def queue_catch_ups(sched: Any, cfg: Any, repo: Any, now: datetime, boot: str) -> list[str]:
    """놓친 잡을 1분 간격으로 작업 스레드에 예약한다. 띄우기 전에 그 회차를 시도로 적는다(실패해도 같은 회차를
    다시 띄우지 않게. 자료 모으기의 저녁 회차처럼 다른 회차는 따로 따라잡는다)."""
    due = _due(cfg, repo, now, boot)
    if not due:
        return []
    mark_tried(repo, now, cfg.settings.timezone, boot, [_key(j, at) for j, at, _ in due])
    for i, (job, at, slot) in enumerate(due):
        sched.schedule_once(job, now + timedelta(minutes=i), prefix_for(slot), at)
    return [j for j, _, _ in due]


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
