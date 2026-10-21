"""잡 스케줄 (contracts 4절).

모든 잡은 **작업 스레드 하나**에서 차례로 돈다. Playwright 동기 API는 만든 스레드에서만 써야 하고,
잡끼리 같은 브라우저·DB 행을 동시에 건드리지 않게 하려는 것이다. 대시보드가 플랫폼 동작(직접 답글)을
요청할 때도 `submit()`으로 같은 스레드에 넣는다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future
from datetime import datetime, timedelta
from typing import Any

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from nsm.config.schema import AppConfig
from nsm.engine.context import JobContext, JobResult
from nsm.engine.runner import run_job

log = logging.getLogger(__name__)


def _at(start, minutes: int) -> tuple[int, int]:
    t = datetime.combine(datetime(2000, 1, 1), start) + timedelta(minutes=minutes)
    return t.hour, t.minute


def job_triggers(cfg: AppConfig) -> dict[str, Any]:
    """잡 이름 → 트리거. 활동 시간 창의 시작 시각을 기준으로 수집·생성 시각을 잡는다."""
    tz = cfg.settings.timezone
    windows = cfg.persona.active_hours
    first, last = windows[0], windows[-1]
    collect_times = {_at(first.start, 20), _at(last.start, 20)}
    gen_h, gen_m = _at(first.start, 50)
    triggers: dict[str, Any] = {
        "health": IntervalTrigger(minutes=30, jitter=120, timezone=tz),
        "generate": CronTrigger(hour=gen_h, minute=gen_m, jitter=600, timezone=tz),
        # 오늘의 추천(S11): 켜져 있으면 generate 대신 돈다(꺼져 있으면 잡이 스스로 건너뜀). 수집은 잡 안에서 먼저 한다
        "picks": CronTrigger(hour=gen_h, minute=gen_m, jitter=600, timezone=tz),
        "publish": IntervalTrigger(minutes=30, jitter=600, timezone=tz),
        "reply": IntervalTrigger(
            minutes=cfg.settings.reply_interval_minutes, jitter=60, timezone=tz
        ),
        "metrics": CronTrigger(hour=3, minute=10, jitter=900, timezone=tz),
        "weekly_report": CronTrigger(day_of_week="mon", hour=8, minute=0, jitter=600, timezone=tz),
        # 데이터 백업·실행 기록 정리(C12). 지표(03:10) 뒤, 다른 작업이 드문 새벽
        "maintenance": CronTrigger(hour=4, minute=30, jitter=600, timezone=tz),
    }
    for i, (h, m) in enumerate(sorted(collect_times)):
        triggers[f"collect#{i}"] = CronTrigger(hour=h, minute=m, jitter=1800, timezone=tz)
    return triggers


class JobScheduler:
    def __init__(self, ctx_provider: Callable[[], JobContext], available_jobs: set[str]) -> None:
        self._ctx = ctx_provider
        self.available = available_jobs
        self.scheduler = BackgroundScheduler(
            executors={"default": ThreadPoolExecutor(max_workers=1)},
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 900},
        )

    def _run(self, name: str, manual: bool = False, arg: str | None = None) -> JobResult:
        result = run_job(name, self._ctx(), manual=manual, arg=arg)
        log.info("%s: %s", name, result.summary())
        return result

    def install(self, cfg: AppConfig) -> list[str]:
        installed = []
        for job_id, trigger in job_triggers(cfg).items():
            name = job_id.split("#")[0]
            if name not in self.available:
                continue
            self.scheduler.add_job(
                self._run, trigger, args=[name], id=job_id, replace_existing=True
            )
            installed.append(job_id)
        return installed

    def reschedule(self, cfg: AppConfig) -> list[str]:
        """활동 시간이 바뀌면 수집·생성 시각도 바뀐다. 예약을 새 설정으로 다시 건다(설정 화면, M17)."""
        new_ids = set(job_triggers(cfg))
        for job in self.scheduler.get_jobs():
            if job.id.startswith("collect#") and job.id not in new_ids:
                self.scheduler.remove_job(job.id)
        return self.install(cfg)

    def start(self) -> None:
        self.scheduler.start()

    def shutdown(self) -> None:
        """멈추기 전에 스케줄러가 하던 한 바퀴를 마치게 한다.

        APScheduler 3은 shutdown()에서 상태를 '멈춤'으로 먼저 바꾼 뒤 잠금을 잡는다. 그 사이 스케줄러
        스레드가 막 끝난 일회성 작업(submit)을 지우려 하면 '멈춤' 상태라 대기 목록에서 찾다가
        JobLookupError가 난다(가끔 나던 테스트 경고, C12). 일시정지로 새 바퀴를 막고, 잠금을 한 번
        잡아 하던 바퀴가 끝나기를 기다린 뒤 멈춘다.
        """
        if self.scheduler.running:
            self.scheduler.pause()
            # 내부 잠금 이름이 바뀐 버전이면 기다리지 않고 그냥 멈춘다(경고가 날 수 있을 뿐 동작은 같다)
            lock = getattr(self.scheduler, "_jobstores_lock", None)
            if lock is not None:
                with lock:  # 하던 바퀴가 끝날 때까지
                    pass
        self.scheduler.shutdown(wait=True)

    def submit(self, fn: Callable[[], Any]) -> Future:
        """작업 스레드에서 fn을 한 번 실행하고 결과를 Future로 돌려준다."""
        fut: Future = Future()

        def wrapper() -> None:
            try:
                fut.set_result(fn())
            except Exception as e:  # noqa: BLE001
                fut.set_exception(e)

        self.scheduler.add_job(wrapper, DateTrigger(run_date=datetime.now().astimezone()))
        return fut

    def run_now(self, name: str, arg: str | None = None) -> Future:
        """대시보드 "지금 실행": 사람이 부른 실행(manual). Future 결과는 JobResult.

        arg: 값을 받는 잡(키워드 원고의 키워드, C15).
        """
        return self.submit(lambda: self._run(name, manual=True, arg=arg))
