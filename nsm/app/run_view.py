"""실행 기록 화면 정리 (배치 C11).

기록은 전부 저장하고, 화면에서만 걸러 보여 준다.

- 작업 상태 표: 작업마다 마지막 실행 한 줄. "잘 돌고 있나?"는 이것만 보면 된다.
- 아래 목록(기본): 아무 일 없던 성공("새 댓글 없음", "로그인 정상", "건너뜀: 게시 시간 밖" 등)은 숨기고,
  같은 작업이 같은 결과로 이어지면 "8번" 한 줄로 묶는다. "전체 보기"를 누르면 지금까지처럼 모든 줄.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from nsm.config.schema import AppConfig
from nsm.engine.runner import INTERRUPTED_PREFIX
from nsm.engine.summary import readable

# 아무 일도 없었다는 뜻의 문장(요약 전체가 이것과 같거나, 이것으로 시작하는 건너뜀)
ROUTINE = {"새 댓글 없음", "로그인 정상", "바뀐 것 없음", "새 원고 없음", "올린 글 없음"}
# 정해진 시간 밖이라 건너뛴 것만. "건너뜀: 쓸 자료 없음" 같은 건 알아야 하므로 보여 준다
ROUTINE_PREFIX = (
    "건너뜀: 활동 시간 밖",
    "건너뜀: 게시 시간 밖",
    "건너뜀: 오늘 생성 목표 달성",
)
NO_POST = "올린 글 없음 · 이유: "
# 게시를 안 한 이유 중 평소 일(간격·한도·원고 없음). "연결 안 됨"·"일시 중지됨"·요청 제한은 보여 준다
ROUTINE_WAITS = ("간격 대기", "다 올림", "올릴 차례인 원고 없음")
ERROR_MARK = " · 오류 "  # describe()가 붙이는 "· 오류 N건: …"

# '실행 중'인 채로 이보다 오래면 실행 도중 프로그램이 꺼진 것으로 본다
STUCK_AFTER = timedelta(hours=2)

# 걸러 보여 줄 때 살펴볼 최근 실행 수(5분마다 도는 작업이 있어 하루 약 400줄). 조용한 밤에도 어제 일이 보이게
RUN_WINDOW = 1000

# 작업 순서(상태 표). 하는 일 순서대로
ORDER = (
    "reply",
    "publish",
    "health",
    "generate",
    "collect",
    "metrics",
    "weekly_report",
    "maintenance",
    "index",
)


@dataclass
class RunRow:
    job: str
    started_at: datetime | None  # 묶였으면 가장 이른 시작
    finished_at: datetime | None  # 묶였으면 가장 늦은 끝
    ok: bool | None
    text: str
    count: int = 1


@dataclass
class JobStatus:
    job: str
    every: str
    last: RunRow | None
    state: str  # ok / bad / running / stale / never
    note: str = ""  # 예: "마지막 성공 01:53"


def is_routine(row: RunRow) -> bool:
    """아무 일 없던 성공인가. 오류가 하나라도 적혀 있으면 성공으로 끝났어도 보여 준다."""
    t = row.text
    if not row.ok or ERROR_MARK in t:
        return False
    if t in ROUTINE or t.startswith(ROUTINE_PREFIX):
        return True
    if t.startswith(NO_POST):
        reasons = t[len(NO_POST) :].split(", ")
        return all(any(w in r for w in ROUTINE_WAITS) for r in reasons)
    return False


def build_rows(runs: list[dict[str, Any]], show_all: bool) -> tuple[list[RunRow], int]:
    """(보여 줄 줄, 숨긴 실행 수). runs는 최근 것부터."""
    rows = [
        RunRow(
            r["job"], r["started_at"], r["finished_at"], r["ok"], readable(r["job"], r["message"])
        )
        for r in runs
    ]
    if show_all:
        return rows, 0
    out: list[RunRow] = []
    hidden = 0
    open_group: dict[str, RunRow] = {}  # 작업별로 지금 이어지는 묶음
    for row in rows:
        if is_routine(row):
            hidden += 1
            open_group.pop(row.job, None)  # 아무 일 없던 실행이 끼면 묶음이 끊긴다
            continue
        g = open_group.get(row.job)
        if g and (g.ok, g.text) == (row.ok, row.text):
            g.count += 1
            g.started_at = row.started_at  # 최근 것부터 오므로 더 이른 시각
            continue
        out.append(row)
        open_group[row.job] = row
    return out, hidden


def _every(trigger) -> str:
    minutes = int(trigger.interval.total_seconds() // 60)
    return f"{minutes}분마다"


def schedule_text(cfg: AppConfig) -> dict[str, str]:
    """작업 → 언제 도는지. 주기 작업은 scheduler.job_triggers에서 읽고, 하루·주 단위는 사람 말로 적는다."""
    from nsm.scheduler.app import job_triggers

    t = job_triggers(cfg)
    s = cfg.settings
    return {
        "reply": f"{_every(t['reply'])}(인스타그램은 {s.instagram_comment_interval_minutes}분마다)",
        "publish": f"{_every(t['publish'])}(게시 시간에만)",
        "health": _every(t["health"]),
        "generate": "하루 1번",
        "collect": "하루 1~2번",
        "metrics": "매일 새벽 3시",
        "weekly_report": "매주 월요일 아침",
        "maintenance": "매일 새벽 4시 반",
        "index": f"매일 {s.rag.index_time}",
    }


def _stale_after(cfg: AppConfig) -> dict[str, timedelta]:
    """이만큼 넘게 안 돌았으면 "오래 안 돌았음"(PC가 꺼졌거나 프로그램이 멈춤). 자주 도는 작업만.

    작업은 한 줄로 차례로 돈다. 원고 만들기·자료 모으기가 길면 댓글 확인이 밀리므로 최소 60분은 기다린다.
    """
    return {
        "reply": max(
            timedelta(minutes=cfg.settings.reply_interval_minutes * 3), timedelta(minutes=60)
        ),
        "health": timedelta(minutes=90),
        "publish": timedelta(minutes=90),
    }


def job_statuses(
    cfg: AppConfig,
    latest: list[dict[str, Any]],
    last_ok: dict[str, datetime],
    now: datetime,
    fmt,
) -> list[JobStatus]:
    by_job = {
        r["job"]: RunRow(
            r["job"], r["started_at"], r["finished_at"], r["ok"], readable(r["job"], r["message"])
        )
        for r in latest
    }
    every = schedule_text(cfg)
    stale = _stale_after(cfg)
    out = []
    for job in ORDER:
        last = by_job.get(job)
        if last is None:
            out.append(JobStatus(job, every[job], None, "never"))
            continue
        ago = now - last.started_at if last.started_at else timedelta(0)
        ok_at = last_ok.get(job)
        since_ok = f"마지막 성공 {fmt(ok_at)}" if ok_at else "성공한 적 없음"
        # 실패·실행 중으로 끝난 뒤 멈춘 경우도 알아본다(프로그램이 실행 도중 꺼지면 '실행 중'으로 남는다)
        if (job in stale and ago > stale[job]) or (last.ok is None and ago > STUCK_AFTER):
            state = "stale"
            note = "오래 안 돌았음: PC가 꺼졌거나 프로그램이 멈췄을 수 있습니다"
            if last.ok is False:
                note += f" · {since_ok}"
        elif last.ok is None:
            state, note = "running", ""
        elif not last.ok and last.text.startswith(INTERRUPTED_PREFIX):
            state, note = "interrupted", since_ok  # 끄는 순간 끊긴 것: 실패가 아니다(C33)
        elif not last.ok:
            state, note = "bad", since_ok
        else:
            state, note = "ok", ""
        out.append(JobStatus(job, every[job], last, state, note))
    return out
