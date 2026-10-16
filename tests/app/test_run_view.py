"""실행 기록 화면 정리 (배치 C11): 작업 상태 표, 기본은 변화가 있던 실행만, 같은 결과는 묶음, 전체 보기."""

from __future__ import annotations

from datetime import timedelta

from nsm.app.run_view import build_rows, job_statuses
from nsm.store.repo import Repository
from tests.app.test_ui import make  # noqa: F401

WAIT = "새 댓글 없음 · 인스타그램 요청 제한으로 쉬는 중(02:56 이후 다시)"


def _runs(clock, *items):
    """(작업, ok, 요약) 목록 → recent_runs 모양(최근 것부터, 5분 간격)."""
    now = clock.now()
    return [
        {
            "job": job,
            "started_at": now - timedelta(minutes=5 * i),
            "finished_at": now - timedelta(minutes=5 * i - 1),
            "ok": ok,
            "message": msg,
        }
        for i, (job, ok, msg) in enumerate(items)
    ]


def test_routine_runs_are_hidden_and_repeats_grouped(clock) -> None:
    runs = _runs(
        clock,
        ("reply", True, "새 댓글 없음"),
        ("reply", True, "새 댓글 2개 · 자동 답글 2개"),
        ("reply", True, WAIT),
        ("health", True, "로그인 정상 · 인스타그램 요청 제한으로 쉬는 중(02:56 이후 다시)"),
        ("reply", True, WAIT),  # 사이에 다른 작업이 껴도 같은 작업끼리 묶인다
        ("publish", True, "건너뜀: 게시 시간 밖"),
        (
            "reply",
            True,
            "reply: new=0, auto_replied=0, hidden=0, escalated=0, ignored=0, rate_limited=['인스타그램 요청 제한으로 쉬는 중(02:56 이후 다시)']",
        ),
        ("health", True, "health: paused=[]"),
        ("reply", False, "새 댓글 없음 · 오류 1건: 타임아웃"),
    )
    rows, hidden = build_rows(runs, show_all=False)
    assert hidden == 3  # 새 댓글 없음, 게시 시간 밖, 로그인 정상(예전 형식)
    assert [(r.job, r.count) for r in rows] == [
        ("reply", 1),  # 새 댓글 2개
        ("reply", 3),  # 요청 제한(예전 형식 줄까지 같은 문장이라 묶임)
        ("health", 1),
        ("reply", 1),  # 실패는 늘 보인다
    ]
    grouped = rows[1]
    assert grouped.text == WAIT
    assert (
        grouped.started_at == runs[6]["started_at"]
        and grouped.finished_at == runs[2]["finished_at"]
    )


def test_show_all_keeps_every_run(clock) -> None:
    runs = _runs(clock, ("reply", True, "새 댓글 없음"), ("reply", True, "새 댓글 없음"))
    rows, hidden = build_rows(runs, show_all=True)
    assert (len(rows), hidden) == (2, 0)


def test_informative_skips_are_not_hidden(clock) -> None:
    runs = _runs(clock, ("generate", True, "건너뜀: 쓸 자료 없음(관련 자료 수집 필요)"))
    assert len(build_rows(runs, show_all=False)[0]) == 1


def test_job_status_table(app_config, clock) -> None:
    now = clock.now()
    latest = [
        {
            "job": "reply",
            "started_at": now - timedelta(minutes=3),
            "finished_at": now,
            "ok": True,
            "message": "새 댓글 없음",
        },
        {
            "job": "health",
            "started_at": now - timedelta(hours=3),
            "finished_at": now,
            "ok": True,
            "message": "로그인 정상",
        },
        {
            "job": "publish",
            "started_at": now - timedelta(minutes=10),
            "finished_at": now,
            "ok": False,
            "message": "1건 실패 · 오류 1건: x",
        },
        {"job": "generate", "started_at": now, "finished_at": None, "ok": None, "message": None},
    ]
    st = {
        s.job: s
        for s in job_statuses(
            app_config,
            latest,
            {"publish": now - timedelta(hours=2)},
            now,
            lambda d: d.strftime("%H:%M"),
        )
    }
    assert st["reply"].state == "ok" and st["reply"].every.startswith("5분마다")
    assert st["health"].state == "stale" and "PC가 꺼졌" in st["health"].note
    assert st["publish"].state == "bad" and st["publish"].note == "마지막 성공 08:00"
    assert st["generate"].state == "running"
    assert st["weekly_report"].state == "never"


def test_repo_latest_runs_and_last_success(db, clock) -> None:
    repo = Repository(db, clock)
    for ok, msg in ((True, "a"), (False, "b")):
        rid = repo.start_run("reply")
        repo.finish_run(rid, ok, msg)
        clock.advance(minutes=5)
    rid = repo.start_run("health")
    repo.finish_run(rid, True, "로그인 정상")
    latest = {r["job"]: r for r in repo.latest_runs()}
    assert latest["reply"]["message"] == "b" and latest["health"]["ok"] is True
    assert set(repo.last_success_at()) == {"reply", "health"}


def test_logs_page_default_and_all(make) -> None:  # noqa: F811
    build, _, repo = make
    for msg in ("새 댓글 없음", WAIT, WAIT, "새 댓글 1개 · 자동 답글 1개"):
        rid = repo.start_run("reply")
        repo.finish_run(rid, True, msg)
    client, _ = build()
    html = client.get("/logs").text
    assert "작업 상태" in html and "변화가 있던 실행" in html
    assert "아무 일 없던 실행 1번은 숨겼습니다" in html and 'href="/logs?all=1"' in html
    assert ">2번<" in html and html.count('class="run-text"') == 2
    every = client.get("/logs?all=1").text
    assert "모든 실행" in every and every.count('class="run-text"') == 4
    repo.set_setting("ui_mode", "advanced")  # 최근 실행 표는 고급 모드 "오늘"에만(C13)
    home = client.get("/").text
    assert "새 댓글 1개" in home and ">새 댓글 없음<" not in home


def test_problems_are_never_hidden(clock) -> None:
    """게시 안 한 이유가 연결·멈춤·요청 제한이거나, 성공으로 끝났어도 오류가 적혀 있으면 보여 준다."""
    shown = [
        "올린 글 없음 · 이유: 인스타그램 연결 안 됨",
        "올린 글 없음 · 이유: 인스타그램 일시 중지됨",
        "올린 글 없음 · 이유: 인스타그램 요청 제한으로 쉬는 중(02:56 이후 다시)",
        "올린 글 없음 · 이유: 스레드 3시간 간격 대기(14:00 이후) · 오류 1건: x",
        "새 댓글 없음 · 오류 1건: instagram 댓글 조회 실패",
    ]
    hidden = [
        "올린 글 없음 · 이유: 인스타그램 오늘 1건 다 올림(내일 다시), 스레드 3시간 간격 대기(14:00 이후)",
        "올린 글 없음 · 이유: 스레드 올릴 차례인 원고 없음(재시도 시각 전이거나 대기 없음)",
    ]
    runs = _runs(clock, *[("publish", True, t) for t in shown + hidden])
    rows, n = build_rows(runs, show_all=False)
    assert [r.text for r in rows] == shown and n == len(hidden)


def test_stopped_after_failure_or_mid_run_is_stale(app_config, clock) -> None:
    now = clock.now()
    latest = [
        {
            "job": "reply",
            "started_at": now - timedelta(hours=3),
            "finished_at": now,
            "ok": False,
            "message": "x",
        },
        {
            "job": "generate",
            "started_at": now - timedelta(hours=5),
            "finished_at": None,
            "ok": None,
            "message": None,
        },
        {
            "job": "health",
            "started_at": now - timedelta(minutes=50),
            "finished_at": now,
            "ok": True,
            "message": "로그인 정상",
        },
    ]
    st = {s.job: s for s in job_statuses(app_config, latest, {}, now, str)}
    assert st["reply"].state == "stale" and "성공한 적 없음" in st["reply"].note
    assert st["generate"].state == "stale"  # 실행 도중 꺼짐
    assert st["health"].state == "ok"
    # 원고 만들기가 길어 댓글 확인이 밀려도(25분) 오래 안 돌았다고 하지 않는다
    latest2 = [
        {
            "job": "reply",
            "started_at": now - timedelta(minutes=25),
            "finished_at": now,
            "ok": True,
            "message": "새 댓글 없음",
        }
    ]
    assert job_statuses(app_config, latest2, {}, now, str)[0].state == "ok"


def test_show_all_accepts_true(make) -> None:  # noqa: F811
    build, _, repo = make
    rid = repo.start_run("reply")
    repo.finish_run(rid, True, "새 댓글 없음")
    client, _ = build()
    for q in ("1", "true"):
        r = client.get(f"/logs?all={q}")
        assert r.status_code == 200 and "모든 실행" in r.text


def test_stalls_show_up_as_todo(make) -> None:  # noqa: F811
    from nsm.engine.watchdog import STALLS_KEY

    build, _, repo = make
    repo.set_setting(
        STALLS_KEY, ["자료 모으기 2일째 관련 자료 0개: 수집하는 사이트 화면이 바뀌었거나"]
    )
    html = build()[0].get("/").text
    assert "계속 0건" in html and "자료 모으기 2일째 관련 자료 0개" in html
    assert "오늘 할 일이 없습니다" not in html
