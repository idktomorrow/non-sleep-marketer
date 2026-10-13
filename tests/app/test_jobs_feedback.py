"""지금 실행 진행 표시·완료 알림·알림 켜기/끄기(N1)."""

from __future__ import annotations

from concurrent.futures import Future

from nsm.app.ko import result_text
from nsm.engine.context import JobResult
from tests.app.test_dashboard import _post, env  # noqa: F401  같은 대시보드 픽스처


def _pending(env, results: dict):  # noqa: F811
    """run_job_now가 끝나지 않은 Future를 돌려주게 바꾼다(실행 중 상태를 보려고)."""

    def run(name: str) -> Future:
        f: Future = Future()
        results[name] = f
        return f

    env["state"].run_job_now = run


def test_run_now_stays_on_page_and_shows_progress(env) -> None:  # noqa: F811
    futures: dict = {}
    _pending(env, futures)
    sent: list = []
    env["state"].notify = lambda t, b, lvl: sent.append((t, b, lvl))
    r = env["client"].post(
        "/jobs/collect/run",
        data={"csrf": env["state"].csrf_token},
        headers={"referer": "http://127.0.0.1:8765/review?x=1"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/review"  # 누른 화면에 머문다
    running = env["client"].get("/jobs/status").text
    assert "자료 모으기" in running and "실행 중" in running and 'hx-trigger="every 2s"' in running
    assert "실행 중" in env["client"].get("/").text  # 모든 화면 오른쪽 아래

    futures["collect"].set_result(
        JobResult("collect", counts={"new": 15, "relevant": 5, "bodies": 4})
    )
    done = env["client"].get("/jobs/status").text
    assert "자료 모으기 완료" in done and "새 자료 15건 · 관련 자료 5건" in done
    assert "hx-trigger" not in done  # 다 끝나면 다시 읽지 않는다
    assert sent == [("자료 모으기 완료", "새 자료 15건 · 관련 자료 5건", "info")]

    env["state"].clock.advance(minutes=4)
    assert "자료 모으기" not in env["client"].get("/jobs/status").text  # 3분 지나면 사라짐


def test_failed_run_is_reported(env) -> None:  # noqa: F811
    futures: dict = {}
    _pending(env, futures)
    sent: list = []
    env["state"].notify = lambda t, b, lvl: sent.append((t, lvl))
    _post(env, "/jobs/publish/run")
    futures["publish"].set_exception(RuntimeError("Aside 응답 없음"))
    html = env["client"].get("/jobs/status").text
    assert "게시하기 실패" in html and "Aside 응답 없음" in html
    assert sent == [("게시하기 실패", "warn")]


def test_foreign_referer_is_not_followed(env) -> None:  # noqa: F811
    r = env["client"].post(
        "/jobs/collect/run",
        data={"csrf": env["state"].csrf_token},
        headers={"referer": "https://evil.example.com/x"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"


def test_result_text_in_korean() -> None:
    assert result_text(JobResult("generate", skipped="쓸 자료 없음")) == "건너뜀: 쓸 자료 없음"
    assert result_text(JobResult("publish", counts={"published": 2, "failed": 0})) == (
        "게시 2건 · 실패 0건"
    )
    assert result_text(JobResult("health", counts={"paused": []})) == "바뀐 것 없음"
    assert "오류 1건: 타임아웃" in result_text(JobResult("reply", errors=["타임아웃"]))
    assert result_text("collect") == "끝났습니다"


def test_notification_switch_and_test_button(env) -> None:  # noqa: F811
    from tests.app.test_settings import _form

    sent: list = []
    env["state"].notify = lambda t, b, lvl: sent.append(t)
    html = env["client"].get("/settings").text
    assert 'name="desktop_notifications"' in html and "checked" in html
    r = _post(env, "/notify/test")
    assert r.headers["location"] == "/settings?notified=1" and sent == ["논슬립 마케터 알림 테스트"]

    _post(env, "/settings", **_form(env["holder"]["cfg"], desktop_notifications=""))
    assert env["holder"]["cfg"].settings.desktop_notifications is False
    r = _post(env, "/notify/test")
    assert r.headers["location"] == "/settings?notified=off" and len(sent) == 1
    assert "알림이 꺼져 있어" in env["client"].get("/settings?notified=off").text
