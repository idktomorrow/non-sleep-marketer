from __future__ import annotations

import threading
from pathlib import Path

from nsm.scheduler.app import JobScheduler, job_triggers
from nsm.scheduler.autostart import SCRIPT_NAME, install, script_text, uninstall


def test_triggers_follow_active_hours(app_config) -> None:
    t = job_triggers(app_config)
    assert {"health", "generate", "publish", "reply", "metrics", "weekly_report"} <= set(t)
    collect = [k for k in t if k.startswith("collect#")]
    assert len(collect) == 2  # 활동 시간 창 두 개(09시, 18시) 기준 하루 2회
    text = str(t["collect#0"]) + str(t["collect#1"])
    assert "hour='9'" in text and "hour='18'" in text and "minute='20'" in text


def test_install_skips_unavailable_jobs(app_config) -> None:
    s = JobScheduler(lambda: None, {"collect", "publish"})
    installed = s.install(app_config)
    assert set(installed) == {"collect#0", "collect#1", "publish"}


def test_submit_runs_on_single_worker_thread() -> None:
    s = JobScheduler(lambda: None, set())
    s.start()
    try:
        ids = [s.submit(lambda: threading.get_ident()).result(timeout=10) for _ in range(3)]
    finally:
        s.shutdown()
    assert len(set(ids)) == 1 and ids[0] != threading.get_ident()


def test_submit_propagates_errors() -> None:
    s = JobScheduler(lambda: None, set())
    s.start()
    try:
        fut = s.submit(lambda: 1 / 0)
        try:
            fut.result(timeout=10)
            raised = False
        except ZeroDivisionError:
            raised = True
    finally:
        s.shutdown()
    assert raised


def test_autostart_script(tmp_path: Path, nsm_home) -> None:
    msg = install(target_dir=tmp_path, project_dir=Path("C:/proj"))
    path = tmp_path / SCRIPT_NAME
    assert path.exists() and str(path) in msg
    text = script_text(Path("C:/proj"), "uv.exe", Path("C:/logs/x.log"))
    assert 'cd /d "C:\\proj"' in text or 'cd /d "C:/proj"' in text
    assert "run nsm run" in text and text.endswith("\r\n")
    assert "삭제" not in uninstall(target_dir=tmp_path) and not path.exists()
    assert "없습니다" in uninstall(target_dir=tmp_path)


def test_reschedule_follows_new_active_hours(app_config) -> None:
    from nsm.config.store import parse_windows

    s = JobScheduler(lambda: None, {"collect", "generate"})
    s.install(app_config)
    s.scheduler.start(paused=True)  # 실행 중인 스케줄러에서 바꾸는 상황(작업은 돌지 않게)
    try:
        one = app_config.model_copy(deep=True)
        one.persona.active_hours = parse_windows("00:00-23:59")
        s.reschedule(one)
        ids = {j.id for j in s.scheduler.get_jobs()}
        assert ids == {"collect#0", "generate"}  # 창이 하나라 하루 1회 수집
        assert "hour='0'" in str(s.scheduler.get_job("collect#0").trigger)
    finally:
        s.shutdown()


def test_reply_interval_follows_setting(app_config) -> None:
    assert "0:05:00" in str(job_triggers(app_config)["reply"])  # 기본 5분
    fast = app_config.model_copy(deep=True)
    fast.settings.reply_interval_minutes = 3
    assert "0:03:00" in str(job_triggers(fast)["reply"])
