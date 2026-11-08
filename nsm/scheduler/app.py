"""잡 스케줄 (contracts 4절).

모든 잡은 **작업 스레드 하나**에서 차례로 돈다. Playwright 동기 API는 만든 스레드에서만 써야 하고,
잡끼리 같은 브라우저·DB 행을 동시에 건드리지 않게 하려는 것이다. 대시보드가 플랫폼 동작(직접 답글)을
요청할 때도 `submit()`으로 같은 스레드에 넣는다.

하루 한 번 이하로 도는 잡(자료 모으기·원고 만들기·백업·정리·문서 색인·성과 읽기·주간 리포트)은 두 겹으로 지킨다(C40·C41, ADR 0066).
- 예약 유예를 3시간(`LONG_GRACE`)으로: 짧은 절전에서 깨면 APScheduler가 스스로 돌린다.
- 따라잡기 점검(`install_catch_up`): 켤 때 한 번과 30분마다, 꺼져 있었거나 3시간 넘게 잔 날을 찾아 한 번 돌린다.
"""

from __future__ import annotations

import logging
import uuid
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
from nsm.engine.catch_up import (
    activity_slots,
    catch_up_slots,
    fixed_slots,
    queue_catch_ups,
    ran_since,
)
from nsm.engine.context import JobContext, JobResult
from nsm.engine.runner import run_job

log = logging.getLogger(__name__)

DEFAULT_GRACE = 900  # 초. 30분 간격 잡 등: 늦으면 다음 회차가 곧 온다
LONG_GRACE = (
    3 * 3600
)  # 하루 1~2회 잡(따라잡기 대상 여섯): 절전에서 3시간 안에 깨면 그날 것을 그대로 돌린다(C40·C41)
CATCH_UP_FIRST_DELAY = timedelta(seconds=90)  # 켠 뒤 시작 점검(로그인 확인 등)이 먼저 돌게
CATCH_UP_EVERY = timedelta(minutes=30)


def job_triggers(cfg: AppConfig) -> dict[str, Any]:
    """잡 이름 → 트리거. 활동 시간 창의 시작 시각을 기준으로 수집·생성 시각을 잡는다."""
    tz = cfg.settings.timezone
    act = activity_slots(cfg)  # 수집·생성 시각은 따라잡기와 같은 계산(catch_up.activity_slots)
    gen = act["generate"][0]
    triggers: dict[str, Any] = {
        "health": IntervalTrigger(minutes=30, jitter=120, timezone=tz),
        "generate": CronTrigger(hour=gen.hour, minute=gen.minute, jitter=gen.jitter, timezone=tz),
        "publish": IntervalTrigger(minutes=30, jitter=600, timezone=tz),
        "reply": IntervalTrigger(
            minutes=cfg.settings.reply_interval_minutes, jitter=60, timezone=tz
        ),
    }
    # 정해진 시각 잡(metrics·weekly_report·maintenance·index): 시각은 따라잡기와 같은 표(catch_up.fixed_slots)
    for name, slot in fixed_slots(cfg).items():
        triggers[name] = CronTrigger(
            day_of_week=slot.day_of_week,
            hour=slot.hour,
            minute=slot.minute,
            jitter=slot.jitter,
            timezone=tz,
        )
    for i, slot in enumerate(act["collect"]):
        triggers[f"collect#{i}"] = CronTrigger(
            hour=slot.hour, minute=slot.minute, jitter=slot.jitter, timezone=tz
        )
    return triggers


class JobScheduler:
    def __init__(self, ctx_provider: Callable[[], JobContext], available_jobs: set[str]) -> None:
        self._ctx = ctx_provider
        self.available = available_jobs
        self.scheduler = BackgroundScheduler(
            executors={"default": ThreadPoolExecutor(max_workers=1)},
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": DEFAULT_GRACE,
            },
        )
        self.boot = uuid.uuid4().hex  # 이 기동의 id: 따라잡기 "오늘 시도"를 기동마다 따로 센다

    def _run(
        self, name: str, manual: bool = False, arg: str | None = None, prefix: str = ""
    ) -> JobResult:
        result = run_job(name, self._ctx(), manual=manual, arg=arg, prefix=prefix)
        log.info("%s: %s%s", name, prefix, result.summary())
        return result

    def install(self, cfg: AppConfig) -> list[str]:
        installed = []
        long_grace = set(catch_up_slots(cfg))  # 따라잡기 대상 여섯(수집·생성 포함, C41)
        for job_id, trigger in job_triggers(cfg).items():
            name = job_id.split("#")[0]
            if name not in self.available:
                continue
            self.scheduler.add_job(
                self._run,
                trigger,
                args=[name],
                id=job_id,
                replace_existing=True,
                misfire_grace_time=LONG_GRACE if name in long_grace else DEFAULT_GRACE,
            )
            installed.append(job_id)
        return installed

    # ------------------------------------------------------------------ 따라잡기(C40)

    def install_catch_up(self, now: datetime | None = None) -> list[str]:
        """켠 뒤 한 번(90초 뒤)과 그 뒤 30분마다 놓친 하루·주 한 번 잡을 찾는다. 설정은 점검할 때마다 ctx에서 읽어
        `catch_up_jobs`·`rag.index_time`을 바꿔도 다음 점검부터 반영된다."""
        first = (now or datetime.now().astimezone()) + CATCH_UP_FIRST_DELAY
        self.scheduler.add_job(
            self.check_catch_up,
            DateTrigger(run_date=first),
            id="catch_up#start",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self.check_catch_up,
            IntervalTrigger(
                minutes=CATCH_UP_EVERY.seconds // 60, start_date=first + CATCH_UP_EVERY
            ),
            id="catch_up",
            replace_existing=True,
            misfire_grace_time=None,  # 오래 자고 깨면 늦었어도 바로 한 번 본다(15분 유예로 버리지 않게)
        )
        return ["catch_up#start", "catch_up"]

    def check_catch_up(self) -> list[str]:
        ctx = self._ctx()
        if ctx.shutting_down():
            return []
        cfg = ctx.config
        if not cfg.settings.catch_up_jobs:
            return []
        queued = queue_catch_ups(self, cfg, ctx.repo, ctx.clock.now(), self.boot)
        if queued:
            log.info("따라잡기 예약: %s", ", ".join(queued))
        return queued

    def schedule_once(
        self, name: str, run_date: datetime, prefix: str, missed_at: datetime
    ) -> None:
        """작업 스레드에서 run_date에 name을 한 번(따라잡기). 실제로 돌 때 그 회차(missed_at)를 다시 확인한다."""
        self.scheduler.add_job(
            self._run_catch_up,
            DateTrigger(run_date=run_date),
            args=[name, prefix, missed_at],
            misfire_grace_time=None,  # 앞 잡이 길어 늦게 집어도 버리지 않는다(오늘 시도로 이미 적었다)
        )

    def _run_catch_up(self, name: str, prefix: str, missed_at: datetime) -> JobResult | None:
        ctx = self._ctx()
        if ctx.shutting_down():
            log.info("따라잡기 %s: 끄는 중이라 하지 않음", name)
            return None
        # 띄울 때 본 그 회차로 다시 본다(자정을 넘겨 집혀도 날짜가 바뀌어 빠지지 않게)
        if ran_since(ctx.repo, name, missed_at):
            # 그새 예약 실행이 돌았다(절전에서 깨며 APScheduler가 스스로 돌린 경우 등)
            log.info("따라잡기 %s: 그새 돌아서 건너뜀", name)
            return None
        return self._run(name, prefix=prefix)

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
