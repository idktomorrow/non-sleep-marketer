"""알림함(종 모양)과 게시 대기 화면, 진행 표시 닫기(N2)."""

from __future__ import annotations

from concurrent.futures import Future

from nsm.adapters.notify.recording import RecordingNotifier, link_for
from tests.app.test_dashboard import _post, env  # noqa: F401  같은 대시보드 픽스처


# ---------------------------------------------------------------- 알림함
def test_recording_notifier_keeps_history_and_forwards(env) -> None:  # noqa: F811
    sent: list = []

    class Desktop:
        def notify(self, t, b, lvl):
            sent.append(t)

    n = RecordingNotifier(env["repo"], Desktop())
    n.notify("검토 대기 콘텐츠", "새 콘텐츠 2건이 검토를 기다립니다", "info")
    n.notify("댓글 확인 필요", "사람이 확인해야 할 댓글이 1건 있습니다", "warn")
    items = env["repo"].list_notifications()
    assert [i["title"] for i in items] == ["댓글 확인 필요", "검토 대기 콘텐츠"]  # 최신 먼저
    assert [i["link"] for i in items] == ["/comments", "/review"]
    assert sent == ["검토 대기 콘텐츠", "댓글 확인 필요"]
    assert env["repo"].unread_notification_count() == 2


def test_link_for_titles() -> None:
    assert link_for("게시 포기") == "/queue" and link_for("자료 모으기 완료") == "/"
    assert link_for("잡 실패: collect") == "/logs" and link_for("비용 상한 도달") == "/performance"


def test_bell_shows_unread_and_reading_goes_to_link(env) -> None:  # noqa: F811
    repo = env["repo"]
    a = repo.add_notification("검토 대기 콘텐츠", "2건", "info", "/review")
    repo.add_notification("댓글 확인 필요", "1건", "warn", "/comments")
    html = env["client"].get("/").text
    assert 'class="bell-count">2<' in html and "검토 대기 콘텐츠" in html
    assert "전체 읽음" in html
    r = _post(env, f"/notifications/{a}/read")
    assert r.status_code == 303 and r.headers["location"] == "/review"
    assert repo.unread_notification_count() == 1
    assert 'class="bell-count">1<' in env["client"].get("/notifications/badge").text
    assert _post(env, "/notifications/nope/read").status_code == 404


def test_read_all_and_notifications_page(env) -> None:  # noqa: F811
    repo = env["repo"]
    for i in range(3):
        repo.add_notification(f"알림 {i}", "내용", "info", "/")
    assert "알림 2" in env["client"].get("/notifications").text
    r = env["client"].post(
        "/notifications/read-all",
        data={"csrf": env["state"].csrf_token},
        headers={"referer": "http://127.0.0.1:8765/queue"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/queue" and repo.unread_notification_count() == 0
    assert "bell-count" not in env["client"].get("/").text


def test_stored_link_must_be_local(env) -> None:  # noqa: F811
    nid = env["repo"].add_notification("이상한 링크", "x", "info", "//evil.example.com")
    assert _post(env, f"/notifications/{nid}/read").headers["location"] == "/"


def test_prune_keeps_recent(env) -> None:  # noqa: F811
    repo = env["repo"]
    for i in range(5):
        repo.add_notification(f"n{i}", "", "info", "/")
    repo.prune_notifications(keep=2)
    assert len(repo.list_notifications(10)) == 2


# ---------------------------------------------------------------- 진행 표시 닫기
def test_finished_toast_can_be_dismissed(env) -> None:  # noqa: F811
    fut: Future = Future()
    env["state"].run_job_now = lambda name: fut
    _post(env, "/jobs/collect/run")
    rid = env["state"].manual_runs[-1].id
    _post(env, f"/jobs/status/{rid}/dismiss")  # 실행 중에는 닫히지 않는다
    assert "실행 중" in env["client"].get("/jobs/status").text
    fut.set_result("collect")
    assert "닫기" in env["client"].get("/jobs/status").text
    r = _post(env, f"/jobs/status/{rid}/dismiss")
    assert r.status_code == 200 and "자료 모으기" not in r.text


# ---------------------------------------------------------------- 게시 대기
def _approve(env, cid: str = "c1") -> None:  # noqa: F811
    env["repo"].transition_content(cid, "approved", approved_by="human")


def test_queue_lists_approved_with_platform_status(env) -> None:  # noqa: F811
    assert "게시를 기다리는 원고가 없습니다" in env["client"].get("/queue").text
    _approve(env)
    html = env["client"].get("/queue").text
    topic = env["repo"].get_content("c1").generated.topic
    assert topic in html and "스레드 · 게시 대기" in html and "인스타그램 · 게시 대기" in html
    assert "검토로 되돌리기" in html
    assert '<span class="count soft">1</span>' in html  # 메뉴 숫자


def test_unapprove_stops_publishing_and_opens_editor(env) -> None:  # noqa: F811
    _approve(env)
    repo = env["repo"]
    assert repo.due_publications("threads", env["state"].clock.now())
    r = _post(env, "/queue/c1/unapprove")
    assert r.status_code == 303 and r.headers["location"] == "/review/c1"
    c = repo.get_content("c1")
    assert c.status == "pending_review" and "되돌림" in c.review_reason
    assert not repo.due_publications("threads", env["state"].clock.now())  # 게시 멈춤
    assert 'id="edit-form"' in env["client"].get("/review/c1").text  # 바로 고칠 수 있음
    assert _post(env, "/queue/c1/unapprove").status_code == 409  # 이미 검토 대기


def test_discard_from_queue_returns_to_queue(env) -> None:  # noqa: F811
    _approve(env)
    r = _post(env, "/review/c1/discard")
    assert r.headers["location"] == "/queue"
    assert env["repo"].get_content("c1").status == "discarded"
    assert not env["repo"].due_publications("instagram", env["state"].clock.now())


def test_actions_blocked_while_publishing(env) -> None:  # noqa: F811
    _approve(env)
    repo = env["repo"]
    pub = repo.due_publications("threads", env["state"].clock.now())[0]
    repo.transition_publication(pub.id, "publishing", increment_attempts=True)
    assert _post(env, "/queue/c1/unapprove").status_code == 409
    assert _post(env, "/review/c1/discard").status_code == 409


def test_detail_of_queued_content_offers_unapprove(env) -> None:  # noqa: F811
    _approve(env)
    html = env["client"].get("/review/c1").text
    assert "검토로 되돌려 고치기" in html and "← 게시 대기" in html
    assert 'id="edit-form"' not in html  # 승인된 원고는 바로 고치지 않는다
